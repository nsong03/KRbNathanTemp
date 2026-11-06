"""2-phase lock-in spin-off, errsig2ph_app.py, against the fake (lock-in on iq2: X = iq2, Y = iq2_2) with an external
servo model. IN1 photodiode -> X -> OUT1 -> (external servo, simulated) -> IN2 -> x gain + DC + dither -> OUT2 -> PZT.
Run from app/test:  python test_errsig2ph.py"""
import os
import sys
import threading
import time

os.environ["FAKE_PZT_OUT"] = "out2"                   # this app's wiring: OUT2 drives the PZT
os.environ["FAKE_LOCKIN"] = "iq2"                     # the fake demodulates with iq2 (both quadratures)
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import errsig2ph_app as E
import errsig2ph_schematic as ES
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


check("every settings key has exactly one home", not ES.ownership_problems(E.KEYS), str(ES.ownership_problems(E.KEYS)))
ctl = E.TwoPhaseController("fake", "t", dict(E.DEFAULTS))
r, eng = ctl.r, ctl.r.eng
stop = threading.Event()
threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()


def idle(t=30):
    t0 = time.time()
    while time.time() - t0 < t and (not ctl.cmds.empty() or ctl.mode == "busy"):
        time.sleep(0.05)
    time.sleep(0.4)


def apply(**kw):
    new = {k: ctl.s[k] for k in E.KEYS}
    new.update(kw)
    ctl.submit("apply", (new, ctl.version))
    idle()


def win(names, dur=0.033):
    return eng.traces(names, dur, "immediately")[0]


time.sleep(2.0)
check("ramp mode: ramp + dither on OUT2, correction off", ctl.mode == "acquire" and r.asg0.output_direct == "out2"
      and r.asg0.amplitude > 0 and r.iq2.output_direct == "out2" and r.pid0.output_direct == "off")
check("error output: pid1 = iq2 (X) pass-through on OUT1 (i = 0)", r.pid1.output_direct == "out1" and r.pid1.input == "iq2"
      and r.pid1.i == 0)
check("pid2 off; nothing routed to OUT1 except the error", r.pid2.output_direct == "off"
      and r.asg0.output_direct != "out1" and r.iq2.output_direct != "out1")
check("peaks found while ramping OUT2", bool((ctl.analysis or {}).get("peaks")))
check("iq0 off (the lock-in is iq2), iq2 low-pass takes 4 stages", r.iq0.output_direct == "off")
apply(lpf_order=4)
check("low-pass order 4 -> iq2.bandwidth has 4 stages", len([b for b in r.iq2.bandwidth if b > 0]) == 4
      and ctl.s["lpf_order"] == 4, str(r.iq2.bandwidth))
apply(lpf_order=2)
for ph in (70.0, -100.0):
    apply(phase_deg=ph)
    time.sleep(1.0)
    an = ctl.analysis or {}
    true = ((ph - F.PZT_LAG_DEG) + 90) % 180 - 90       # fake: X ~ cos(phase - lag), Y ~ sin(phase - lag)
    th = an.get("theta")
    check(f"theta measured from one sweep at phase {ph:+.0f}", th is not None and an.get("theta_q", 0) > 10
          and abs(((th - true) + 90) % 180 - 90) < 3, f"theta {th}, true {true:.1f}, q {an.get('theta_q', 0):.0f}")
    t0 = time.time()
    ctl.submit("autophase"); idle(60)
    an = ctl.analysis or {}
    check(f"auto phase from {ph:+.0f}: theta -> 0, negative slope", abs(an.get("theta") or 99) < 3
          and (an.get("slope") or 1) < 0, f"theta {an.get('theta')}, slope {an.get('slope')}, phase "
          f"{ctl.s['phase_deg']}, {time.time()-t0:.1f} s")
check("rotation direction of iq2.phase found once", ctl._phase_sign in (1, -1), str(ctl._phase_sign))
time.sleep(1.0)
tr = ctl.traces
check("sweep frame carries Y (iq2_2) and X", tr is not None and "y" in tr and len(tr["y"]) == len(tr["err"]))
an = ctl.analysis or {}
check("auto phase: error zero crossing at the target", an.get("v0") is not None, str(an.get("notes"))[:120])
ctl.submit("park"); idle()
v0, S = an["v0"], ctl.slope
check("Park at peak -> servo mode: DC offset on the crossing, IN2 correction on (pid0 in2 -> out2, i = 0)",
      ctl.mode == "servo" and r.asg0.amplitude == 0 and abs(r.asg0.offset - v0) < 0.002
      and abs(r.asg0.offset - ctl.s["ramp_center_mv"] * 1e-3) < 1e-9 and r.pid0.output_direct == "out2"
      and r.pid0.input == "in2" and r.pid0.i == 0, f"{r.asg0.offset} vs {v0}")
check("error slope reported", S is not None and S < 0, f"{S}")
time.sleep(0.6)
w = win(["iq2", "iq2_2"])
check("phased: Y (iq2_2) small next to X at the lock point", np.std(w[1]) < 3 * F.NOISE_ERR + 1e-4,
      f"std X {np.std(w[0])*1e3:.2f} mV, Y {np.std(w[1])*1e3:.2f} mV")
