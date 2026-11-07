"""extref_app.py against the fake: external dither (FAKE_EXTREF=1: a sine at 1000.0137 Hz on the PZT and, 5 Vpp / 20,
on IN2), dual-phase lock-in on iq2 locked to it by iq0 / iq1 tracking, raw PI, error -> OUT2, ramp.
Run from app/test:  python test_extref.py"""
import os
import sys
import threading
import time

os.environ["FAKE_LOCKIN"] = "iq2"
os.environ["FAKE_EXTREF"] = "1"
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import extref_app as E
import extref_schematic as ES
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


check("schematic: every settings key has exactly one home", not ES.ownership_problems(E.KEYS),
      str(ES.ownership_problems(E.KEYS)))
ctl = E.ExtRefController("fake", "t", dict(E.DEFAULTS))
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


def fresh(n=2):
    q = ctl.seq
    t0 = time.time()
    while ctl.seq < q + n and time.time() - t0 < 30:
        time.sleep(0.05)


def win(names, dur=0.1):
    return eng.traces(names, dur, "immediately")[0]


def angle():
    """The X-Y angle of the error signal in a ramp trace (display helper of the app)."""
    fresh(2)
    th_ = ctl.snapshot()["theta"]
    return None if th_ is None else th_["theta"]


time.sleep(1.5)
check("start-up: ramp on OUT1, PI off, no dither from the Red Pitaya (iq2 amplitude 0, output off)",
      ctl.mode == "ramp" and r.asg0.output_direct == "out1" and r.pid0.output_direct == "off"
      and r.iq2.amplitude == 0 and r.iq2.output_direct == "off")
check("reference modules: iq0 / iq1 on IN2, outputs off, same NCO frequency as iq2", r.iq0.input == "in2"
      and r.iq1.input == "in2" and r.iq0.output_direct == "off" and r.iq1.output_direct == "off"
      and r.iq0.frequency == r.iq1.frequency == r.iq2.frequency)
check("OUT2: error output off at start, pid2 off", r.pid1.output_direct == "off" and r.pid2.output_direct == "off")
log = "\n".join(ctl.logs)
check("start-up measures the IN2 reference (no automatic change)", "IN2 reference measured at 1000.01" in log
      or "IN2 reference 1000.01" in log, [l for l in ctl.logs if "IN2" in l][-1:])
print("--- reference tracking")
time.sleep(4.0)
rf = ctl.snapshot()["ref"]
check("tracking: reference found and locked (sign found once)", rf["ok"] and rf["locked"] and ctl._sign in (1, -1),
      str({k: rf.get(k) for k in ("level", "ok", "locked", "rate")}))
check("tracking supplies the 0.0137 Hz difference (~4.9 deg/s)", rf.get("rate") is not None
      and abs(abs(rf["rate"]) - 360 * 0.0137) < 1.0, f"{rf.get('rate')}")
a1 = angle(); time.sleep(6.0); a2 = angle()
check("tracking on: the X-Y angle of the error signal stays put", a1 is not None and a2 is not None
      and abs(E.wrap(a2 - a1)) < 4, f"{a1:+.1f} -> {a2:+.1f} deg")
sw("track", False)
a1 = angle(); time.sleep(6.0); a2 = angle()
check("tracking off: X / Y rotate at the frequency difference", a1 is not None and a2 is not None
      and abs(E.wrap(a2 - a1)) > 15, f"{a1:+.1f} -> {a2:+.1f} deg")
sw("track", True)
ctl.submit("find_ref"); idle()
check("Find f: NCO set to the IN2 frequency", abs(ctl.f_nco - eng.f_ext) < 0.005 and abs(r.iq2.frequency - eng.f_ext)
      < 0.005 and abs(ctl.s["ref_hz"] - eng.f_ext) < 0.005, f"{ctl.f_nco:.4f} vs {eng.f_ext:.4f}")
