"""lockin_pi_app.py against the fake: dual-phase lock-in on iq2 (FAKE_LOCKIN=iq2, the cavity PZT on OUT1), raw PI
pid0 on X or Y, ramp asg0; intensity raw PI pid2 on IN2 -> OUT2 (the fake AOM). Run from app/test:
python test_lockin_pi.py"""
import os
import sys
import threading
import time

os.environ["FAKE_LOCKIN"] = "iq2"
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import lockin_pi_app as E
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


ctl = E.LockinPIController("fake", "t", dict(E.DEFAULTS))
r, eng = ctl.r, ctl.r.eng
stop = threading.Event()
th = threading.Thread(target=ctl.run, args=(stop,), daemon=True)
th.start()


def idle(t=20):
    t0 = time.time()
    while time.time() - t0 < t and not ctl.cmds.empty():
        time.sleep(0.05)
    time.sleep(0.5)


def apply(**kw):
    new = {k: ctl.s[k] for k in E.KEYS}
    new.update(kw)
    ctl.submit("apply", (new, ctl.version))
    idle()


def sw(name, on):
    ctl.submit("switch", (name, on)); idle()


def win(names, dur=0.1):
    return eng.traces(names, dur, "immediately")[0]


time.sleep(1.5)
check("start-up lock: RAMP + dither on OUT1 (iq2), PI off", ctl.mode == "ramp" and r.asg0.output_direct == "out1"
      and r.asg0.amplitude > 0 and r.iq2.output_direct == "out1" and r.pid0.output_direct == "off")
check("start-up intensity: servo off, OUT2 = 0 V", not ctl.int_on and r.pid2.output_direct == "off"
      and r.pid2.ival == 0)
check("filters off by default", list(r.pid0.inputfilter) == [0] * 4 and list(r.pid2.inputfilter) == [0] * 4
      and not ctl.s["filt_on"] and not ctl.s["int_filt_on"])
check("lock-in = iq2 demodulating IN1, PI input X = 'iq2'", r.iq2.input == "in1" and r.pid0.input == "iq2")
import lockin_pi_schematic as LS
check("schematic: every settings key has exactly one home", not LS.ownership_problems(E.KEYS),
      str(LS.ownership_problems(E.KEYS)))


def fresh(n=2):
    q = ctl.seq
    t0 = time.time()
    while ctl.seq < q + n and time.time() - t0 < 20:
        time.sleep(0.05)


fresh()
tr = ctl.trace
per = tr["t"][-1] * ctl.s["ramp_hz"]
o = tr["out1"] - np.mean(tr["out1"])
nb = len(o) // 200 * 200
ob = o[:nb].reshape(200, -1).mean(axis=1)               # 200 bins: averages out the dither
turns = int(np.sum(np.diff(np.sign(np.diff(ob))[2:-2]) != 0))
check("ramp trace shows 4 ramp periods (default), triggered", ctl.s["ramp_periods"] == 4 and 3.95 < per < 4.05
      and 6 <= turns <= 9, f"{per:.2f} periods, {turns} turning points")
d = {"i_hz": 3.3, "int_i_hz": 44.0, "p": 0.5, "bogus": 1}
ld = E.load_settings(d)
check("old settings files: i_hz / int_i_hz load as i / int_i, values unchanged", ld == {"i": 3.3, "int_i": 44.0,
                                                                                      "p": 0.5}, str(ld))
apply(ramp_hz=5.0, ramp_periods=1)                     # slow ramp, one period: the lock-point analysis below
fresh()
apply(phase_deg=F.PZT_LAG_DEG + 60)
fresh()
tr = ctl.trace
th0 = ctl.snapshot()["theta"]
check("ramp frame: IN1, OUT1, X and Y; signal angle shown (display only)", tr["kind"] == "ramp" and len(tr["y"]) ==
      len(tr["x"]) and np.ptp(tr["y"]) > 0.3 * np.ptp(tr["x"]) and th0 is not None and th0["q"] > 10,
      f"X pp {np.ptp(tr['x']):.3f}, Y pp {np.ptp(tr['y']):.3f}, theta {th0}")
