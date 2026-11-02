import sys, threading, time
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import cavity_lock_app as A
import schematic as SCH
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


# ---- the model: every FIELDS key has exactly one home; every block parameter names a FIELDS key ----
print("--- schematic model")
check("every FIELDS key belongs to exactly one block / home", not SCH.ownership_problems(A.KEYS),
      "; ".join(SCH.ownership_problems(A.KEYS)))
HW_KEYS = [p["key"] for b in SCH.BLOCKS for p in b["params"] if p["attr"]]
check("hardware-affecting keys are owned by exactly one block",
      all(sum(p["key"] == k for b in SCH.BLOCKS for p in b["params"]) == 1 for k in HW_KEYS))
check("plant parameters write no register",
      all(p["attr"] is None for b in SCH.BLOCKS if b["kind"] == "plant" for p in b["params"]))

ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
eng = ctl.r.eng
stop = threading.Event()
th = threading.Thread(target=ctl.run, args=(stop,), daemon=True); th.start()

def wait_mode(m, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ctl.mode == m and ctl.cmds.empty(): return True
        time.sleep(0.1)
    return False

def show(tag):
    an = ctl.analysis
    print(f"--- {tag}: mode {ctl.mode}, true TEM00 at {eng.vc*1e3:+.2f} mV, phase {ctl.s['phase_deg']}")
    for p in an.get("peaks", []):
        print(f"   peak {p['v']*1e3:+8.2f} mV h={p['height']:.3f} fwhm={p['fwhm']*1e3:.2f} t={['%.1f'%x for x in p['t_ms']]}")
    print("   target", an.get("target"), "v0", None if an.get("v0") is None else round(an["v0"]*1e3, 3), "slope", an.get("slope"))
    for n in an.get("notes", []): print("   ", n)

time.sleep(2.5); show("startup acquisition")
print("dither measured", ctl.dither_meas, "amp_cal", ctl.amp_cal)
# pick the +120 mV mode by clicking at its time
p2 = [p for p in ctl.analysis["peaks"] if abs(p["v"] - (eng.vc + 0.120)) < 0.01][0]
ctl.submit("target", p2["t_ms"][0]); time.sleep(1.0); show("after clicking the +120 mV peak")
ctl.submit("tallest"); time.sleep(1.0)
ctl.submit("autophase"); time.sleep(0.5); wait_mode("acquire"); time.sleep(0.6); show("after auto phase")
print("kappa", ctl.kappa)
# different lock dither: 4 mVpp at 2 kHz
new = {k: ctl.s[k] for k in A.KEYS}; new.update(lock_same_dither=False, lock_dither_mvpp=4.0, lock_dither_hz=2000.0)
ctl.submit("apply", (new, ctl.version)); time.sleep(0.5)
ctl.submit("lock"); time.sleep(0.5); ok = wait_mode("locked", 60)
time.sleep(2.0)
print("LOCKED?", ok, "setpoint", round(ctl.r.pid0.ival*1e3, 3), "true", round(eng.vc*1e3, 3), "lock_info",
      {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ctl.lock_info.items() if k != "clamp"})
print("dither now", ctl._dither(), "measured", ctl.dither_meas)
# diagnostics
new = {k: ctl.s[k] for k in A.KEYS}; new.update(noise_avg=2)
ctl.submit("apply", (new, ctl.version)); time.sleep(0.5)
ctl.submit("noise"); time.sleep(0.5); wait_mode("locked", 60); time.sleep(0.3)
nz = ctl.noise
print("NOISE rows:", [(r["band"], None if r["det"] is None else round(r["det"], 5), round(r["rin"], 5)) for r in nz["rows"]], "src", nz["kappa_src"])
ctl.submit("loop"); time.sleep(0.5); wait_mode("locked", 60); time.sleep(0.3)
lp = ctl.loop
print("LOOP ugf", lp["ugf"], "pm", lp["pm"], "points", len(lp["f"]), "model |G| at first f", lp["M_mag"][0], "meas", lp["G_mag"][0])
# kick the cavity by +25 mV
print("== kick +25 mV"); eng.vc += 0.025
time.sleep(1.0); ok = wait_mode("locked", 60); time.sleep(1.5)
print("relocked?", ok, "setpoint", round(ctl.r.pid0.ival*1e3, 3), "true", round(eng.vc*1e3, 3))
ctl.submit("unlock"); time.sleep(0.8); print("mode", ctl.mode)
ctl.submit("lock"); time.sleep(0.5); ok = wait_mode("locked", 60); time.sleep(1)
print("lock from held?", ok, "setpoint", round(ctl.r.pid0.ival*1e3, 3), "true", round(eng.vc*1e3, 3))
ctl.submit("acquire"); time.sleep(2.0); show("back to acquisition")
# sawtooth
new = {k: ctl.s[k] for k in A.KEYS}; new.update(ramp_shape="sawtooth")
ctl.submit("apply", (new, ctl.version)); time.sleep(2.0); show("sawtooth")

# ---------------------------------------------------------------------------------------------
# Schematic: every block parameter changes the register drawn on its block; switches; probes; Lock Assist
# ---------------------------------------------------------------------------------------------
def reg(attr):
    mod, a = attr.split(".")
    return getattr(getattr(ctl.r, mod), a)


def apply(**kw):
    new = {k: ctl.s[k] for k in A.KEYS}
    new.update(kw)
    ctl.submit("apply", (new, ctl.version))
    time.sleep(0.4)
    wait_idle()


def wait_idle(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout and (not ctl.cmds.empty() or ctl.mode in SCH.BUSY):
        time.sleep(0.05)
    time.sleep(0.15)


NEW = dict(ramp_hz=13.0, ramp_mvpp=300.0, ramp_shape="triangle", ramp_center_mv=30.0, acq_dither_hz=1700.0,
           acq_dither_mvpp=20.0, phase_deg=80.0, ac_hz=300.0, lpf_hz=600.0, lpf_order=1, err_gain=3,
           setpoint_mv=2.0, out2_signal="photodiode", out2_gain=2.0,
           ugf_hz=15.0, p_gain=0.3, clamp_auto=False, clamp_mv=30.0,
           lock_same_dither=False, lock_dither_hz=2500.0, lock_dither_mvpp=6.0)
params = [(b["id"], p) for b in SCH.BLOCKS for p in b["params"] if p["attr"]]
check("the register test covers every block parameter", all(p["key"] in NEW for _, p in params),
      str([p["key"] for _, p in params if p["key"] not in NEW]))


def try_param(bid, p):
    k = p["key"]
    if k == "ramp_shape":                            # the run above ended on sawtooth
        apply(ramp_shape="sawtooth")
    pre = {"clamp_mv": {"clamp_auto": False}}.get(k, {})   # clamp_mv is live only with the auto clamp off
    if pre:
        apply(**pre)
    old_val, before = ctl.s[k], reg(p["attr"])
    apply(**{k: NEW[k]})
    after = reg(p["attr"])
    changed = (before != after) if not isinstance(after, float) else abs(after - before) > 1e-12
    hw_ok = ctl.hw.get(p["attr"]) == (list(after) if isinstance(after, list) else after) or \
        (isinstance(after, float) and abs(ctl.hw.get(p["attr"], 1e9) - after) < 1e-9)
    check(f"{bid}: {k} {old_val} -> {NEW[k]} changes {p['attr']}", changed and hw_ok, f"{before} -> {after}")
    apply(**{k: old_val})
    if pre:
        apply(**{pk: not pv if isinstance(pv, bool) else pv for pk, pv in pre.items()})


print("--- block parameters in acquisition (Scan on)")
ctl.submit("switch", ("scan", True)); wait_idle()
for bid, p in params:
    if p["live"] in ("always", "acquire"):
        try_param(bid, p)
apply(ramp_shape="triangle", lock_same_dither=True)
ctl.submit("tallest"); time.sleep(0.8)

print("--- Lock switch")
ctl.submit("switch", ("dither", False)); wait_idle()
check("Dither off: iq0.output_direct off, switch shows off", ctl.r.iq0.output_direct == "off"
      and not ctl.switches()["dither"])
ctl.submit("switch", ("lock", True)); time.sleep(0.5); ok = wait_mode("locked", 60)
sw = ctl.switches()
check("Lock on: locks, turns Dither on and Scan off", ok and sw["dither"] and not sw["scan"] and sw["lock"], str(sw))
check("locked: pid0 drives OUT1 with i != 0", ctl.r.pid0.output_direct == "out1" and ctl.r.pid0.i != 0)
print("--- block parameters while locked")
for bid, p in params:
    if p["live"] == "locked":
        try_param(bid, p)
apply(lock_dither_hz=2400.0, lock_dither_mvpp=5.0)
f0 = ctl.r.iq0.frequency
apply(lock_same_dither=False)
check("dither: lock_same_dither off writes the lock dither in the lock context", ctl.r.iq0.frequency != f0
      and abs(ctl.r.iq0.frequency - 2400) < 1e-6, f"{f0} -> {ctl.r.iq0.frequency}")
for k in ("lock_dither_hz", "lock_dither_mvpp"):
    p = next(p for _, p in params if p["key"] == k)
    try_param("dither", p)
apply(lock_same_dither=True)
ctl.submit("lock"); time.sleep(0.5); wait_mode("locked", 60)

print("--- switches and interlocks")
ctl.submit("switch", ("dither", False)); wait_idle()
check("Dither off while locked: unlocks and holds first", ctl.mode == "held" and ctl.r.pid0.i == 0
      and ctl.r.iq0.output_direct == "off")
ctl.submit("switch", ("lock", True)); time.sleep(0.5); ok = wait_mode("locked", 60)
check("Lock on from held relocks (dither back on)", ok and ctl.r.iq0.output_direct == "out1")
ctl.submit("switch", ("lock", False)); wait_idle()
check("Lock off: held, i = 0, setpoint kept", ctl.mode == "held" and ctl.r.pid0.i == 0
      and ctl.r.pid0.output_direct == "out1")
ctl.submit("switch", ("lock", True)); time.sleep(0.5); wait_mode("locked", 60)
ctl.submit("switch", ("scan", True)); wait_idle()
check("Scan on while locked (after the UI confirm): unlocks and scans", ctl.mode == "acquire"
      and ctl.r.asg0.output_direct == "out1" and ctl.r.pid0.output_direct == "off")
time.sleep(1.0)
ctl.submit("switch", ("scan", False)); wait_idle()
check("Scan off: ramp off, OUT1 held at the ramp centre", ctl.mode == "held" and ctl.r.asg0.output_direct == "off"
      and abs(ctl.r.pid0.ival - (ctl.ramp["lo"] + ctl.ramp["hi"]) / 2) < 1e-6)
ctl.submit("switch", ("output", False)); wait_idle()
check("Output off: mode off, nothing drives OUT1", ctl.mode == "off" and ctl.r.asg0.output_direct == "off"
      and ctl.r.iq0.output_direct == "off" and ctl.r.pid0.output_direct == "off" and not ctl.switches()["output"])
ctl.submit("switch", ("lock", True)); wait_idle()
check("Lock on refused while Output is off", ctl.mode == "off")
ctl.submit("switch", ("scan", True)); wait_idle()
check("Scan on refused while Output is off", ctl.mode == "off")
time.sleep(0.5)
check("live traces continue with Output off", ctl.traces is not None and ctl.traces["kind"] == "time")
ctl.submit("switch", ("output", True)); wait_idle()
check("Output on: scanning again with dither", ctl.mode == "acquire" and ctl.switches()["scan"]
      and ctl.switches()["dither"])
r0 = ctl.s["auto_relock"]
ctl.submit("switch", ("relock", not r0)); wait_idle()
check("Auto-relock switch toggles the setting", ctl.s["auto_relock"] == (not r0) and ctl.switches()["relock"] == (not r0))
ctl.submit("switch", ("relock", r0)); wait_idle()

print("--- probes")
apply(probe_a="out1", probe_b="asg0")
ctl._probe_last = "b"
for sig, want in (("iq0", ("iq0", "asg0")), ("asg0", ("iq0", "off")), ("pid0", ("iq0", "pid0")),
                  ("out2", ("out2", "pid0")), ("in1", ("out2", "in1")), ("out2", ("off", "in1"))):
    ctl.submit("probe", sig); wait_idle()
    got = (ctl.s["probe_a"], ctl.s["probe_b"])
    check(f"probe click {sig}: A/B = {want}", got == want, str(got))
ctl.submit("probe", "hp"); wait_idle()
check("a grey probe point is refused", (ctl.s["probe_a"], ctl.s["probe_b"]) == ("off", "in1"))
apply(probe_a="out2", probe_b="pid0", drive_every=1)
time.sleep(1.5)
d = ctl.drive or {}
check("probe capture of out2 / pid0 reaches the scope panel", d.get("a_name") == "out2" and d.get("b") is not None
      and d.get("a") is not None and abs(np.mean(d["a"]) - np.mean(d["a"])) == 0, str(d.get("pair")))
apply(probe_a="out1", probe_b="asg0", drive_every=3)

print("--- Bode plot (swept sine, asg1 + scope)")
apply(bode_mode="closed loop", bode_points=8, bode_start_hz=4.0, bode_stop_hz=300.0, bode_cycles=6)
ctl.submit("bode"); wait_idle()
check("closed-loop Bode refused while not locked", ctl.mode == "acquire" and ctl.r.asg1.output_direct == "off")
outs = []
_setup = ctl.r.asg1.setup
ctl.r.asg1.setup = lambda **kw: (outs.append(kw.get("output_direct")), _setup(**kw))[1]
apply(bode_mode="circuit only")
ctl.submit("bode"); time.sleep(0.5); wait_idle(120)
B = ctl.bode
check("circuit only: every point measured, mode restored", B and B["done"] and B["n"] == 8 and ctl.mode == "acquire",
      f"n={B and B['n']}")
check("circuit only: asg1 never routed to an output (OUT1 untouched)", outs and set(outs) == {"off"}, str(set(outs)))
for cv in B["curves"]:
    r_ = np.abs(np.array(cv["c"]) / np.array(cv["model"][:len(cv["c"])]))
    ph = np.degrees(np.angle(np.array(cv["c"]) / np.array(cv["model"][:len(cv["c"])])))
    check(f"circuit only: '{cv['name'][:28]}' matches its model (|ratio-1| < 5 %, < 5 deg)",
          np.all(np.abs(r_ - 1) < 0.05) and np.all(np.abs(ph) < 5), f"{np.round(r_, 3)}")
check("circuit only: electronics UGF = loop bandwidth setting", B["ugf"] and abs(B["ugf"] - ctl.s["ugf_hz"]) < 0.1 *
      ctl.s["ugf_hz"], f"{B['ugf']}")
ctl.r.asg1.setup = _setup
ctl.submit("lock"); time.sleep(0.5); ok = wait_mode("locked", 60)
apply(bode_mode="closed loop", na_amp_frac=0.1)   # low frequencies: G comes from 1 + c/x, so excite enough
ctl.submit("bode"); time.sleep(0.5); wait_idle(180)
B = ctl.bode
f_ = np.array(B["curves"][0]["f"]); G_ = np.array(B["curves"][0]["c"]); S_ = np.array(B["curves"][1]["c"])
eng_S = float((eng.err_raw(eng.vc + 1e-5) - eng.err_raw(eng.vc - 1e-5))[0] / 2e-5)
Gt = -(ctl.r.pid0.i / (1j * f_)) * eng_S
for bw_ in [b for b in ctl.r.iq0.bandwidth if b > 0]:
    Gt = Gt / (1 + 1j * f_ / bw_)
lowf = f_ < 100
check("closed loop: every point, still locked, asg1 off afterwards", ok and B["n"] == 8 and ctl.mode == "locked"
      and ctl.r.asg1.output_direct == "off")
check("closed loop: measured G = the fake's true loop gain below 100 Hz (5 %)",
      np.all(np.abs(np.abs(G_[lowf] / Gt[lowf]) - 1) < 0.05), f"{np.round(np.abs(G_ / Gt), 3)}")
check("closed loop: disturbance suppressed at low frequency (|S| < 0.5 at 4 Hz)", abs(S_[0]) < 0.5, f"{abs(S_[0]):.2f}")
check("closed loop: UGF and phase margin found", B["ugf"] and 5 < B["ugf"] < 15 and B["pm"] > 45,
      f"UGF {B['ugf']}, PM {B['pm']}")
apply(bode_points=30, bode_start_hz=2.0, na_amp_frac=0.03)
ctl.submit("bode"); time.sleep(3.0)
ctl.stop_measurement(); wait_idle(60)
check("Stop ends the sweep early and the lock stays", ctl.bode["done"] and ctl.bode["n"] < 30 and ctl.mode == "locked"
      and ctl.r.asg1.output_direct == "off", f"n={ctl.bode['n']}")
ctl.submit("switch", ("scan", True)); wait_idle(); time.sleep(1.5)

print("--- Lock Assist crosshairs")
time.sleep(1.0)
an = ctl.analysis
zc = [z for z in an.get("zc", []) if z["on_peak"]]
check("error zero crossings found on the peaks", len(zc) >= 2, f"{len(zc)} on peaks, {len(an.get('zc', []))} total")
other = [z for z in zc if abs(z["peak_v"] - an["peaks"][an["target"]]["v"]) > 0.01]
if other:
    ctl.submit("target_v", other[0]["v"]); time.sleep(1.0)
    check("clicking a crosshair targets that peak", abs(ctl.s["target_mv"] - other[0]["peak_v"] * 1e3) < 5,
          f"{ctl.s['target_mv']:.1f} mV vs {other[0]['peak_v']*1e3:.1f}")
else:
    check("a crosshair on another peak", False)
ctl.submit("tallest"); time.sleep(0.5)

print("--- diagram readback")
snap = ctl.snapshot()
dg = SCH.diagram_state(snap, A.predicted_margin(ctl.s, ctl._lp_list()))
print("   lowpass:", dg["boxes"]["lowpass"][0][0], "| bandpass:", dg["boxes"]["bandpass"][0][0], "| amp:",
      dg["boxes"]["amp"][0][0])
check("readback shows the snapped low-pass corner", "303.6" in dg["boxes"]["lowpass"][0][0])
apply(lpf_hz=250.0)
dg = SCH.diagram_state(ctl.snapshot())
check("requested -> hardware shown ('250 -> 303.6')", "250 → 303.6" in dg["boxes"]["lowpass"][0][0],
      dg["boxes"]["lowpass"][0][0])

print("\nLOG:"); print("\n".join(list(ctl.logs)[-60:]))
stop.set()
print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL CHECKS PASSED")
sys.exit(1 if FAILS else 0)