fnd = ctl.snapshot()["ref"]["find"]
check("Find f: reference amplitude at the connector (IN2 scale 20)", abs(fnd["vpp"] - 5.0) < 0.2, f"{fnd['vpp']:.2f} Vpp")
apply(ref_hz=999.7)                                    # 0.31 Hz off: 112 deg/s
time.sleep(12.0)
check("frequency correction: a 0.3 Hz error is pulled in by the tracking", abs(ctl.f_nco - eng.f_ext) < 0.05
      and ctl.snapshot()["ref"]["locked"], f"NCO {ctl.f_nco:.4f} Hz")
print("--- lock-in, phase by hand, lock")
apply(ramp_hz=5.0, phase_deg=0.0)
time.sleep(1.0)
th0 = angle()
check("X-Y signal angle visible", th0 is not None, str(th0))
apply(phase_deg=round(ctl.s["phase_deg"] + (th0 if ctl._sign is None else 0) - ctl._sign * 0 + 0, 1))
# rotate the signal into X by hand: try +angle and -angle, keep the one with Y ~ 0 (what the user does by eye)
best = None
for ph in (E.wrap(th0), E.wrap(-th0)):
    apply(phase_deg=round(ph, 1))
    fresh(2)
    tr = ctl.trace
    yr = np.ptp(tr["y"]) / max(np.ptp(tr["x"]), 1e-9)
    if best is None or yr < best[1]:
        best = (ph, yr)
apply(phase_deg=round(best[0], 1))
fresh(2)
tr = ctl.trace
check("phase set by hand: Y ~ 0, the error signal in X", np.ptp(tr["y"]) < 0.15 * np.ptp(tr["x"]),
      f"X pp {np.ptp(tr['x'])*1e3:.1f} mV, Y pp {np.ptp(tr['y'])*1e3:.1f} mV")