check("the phase is not touched by the app", abs(r.iq2.phase - (F.PZT_LAG_DEG + 60)) < 1e-9)
apply(phase_deg=F.PZT_LAG_DEG)                         # by hand: the whole signal in X
fresh()
tr = ctl.trace
check("phase set by hand: Y ~ 0, the signal in X", np.ptp(tr["y"]) < 0.1 * np.ptp(tr["x"]),
      f"X pp {np.ptp(tr['x']):.3f}, Y pp {np.ptp(tr['y']):.3f}")
# lock point from the ramp data, found by the cavity app's sweep analysis (test side only; the app derives nothing)
import cavity_lock_app as CL
o = tr["out1"]
k = max(3, len(o) // 200)
sm = np.convolve(np.pad(o, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
s_ = ctl.s
an = CL.analyze_sweep(dict(t=tr["t"], v=sm, pd=tr["pd"], err=tr["x"], up=np.gradient(sm) > 0),
                      dict(prominence=0.1, target_mv=None, rate=2 * s_["ramp_mvpp"] * 1e-3 * s_["ramp_hz"],
                           lpf_hz=s_["lpf_hz"], lpf_order=s_["lpf_order"], dither_hz=s_["dither_hz"],
                           dither_mvpp=s_["dither_mvpp"], kappa=None, ramp_hz=s_["ramp_hz"],
                           ramp_mvpp=s_["ramp_mvpp"]))
v0, S = an["v0"], an["slope"]
pk = an["peaks"][an["target"]]["height"]
check("lock point found in the ramp data (within the low-pass lag of the ramp)", abs(v0 - eng.vc) < 0.004, f"{v0*1e3:+.2f} vs {eng.vc*1e3:+.2f} mV, "
      f"slope {S:+.1f} V/V")
sw("ramp", False)
check("Ramp off: offset + dither, PI off", ctl.mode == "dc" and r.asg0.amplitude == 0 and r.iq2.output_direct == "out1")
p_typed, i_typed = round(-0.1 / S, 5), round(-10.0 / S, 4)
v_before = ctl.version
apply(offset_mv=round(v0 * 1e3, 2), p=p_typed, i=i_typed)          # raw gains, chosen by the user (|i S| = 10 Hz)
check("p and i go to pid0.p / pid0.i as typed (PyRPL units, FPGA rounding only)", abs(r.pid0.p - p_typed) < 2.5e-4
      and abs(r.pid0.i - i_typed) < 0.005, f"p {r.pid0.p}, i {r.pid0.i}")
check("typed values are never rewritten (no push-back of rounded values)", ctl.s["p"] == p_typed
      and ctl.s["i"] == i_typed and ctl.version == v_before, f"{ctl.s['p']}, {ctl.s['i']}, version {ctl.version}")
sw("pi", True)
time.sleep(2.0)
w = win(["iq2", "in1"], 0.2)
check("PI on: locked, X averages to 0, transmission up", ctl.mode == "pi" and r.pid0.output_direct == "out1"
      and abs(np.mean(w[0])) < 0.003 and np.mean(w[1]) > 0.5 * pk, f"X {np.mean(w[0])*1e3:+.2f} mV, "
      f"IN1 {np.mean(w[1]):.3f} V")
eng.vc += 0.003
time.sleep(3.0)
w = win(["iq2", "in1", "out1"], 0.2)
check("follows a drift", np.mean(w[1]) > 0.5 * pk and abs(np.mean(w[2]) - eng.vc) < 0.0015,
      f"IN1 {np.mean(w[1]):.3f} V, OUT1 {np.mean(w[2])*1e3:+.2f} vs {eng.vc*1e3:+.2f} mV")
apply(filt1_hz=2000.0, filt2_hz=-1.0)
check("filter values alone do nothing while the Filter toggle is off", list(r.pid0.inputfilter) == [0] * 4)
sw("filt", True)
f = list(r.pid0.inputfilter)
check("Filter toggle on -> pid0.inputfilter; typed values kept", f[0] > 0 and f[1] < 0
      and ctl.s["filt1_hz"] == 2000.0 and ctl.s["filt2_hz"] == -1.0, str(f))
sw("filt", False)
check("Filter toggle off -> all stages 0", list(r.pid0.inputfilter) == [0] * 4)
apply(pi_input="Y")
check("PI input Y -> pid0.input iq2_2", r.pid0.input == "iq2_2")
apply(pi_input="X")
ctl.submit("reset_i"); idle()
check("Reset integrator (then the lock pulls it back)", any("lock integrator reset" in l for l in ctl.logs))
time.sleep(1.5)
print("--- intensity (OUT2)")
apply(int_p=0.01, int_i=2000.0, int_setpoint_mv=400.0)
check("intensity gains written while off, output stays off", r.pid2.output_direct == "off" and abs(r.pid2.i - 2000) < 1
      and r.pid2.input == "in2")
sw("int_on", True)
time.sleep(1.0)
x2 = np.mean(win(["in2", "out2"], 0.01)[0])
check("intensity servo on: IN2 at the setpoint", ctl.int_on and r.pid2.output_direct == "out2"
      and abs(x2 - 0.4) < 0.005, f"IN2 {x2*1e3:.1f} mV")
check("the lock is still locked", np.mean(win(["in1"], 0.2)[0]) > 0.5 * pk)
apply(int_setpoint_mv=300.0)
time.sleep(0.8)
x2 = np.mean(win(["in2"], 0.01)[0])
check("intensity setpoint 300 mV", abs(x2 - 0.3) < 0.005, f"{x2*1e3:.1f} mV")
apply(int_filt1_hz=50000.0)
sw("int_filt", True)
check("intensity Filter toggle -> pid2.inputfilter", r.pid2.inputfilter[0] > 0)
sw("int_filt", False)
check("intensity Filter toggle off", list(r.pid2.inputfilter) == [0] * 4)
apply(int_input="in1")
check("intensity input selectable (in1)", r.pid2.input == "in1")
apply(int_input="in2")
print("--- restart with both servos running")
stop.set(); th.join(5)
before = [(m.p, m.i, m.input, m.output_direct, m.setpoint) for m in (r.pid0, r.pid2)] + [r.iq2.phase]
n0 = len(F.WRITES)


class Same:
    def __init__(self, **kw):
        self.rp = r


F.Pyrpl = Same
ctl2 = E.LockinPIController("fake", "t", dict(E.DEFAULTS))
after = [(m.p, m.i, m.input, m.output_direct, m.setpoint) for m in (r.pid0, r.pid2)] + [r.iq2.phase]
neww = [w for w in F.WRITES[n0:] if w[1] in ("pid0", "pid2", "iq2", "asg0")]
check("restart: lock and intensity adopted untouched", ctl2.mode == "pi" and ctl2.int_on and before == after
      and not neww and abs(ctl2.s["int_p"] - r.pid2.p) < 1e-12 and abs(ctl2.s["i"] - r.pid0.i) < 1e-12 and abs(ctl2.s["phase_deg"] - r.iq2.phase) < 1e-9,
      str(neww[:3]))
stop2 = threading.Event()
threading.Thread(target=ctl2.run, args=(stop2,), daemon=True).start()
time.sleep(0.5)
ctl = ctl2
sw("output", False)
check("lock Output off: OUT1 = 0 V, intensity untouched", ctl.mode == "off" and np.allclose(win(["out1"])[0], 0)
      and r.pid2.output_direct == "out2")
sw("int_on", False)
check("intensity servo off: OUT2 = 0 V", r.pid2.output_direct == "off" and np.allclose(win(["out2"], 0.001)[0], 0,
                                                                                         atol=1e-9))
dg = LS.diagram_state(ctl.snapshot(), ctl.switches())
check("diagram state: every block has readback text, all switches present", set(dg["boxes"]) == {b["id"] for b in
      LS.BLOCKS} and set(dg["sw"]) == {x["id"] for x in LS.SWITCHES})
stop2.set()
mine = [w for w in F.WRITES if w[4] == "lockin_pi_app.py"]
check("writes only iq2, pid0, pid2, asg0, scope", {w[1] for w in mine} <= {"iq2", "pid0", "pid2", "asg0", "scope"},
      str(sorted({w[1] for w in mine})))
bad = [w for w in mine if w[2] == "output_direct" and ((w[1] in ("iq2", "pid0", "asg0") and w[3] not in ("out1", "off"))
                                                       or (w[1] == "pid2" and w[3] not in ("out2", "off")))]
check("lock modules only on OUT1, pid2 only on OUT2", not bad, str(bad[:3]))
print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL PASS")