check("live theta published in servo mode", ctl.snapshot()["xy"].get("src") == "live", str(ctl.snapshot()["xy"]))
time.sleep(0.6)
w = win(["out1", "iq2", "out2", "in1"])
check("OUT1 = gain x error + offset", np.allclose(w[0], np.clip(w[1], -1, 1), atol=1e-6))
check("OUT2 = DC + dither (correction 0 V)", abs(np.mean(w[2]) - r.asg0.offset) < 0.003 and np.ptp(w[2]) < 0.05)
check("on resonance: transmission up", np.mean(w[3]) > 0.5 * an["peaks"][an["target"]]["height"], f"{np.mean(w[3]):.3f}")
eng.ext_v = 0.05                                       # the servo box puts out a correction
time.sleep(0.5)
w = win(["out2", "in2"])
check("OUT2 sums the IN2 correction (gain 1)", abs(np.mean(w[0]) - (r.asg0.offset + 0.05)) < 0.003
      and abs(np.mean(w[1]) - 0.05) < 0.002, f"{np.mean(w[0]):.4f}")
apply(in2_gain=-0.5)
time.sleep(0.4)
w = win(["out2"])
check("correction gain -0.5 -> pid0.p, OUT2 += -0.5 x IN2", r.pid0.p == -0.5
      and abs(np.mean(w[0]) - (r.asg0.offset - 0.025)) < 0.003, f"{np.mean(w[0]):.4f}")
apply(in2_gain=1.0)
eng.ext_v = 0.0
time.sleep(0.4)
print("--- end to end through the simulated servo box (integrator OUT1 -> IN2)")
eng.ext_i = 10.0 / abs(S)                              # 10 Hz loop: feedback sign for S < 0, gains +1
eng.ext_on = True
time.sleep(1.0)
eng.vc += 0.004                                        # the cavity drifts by 0.7 linewidths
time.sleep(1.5)
w = win(["in1", "in2"])
pk = an["peaks"][an["target"]]["height"]
check("the external servo tracks a drift through IN2 -> OUT2", abs(np.mean(w[1]) - 0.004) < 0.0015
      and np.mean(w[0]) > 0.6 * pk, f"IN2 {np.mean(w[1])*1e3:+.2f} mV, transmission {np.mean(w[0]):.3f} of {pk:.3f}")
check("the Red Pitaya itself did not move: DC offset unchanged", abs(r.asg0.offset - ctl.s["ramp_center_mv"] * 1e-3) < 1e-9)
eng.ext_on = False
iv = r.asg0.offset
ctl.submit("ramp_nudge", 1.0); idle()
check("servo mode: setpoint step moves the OUT2 DC offset", abs(r.asg0.offset - (iv + 0.001)) < 1e-9)
apply(eo_gain=-4.0, eo_offset_mv=50.0, eo_limit_mv=400.0)
w = win(["out1", "iq2"])
check("error output gain -4, offset +50 mV, clamp 400 mV", np.allclose(w[0], np.clip(-4 * w[1] + 0.05, -0.4, 0.4),
                                                                       atol=1e-6))
apply(eo_source="Y")
w = win(["out1", "iq2_2"])
check("error output source Y -> pid1.input iq2_2, OUT1 = -4 x Y + 50 mV", r.pid1.input == "iq2_2"
      and np.allclose(w[0], np.clip(-4 * w[1] + 0.05, -0.4, 0.4), atol=1e-6))
apply(eo_source="X")
check("error output source X -> pid1.input iq2", r.pid1.input == "iq2")
ctl.submit("switch", ("errout", False)); idle(); time.sleep(0.3)
check("Error out off: OUT1 = 0 V", r.pid1.output_direct == "off" and np.allclose(win(["out1"])[0], 0))
ctl.submit("switch", ("errout", True)); idle()
ctl.submit("switch", ("ramp", True)); idle(); time.sleep(1)
check("Ramp toggle on: OUT2 = ramp + dither, IN2 correction off", ctl.mode == "acquire" and r.asg0.amplitude > 0
      and r.pid0.output_direct == "off" and abs(r.asg0.offset - (iv + 0.001)) < 0.002)
ctl.submit("switch", ("ramp", False)); idle()
check("Ramp toggle off: servo mode at the setpoint", ctl.mode == "servo" and r.pid0.output_direct == "out2"
      and abs(r.asg0.offset - (iv + 0.001)) < 1e-9)
ctl.submit("switch", ("output", False)); idle()
check("Output off: OUT2 = 0 V (ramp/DC, correction, dither off)", ctl.mode == "off" and r.asg0.output_direct == "off"
      and r.pid0.output_direct == "off" and r.iq2.output_direct == "off")
check("xy_theta: 30 deg axis", abs(E.xy_theta(np.cos(np.radians(30)) * np.arange(-5, 6.),
                                               np.sin(np.radians(30)) * np.arange(-5, 6.))[0] - 30) < 1e-9)
check("diagram state builds in every mode", bool(ES.diagram_state(ctl.snapshot())["boxes"]["corr"]))
mine = [w for w in F.WRITES if w[4] in ("errsig2ph_app.py", "errsig2ph_schematic.py")]
bad = [w for w in mine if (w[1] in ("pid0", "pid1") and w[2] == "i" and w[3] != 0)
       or (w[1] == "pid2" and w[2] == "output_direct" and w[3] != "off")
       or (w[2] == "output_direct" and w[1] in ("asg0", "iq2", "pid0") and w[3] in ("out1", "both"))
       or (w[1] == "pid1" and w[2] == "output_direct" and w[3] not in ("out1", "off"))]
check("never feedback (pid0/pid1 i = 0, pid2 off); only the error goes to OUT1", not bad, str(bad[:3]))
stop.set()
print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL PASS")
