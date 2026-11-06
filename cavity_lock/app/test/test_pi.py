"""PI servo app, pi_app.py, against the fake with an external error signal on IN1 (FAKE_PLANT=errin):
IN1 = dispersive cavity error (slope about -140 V/V at resonance) for the OUT1 drive; pid0 = PI with its input filter.
Run from app/test:  python test_pi.py"""
import os
import sys
import threading
import time

os.environ["FAKE_PLANT"] = "errin"
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import pi_app as E
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


ctl = E.PIController("fake", "t", dict(E.DEFAULTS))
r, eng = ctl.r, ctl.r.eng
stop = threading.Event()
th = threading.Thread(target=ctl.run, args=(stop,), daemon=True)
th.start()


def idle(t=20):
    t0 = time.time()
    while time.time() - t0 < t and not ctl.cmds.empty():
        time.sleep(0.05)
    time.sleep(0.4)


def apply(**kw):
    new = {k: ctl.s[k] for k in E.KEYS}
    new.update(kw)
    ctl.submit("apply", (new, ctl.version))
    idle()


def win(names, dur=0.05):
    return eng.traces(names, dur, "immediately")[0]


time.sleep(1.5)
check("start-up: RAMP on OUT1, PI off", ctl.mode == "ramp" and r.asg0.output_direct == "out1" and r.asg0.amplitude > 0
      and r.pid0.output_direct == "off")
check("input filter off by default", list(r.pid0.inputfilter) == [0, 0, 0, 0], str(r.pid0.inputfilter))
check("PI defaults p = i = 0, input in1", r.pid0.p == 0 and r.pid0.i == 0 and r.pid0.input == "in1")
time.sleep(1.0)
tr = ctl.trace
check("ramp trace: one period of IN1 and OUT1, triggered", tr["kind"] == "ramp"
      and abs(np.ptp(tr["out1"]) - 0.5) < 0.02 and np.ptp(tr["in1"]) > 0.3,
      f"OUT1 pp {np.ptp(tr['out1']):.3f} V, IN1 pp {np.ptp(tr['in1']):.3f} V")
# find the main resonance's crossing from the ramp data: steepest negative slope with IN1 crossing 0
o, e = tr["out1"], tr["in1"]
up = np.gradient(o) > 0                               # the rising half of the triangle
o, e = o[up], e[up]
e_s = np.convolve(e, np.ones(9) / 9, mode="same")
zc = [j for j in range(5, len(e_s) - 5) if e_s[j - 1] > 0 >= e_s[j]]
best = max(zc, key=lambda j: np.ptp(e_s[max(j - 200, 0):j + 200]))   # the strongest + to - crossing
v0 = float(o[best])
check("resonance visible in the ramp", abs(v0 - eng.vc) < 0.003, f"{v0*1e3:+.2f} mV vs true {eng.vc*1e3:+.2f} mV")
ctl.submit("switch", ("ramp", False)); idle()
check("Ramp off: OUT1 = offset only", ctl.mode == "dc" and r.asg0.amplitude == 0 and r.pid0.output_direct == "off")
apply(offset_mv=round(v0 * 1e3, 1))
check("offset written to asg0", abs(r.asg0.offset - round(v0 * 1e3, 1) * 1e-3) < 1e-9)
apply(p=0.001, i_hz=0.2)
check("p and i raw, snapped to the FPGA steps and shown", ctl.s["p"] == r.pid0.p and abs(r.pid0.p - 0.001) < 2.5e-4
      and abs(ctl.s["i_hz"] - r.pid0.i) < 1e-12, f"p {r.pid0.p}, i {r.pid0.i}")
check("gains are written while PI is off, output stays off", r.pid0.output_direct == "off")
ctl.submit("switch", ("pi", True)); idle()
time.sleep(2.0)
w = win(["in1", "out1"], 0.2)
check("PI on: locks, IN1 averages to the setpoint", ctl.mode == "pi" and r.pid0.output_direct == "out1"
      and abs(np.mean(w[0])) < 0.002, f"IN1 mean {np.mean(w[0])*1e3:+.3f} mV")
eng.vc += 0.004                                       # drift of 0.7 linewidths
time.sleep(2.5)
w = win(["in1", "out1"], 0.2)
check("PI follows a drift (integrator)", abs(np.mean(w[0])) < 0.002 and abs(np.mean(w[1]) - eng.vc) < 0.001,
      f"IN1 {np.mean(w[0])*1e3:+.3f} mV, OUT1 {np.mean(w[1])*1e3:+.2f} mV vs resonance {eng.vc*1e3:+.2f}")