import cavity_lock_app as CL
dur = 1.0 / ctl.s["ramp_hz"]
n = int(np.searchsorted(tr["t"], dur * 0.999))
o = tr["out1"][:n]
k = max(3, len(o) // 200)
sm = np.convolve(np.pad(o, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
s_ = ctl.s
an = CL.analyze_sweep(dict(t=tr["t"][:n], v=sm, pd=tr["pd"][:n], err=tr["x"][:n], up=np.gradient(sm) > 0),
                      dict(prominence=0.1, target_mv=None, rate=2 * s_["ramp_mvpp"] * 1e-3 * s_["ramp_hz"],
                           lpf_hz=s_["lpf_hz"], lpf_order=s_["lpf_order"], dither_hz=s_["ref_hz"],
                           dither_mvpp=30.0, kappa=None, ramp_hz=s_["ramp_hz"], ramp_mvpp=s_["ramp_mvpp"]))
v0, S = an["v0"], an["slope"]
pk = an["peaks"][an["target"]]["height"]
check("lock point in the ramp data", abs(v0 - eng.vc) < 0.004, f"{v0*1e3:+.2f} vs {eng.vc*1e3:+.2f} mV, S {S:+.2f}")
sw("ramp", False)
apply(offset_mv=round(v0 * 1e3, 2), p=round(-0.1 / S, 5), i=round(-10.0 / S, 4), clamp_lo_mv=-400.0,
      clamp_hi_mv=400.0)
check("PI limiters written", abs(r.pid0.min_voltage + 0.4) < 1e-9 and abs(r.pid0.max_voltage - 0.4) < 1e-9)
sw("pi", True)
time.sleep(2.5)
w = win(["iq2", "in1", "out1"], 0.2)
check("PI → OUT1: locked with the external dither (X ~ 0, transmission up)", ctl.mode == "pi"
      and r.pid0.output_direct == "out1" and abs(np.mean(w[0])) < 0.003 and np.mean(w[1]) > 0.5 * pk,
      f"X {np.mean(w[0])*1e3:+.2f} mV, IN1 {np.mean(w[1]):.3f} of {pk:.3f}")
check("OUT1 carries no dither (offset + PI only)", np.std(w[2]) < 0.002, f"OUT1 rms {np.std(w[2])*1e3:.2f} mV")
eng.vc += 0.003
time.sleep(3.0)
check("follows a drift", np.mean(win(["in1"], 0.2)[0]) > 0.5 * pk)
print("--- OUT2")
apply(eo_gain=-3.0, eo_offset_mv=20.0, eo_limit_mv=300.0)
sw("eo", True)
w = win(["out2", "iq2"], 0.05)
check("Error → OUT2: pid1 = -3 x X + 20 mV, clamp 300 mV", r.pid1.output_direct == "out2" and r.pid1.i == 0
      and np.allclose(w[0], np.clip(-3 * w[1] + 0.02, -0.3, 0.3), atol=1e-6))
apply(eo_source="Y")
check("error output source Y -> pid1.input iq2_2", r.pid1.input == "iq2_2")
apply(eo_source="X")
sw("pi_out2", True)
check("PI → OUT2 turns Error → OUT2 off (OUT2 carries one of them); PI on both outputs",
      r.pid1.output_direct == "off" and r.pid0.output_direct == "both" and not ctl.s["eo_on"])
sw("eo", True)
check("Error → OUT2 turns PI → OUT2 off", r.pid1.output_direct == "out2" and r.pid0.output_direct == "out1"
      and not ctl.s["pi_out2"])
sw("ramp", True)
check("Ramp on: PI off OUT1, ramp on OUT1, error still on OUT2", ctl.mode == "ramp" and r.pid0.output_direct == "off"
      and r.asg0.amplitude > 0 and r.pid1.output_direct == "out2")
sw("output", False)
check("Output off: OUT1 = 0 V", ctl.mode == "off" and r.asg0.output_direct == "off")
sw("output", True)
sw("pi", True)
time.sleep(1.5)
print("--- restart while locked")
stop.set(); th.join(5)
before = (r.pid0.p, r.pid0.i, r.pid0.output_direct, r.iq2.phase, r.iq2.frequency, r.pid1.output_direct)
n0 = len(F.WRITES)


class Same:
    def __init__(self, **kw):
        self.rp = r


F.Pyrpl = Same
ctl2 = E.ExtRefController("fake", "t", dict(E.DEFAULTS))
after = (r.pid0.p, r.pid0.i, r.pid0.output_direct, r.iq2.phase, r.iq2.frequency, r.pid1.output_direct)
neww = [w for w in F.WRITES[n0:] if w[1] in ("pid0", "pid1", "asg0") or (w[1] == "iq2" and w[2] != "phase")]
dph = [abs(E.wrap(w[3] - before[3])) for w in F.WRITES[n0:] if w[1] == "iq2" and w[2] == "phase"]
check("restart: PI, error output and lock-in adopted untouched (iq2 phase only moved by the tracking, < 5 deg)",
      ctl2.mode == "pi" and before[:3] == after[:3] and before[4:] == after[4:] and not neww and ctl2.s["eo_on"]
      and abs(ctl2.f_nco - r.iq2.frequency) < 1e-9 and max(dph or [0]) < 5, f"{neww[:3]} max dphase {max(dph or [0]):.2f}")
dl = ES.diagram_state(ctl2.snapshot(), ctl2.switches())
check("diagram state: every block has readback text, all switches", set(dl["boxes"]) == {b["id"] for b in ES.BLOCKS}
      and set(dl["sw"]) == {x["id"] for x in ES.SWITCHES})
mine = [w for w in F.WRITES if w[4] == "extref_app.py"]
check("writes only iq0, iq1, iq2, pid0, pid1, asg0, scope", {w[1] for w in mine} <=
      {"iq0", "iq1", "iq2", "pid0", "pid1", "asg0", "scope"}, str(sorted({w[1] for w in mine})))
bad = [w for w in mine if (w[2] == "output_direct" and w[1] in ("iq0", "iq1", "iq2") and w[3] != "off")
       or (w[1] == "iq2" and w[2] == "amplitude" and w[3] != 0)
       or (w[2] == "output_direct" and w[1] == "pid1" and w[3] not in ("out2", "off"))
       or (w[2] == "output_direct" and w[1] == "asg0" and w[3] not in ("out1", "off"))]
check("never a dither or a reference module on an output; pid1 only on OUT2", not bad, str(bad[:3]))
print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL PASS")