check("integrator shown", ctl.snapshot()["ival"] is not None and abs(ctl.snapshot()["ival"] - 0.004) < 0.0015,
      str(ctl.snapshot()["ival"]))
apply(setpoint_mv=50.0)
time.sleep(2.0)
w = win(["in1"], 0.2)
check("setpoint: IN1 locks to +50 mV", abs(np.mean(w[0]) - 0.05) < 0.003 and abs(r.pid0.setpoint - 0.05) < 1e-9,
      f"{np.mean(w[0])*1e3:+.2f} mV")
apply(setpoint_mv=0.0)
apply(filt1_hz=100.0, filt2_hz=-5.0)
f = list(r.pid0.inputfilter)
check("input filter: low-pass + high-pass stages written, snapped values shown", f[0] > 0 and f[1] < 0
      and ctl.s["filt1_hz"] == round(f[0], 3) and ctl.s["filt2_hz"] == round(f[1], 3), str(f))
apply(filt1_hz=0.0, filt2_hz=0.0)
check("input filter back off", list(r.pid0.inputfilter) == [0, 0, 0, 0])
apply(clamp_lo_mv=-30.0, clamp_hi_mv=30.0)
check("clamps written", abs(r.pid0.min_voltage + 0.03) < 1e-9 and abs(r.pid0.max_voltage - 0.03) < 1e-9)
apply(i_hz=-0.2, p=-0.001)                            # wrong sign: runs away to a clamp
time.sleep(2.0)
iv = r.pid0.ival
check("wrong sign: the PI runs to its clamp (and stays there)", abs(abs(iv) - 0.03) < 1e-6, f"ival {iv}")
ctl.submit("reset_i"); time.sleep(0.05)
check("Reset integrator", abs(r.pid0.ival) < 0.03)
apply(i_hz=0.2, p=0.001, clamp_lo_mv=-1000.0, clamp_hi_mv=1000.0)
time.sleep(1.5)
ctl.submit("switch", ("ramp", True)); idle(); time.sleep(0.5)
check("Ramp on while PI on: PI off, ramp on OUT1", ctl.mode == "ramp" and r.pid0.output_direct == "off"
      and r.pid0.ival == 0 and r.asg0.amplitude > 0)
apply(xaxis="OUT1", ramp_hz=50.0, ramp_mvpp=200.0)
check("ramp settings live", abs(r.asg0.frequency - 50) < 1e-9 and abs(2 * r.asg0.amplitude - 0.2) < 1e-9)
ctl.submit("switch", ("pi", True)); idle()
check("PI on from ramp: ramp off, offset + PI", ctl.mode == "pi" and r.asg0.amplitude == 0
      and r.pid0.output_direct == "out1")
time.sleep(1.5)
check("locks again", abs(np.mean(win(["in1"], 0.2)[0])) < 0.002)
ctl.submit("switch", ("output", False)); idle(); time.sleep(0.3)
w = win(["out1"])
check("Output off: OUT1 = 0 V", ctl.mode == "off" and np.allclose(w[0], 0, atol=1e-9))
ctl.submit("switch", ("ramp", True)); idle()
check("Ramp refused while Output is off", ctl.mode == "off")
ctl.submit("switch", ("output", True)); idle()
check("Output on: offset only", ctl.mode == "dc" and r.asg0.output_direct == "out1")
ctl.submit("switch", ("pi", True)); idle(); time.sleep(1.0)
mine = [w for w in F.WRITES if w[4] in ("pi_app.py",)]
mods = sorted({w[1] for w in mine})
check("writes only pid0, asg0 and the scope", set(mods) <= {"pid0", "asg0", "scope"}, str(mods))
check("nothing routed to OUT2", not [w for w in mine if w[2] == "output_direct" and w[3] in ("out2", "both")])
print("--- restart while locked")
stop.set(); th.join(5)
p0 = r.pid0
before = (p0.p, p0.i, p0.output_direct, p0.setpoint)
n0 = len(F.WRITES)


class Same:
    def __init__(self, **kw):
        self.rp = r


F.Pyrpl = Same
ctl2 = E.PIController("fake", "t", dict(E.DEFAULTS))
new = [w for w in F.WRITES[n0:] if w[1] == "pid0"]
check("restart: running PI adopted untouched", ctl2.mode == "pi" and (p0.p, p0.i, p0.output_direct, p0.setpoint)
      == before and not new and abs(ctl2.s["p"] - p0.p) < 1e-12 and abs(ctl2.s["offset_mv"] - r.asg0.offset * 1e3)
      < 1e-9, str(new[:3]))
print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL PASS")
