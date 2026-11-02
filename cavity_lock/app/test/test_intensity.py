"""Controller test of the AOM intensity servo (pid2 / asg1 on IN2 / OUT2) against the fake.
Run from app/test:  python test_intensity.py"""
import json
import math
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import cavity_lock_app as A
import intensity as INT
import intensity_schematic as ISCH
import pyrpl as F

FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


print("--- intensity model")
check("every intensity FIELDS key has exactly one home", not ISCH.ownership_problems(INT.I_KEYS),
      "; ".join(ISCH.ownership_problems(INT.I_KEYS)))
regs = {p["attr"].split(".")[0] for b in ISCH.BLOCKS for p in b["params"] if p["attr"]}
check("intensity block parameters write only pid2 / asg1", regs <= {"pid2", "asg1"}, str(regs))
check("plant parameters write no register",
      all(p["attr"] is None for b in ISCH.BLOCKS if b["kind"] == "plant" for p in b["params"]))
check("ladders stay inside the register ranges", max(ISCH.P_LADDER) <= ISCH.P_MAX and max(ISCH.I_LADDER) <= ISCH.I_MAX
      and abs(ISCH.I_MAX - 38.9e3) < 100 and abs(ISCH.P_MAX - 2048) < 1)

ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
eng, it, r = ctl.r.eng, ctl.ints, ctl.r
aom = eng.aom
stop = threading.Event()
threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()
ctl.view = "intensity"


def wait_idle(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout and (not ctl.cmds.empty() or it.mode in ISCH.BUSY):
        time.sleep(0.05)
    time.sleep(0.2)


def wait_for(cond, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def apply(**kw):
    new = {k: ctl.s[k] for k in A.KEYS}
    new.update(kw)
    ctl.submit("apply", (new, ctl.version))
    time.sleep(0.3)
    wait_idle()


def window(dur=0.033):
    """A scope-like window straight from the fake plant (test thread; no controller scope access)."""
    n = 2 ** 14
    return aom.synth(time.time(), np.arange(n) * dur / n)


def line_amp(y, dur, f0):
    t = np.arange(len(y)) * dur / len(y)
    return abs(A.fit_sine(t, y, f0))


print("--- start-up: OUT2 = 0 V (bias level), output off")
time.sleep(1.0)
check("start-up mode off, pid2 output off, asg1 off", it.mode == "off" and r.pid2.output_direct == "off"
      and r.asg1.output_direct == "off" and r.pid2.ival == 0)
w = window()
check("OUT2 = 0 V at start-up", abs(np.mean(w["out2"])) < 1e-9, f"{np.mean(w['out2']):.2e}")

print("--- power scan")
apply(int_setpoint_mv=500.0)
ctl.submit("int_switch", ("output", True)); ctl.submit("int_switch", ("scan", True)); wait_idle()
check("Scan on: asg1 triangle on out2, loop open", it.mode == "scanning" and r.asg1.output_direct == "out2"
      and r.asg1.waveform == "ramp" and r.pid2.i == 0 and r.pid2.p == 0)
time.sleep(2.5)
ctl.submit("int_switch", ("scan", False)); wait_idle()
sc = it.scan or {}
true_pk = float(aom.ug[np.argmax(aom.eg * (np.abs(aom.ug) <= 0.2))])
check("scan finds the efficiency peak (fake: OUT2 -63 mV)", sc.get("u_peak") is not None
      and abs(sc["u_peak"] - true_pk) < 0.01, f"{sc.get('u_peak')} vs {true_pk:.4f}")
lo, hi = sc.get("clamp", (0, 0))
bright = lo if sc.get("bright_dir", 0) < 0 else hi
check("the clamp lands below the peak (on the near side)", sc.get("bright_dir") == -1 and bright > sc["u_peak"],
      f"bright clamp {bright*1e3:+.1f} mV, peak {sc['u_peak']*1e3:+.1f} mV")
pd_at = float(aom.pd(bright))
check("light at the clamp = (1 - margin) x peak", abs(pd_at / sc["peak"] - (1 - ctl.s["int_clamp_margin"])) < 0.03,
      f"{pd_at/sc['peak']:.3f}")
K_true = aom.slope(sc["u_sp"])
check("K measured with the right sign (negative: inverting driver)", sc.get("K", 0) < 0, f"K {sc.get('K')}")
check("K matches the fake's true slope at the setpoint (10 %)", abs(sc["K"] / K_true - 1) < 0.1,
      f"{sc['K']:.2f} vs {K_true:.2f}")
check("the scan is saved for a restart", os.path.exists(INT.SCAN_FILE))

print("--- lock")
apply(int_gain_mode="bandwidth", int_ugf_khz=30.0, int_corner_khz=1000.0)
ctl.submit("int_switch", ("lock", True)); wait_idle()
time.sleep(0.5)
check("locked", it.mode == "locked", it.mode)
check("asg1 off and pid2 gains positive (i K < 0 with K < 0)", r.asg1.output_direct == "off" and r.pid2.i > 0
      and r.pid2.p > 0, f"p {r.pid2.p}, i {r.pid2.i}")
check("|i K| = the requested bandwidth", abs(abs(r.pid2.i * it.lock_info["K"]) - 30e3) < 0.02 * 30e3,
      f"{abs(r.pid2.i*it.lock_info['K']):.0f} Hz")
check("p = i / f_corner (quantized)", abs(r.pid2.p - r.pid2.i / 1e6) <= F.P_STEP, f"{r.pid2.p} vs {r.pid2.i/1e6}")
w = window()
check("photodiode at the setpoint (1 %)", abs(np.mean(w["in2"]) / 0.5 - 1) < 0.01, f"{np.mean(w['in2']):.4f} V")
dur = 1.07                                   # 1 s windows: the line is resolved below the HF noise of the bump
closed = np.mean([line_amp(window(dur)["in2"], dur, 120.0) for _ in range(3)])
ctl.submit("int_switch", ("lock", False)); wait_idle()
check("Lock off: held (loop open, same drive)", it.mode == "held" and r.pid2.i == 0)
openl = np.mean([line_amp(window(dur)["in2"], dur, 120.0) for _ in range(3)])
check("the loop suppresses the 120 Hz intensity line > 30x", openl / max(closed, 1e-12) > 30,
      f"open {openl*1e6:.1f} uV, closed {closed*1e6:.2f} uV, x{openl/max(closed, 1e-12):.0f}")
from scipy.signal import welch


def band_rms(dur, lo, hi, n=3):
    tot = 0.0
    for _ in range(n):
        y = window(dur)["in2"]
        f, P = welch(y, fs=len(y) / dur, nperseg=4096)
        m = (f >= lo) & (f < hi)
        tot += np.sum(P[m]) * (f[1] - f[0])
    return math.sqrt(tot / n)


b_open = band_rms(0.033, 100, 3000)
ctl.submit("int_switch", ("lock", True)); wait_idle(); time.sleep(0.3)
b_closed = band_rms(0.033, 100, 3000)
check("intensity noise 100 Hz - 3 kHz suppressed > 10x (in-loop)", b_open / b_closed > 10,
      f"open {b_open*1e6:.2f} uV, closed {b_closed*1e6:.3f} uV rms, x{b_open/b_closed:.0f}")

print("--- step disturbance")
d0 = it.drive
aom.disturb = 0.85
time.sleep(0.6)
check("recovers after a -15 % step in the light", it.mode == "locked" and abs(it.pd / 0.5 - 1) < 0.01
      and it.drive < d0, f"pd {it.pd:.4f}, drive {d0*1e3:+.2f} -> {it.drive*1e3:+.2f} mV")
aom.disturb = 1.0
time.sleep(0.4)

print("--- beam blocked, then back")
good = it.drive
aom.blocked = True
ok = wait_for(lambda: it.mode == "nolight", 3)
time.sleep(0.2)
lo, hi = it.lock_info["clamp"]
check("blocked beam -> 'no light' hold", ok and r.pid2.i == 0 and r.pid2.p == 0, it.mode)
check("held at the last good drive, not at the clamp", abs(r.pid2.ival - good) < 0.003 and lo + 0.005 < r.pid2.ival
      < hi - 0.005, f"{r.pid2.ival*1e3:+.2f} mV (good {good*1e3:+.2f})")
aom.blocked = False
ok = wait_for(lambda: it.mode == "locked", 4)
time.sleep(0.3)
check("light back -> re-engaged", ok and abs(it.pd / 0.5 - 1) < 0.01, f"{it.mode}, pd {it.pd}")
apply(int_auto_reengage=False)
aom.blocked = True
wait_for(lambda: it.mode == "nolight", 3)
aom.blocked = False
time.sleep(1.0)
check("Re-engage off: stays held after the light returns", it.mode == "nolight")
apply(int_auto_reengage=True)
wait_for(lambda: it.mode == "locked", 4)

print("--- setpoint above the achievable light -> railed")
ctl.submit("int_setpoint", (sc["max_lock"] + 0.04) * 1e3); wait_idle()
ok = wait_for(lambda: it.mode == "railed", 3)
check("railed fault", ok, it.mode)
check("railed: held at the last good drive, gains 0", r.pid2.i == 0 and lo + 0.002 < r.pid2.ival < hi - 0.002,
      f"{r.pid2.ival*1e3:+.2f} mV")
check("railed is logged", any("RAILED" in l for l in ctl.logs))
ctl.submit("int_setpoint", 500.0); ctl.submit("int_switch", ("lock", True)); wait_idle()
check("lock again after a fault", it.mode == "locked")

print("--- setpoint adjuster and manual gains")
ctl.submit("int_nudge", 10.0); wait_idle()
check("nudge +10 mV writes pid2.setpoint live", abs(ctl.s["int_setpoint_mv"] - 510) < 1e-9
      and abs(r.pid2.setpoint - 0.51) < 1e-9)
time.sleep(0.4)
check("still locked at the new setpoint", it.mode == "locked" and abs(it.pd / 0.51 - 1) < 0.01, f"{it.pd}")
check("gains follow K at the new setpoint (same bandwidth)",
      abs(abs(r.pid2.i * it.lock_info["K"]) - 30e3) < 0.03 * 30e3)
ctl.submit("int_setpoint", 500.0); wait_idle()
p_now, i_now = r.pid2.p, r.pid2.i
apply(int_gain_mode="manual")
check("manual mode starts at the ladder values nearest the present gains",
      ctl.s["int_p"] == INT.nearest(ISCH.P_LADDER, p_now) and ctl.s["int_i_hz"] == INT.nearest(ISCH.I_LADDER, i_now),
      f"P {p_now:.4g} -> {ctl.s['int_p']}, I {i_now:.4g} -> {ctl.s['int_i_hz']}")
apply(int_p=0.002, int_i_hz=2000.0)
check("manual P / I dropdowns -> pid2 with the feedback sign", abs(r.pid2.p - 0.002) <= F.P_STEP
      and abs(r.pid2.i - 2000) < 0.01 and it.mode == "locked", f"p {r.pid2.p}, i {r.pid2.i}")
apply(int_gain_mode="bandwidth", int_ugf_khz=1000.0)
check("bandwidth above the integrator cap is warned", any("capped" in l for l in list(ctl.logs)[-5:]))
apply(int_ugf_khz=30.0)

print("--- every intensity block parameter changes its register")
NEW = dict(int_filter_hz=200e3, int_filter_stages=2, int_setpoint_mv=480.0, int_gain_mode="manual",
           int_ugf_khz=20.0, int_p=0.01, int_i_hz=1000.0, int_corner_khz=500.0, int_clamp_auto=False,
           int_clamp_margin=0.1, int_clamp_lo_mv=-30.0, int_clamp_hi_mv=120.0,
           int_scan_lo_mv=-150.0, int_scan_hi_mv=150.0, int_scan_hz=80.0)
params = [(b["id"], p) for b in ISCH.BLOCKS for p in b["params"] if p["attr"]]
check("the register test covers every intensity block parameter", all(p["key"] in NEW for _, p in params),
      str([p["key"] for _, p in params if p["key"] not in NEW]))


def reg(attr):
    mod, a = attr.split(".")
    return getattr(getattr(r, mod), a)


PRE = {"int_p": {"int_gain_mode": "manual"}, "int_i_hz": {"int_gain_mode": "manual"},
       "int_ugf_khz": {"int_gain_mode": "bandwidth"}, "int_corner_khz": {"int_gain_mode": "bandwidth"},
       "int_clamp_lo_mv": {"int_clamp_auto": False}, "int_clamp_hi_mv": {"int_clamp_auto": False},
       "int_clamp_margin": {"int_clamp_auto": True}, "int_filter_stages": {"int_filter_hz": 200e3}}


def try_param(bid, p):
    k = p["key"]
    pre = PRE.get(k, {})
    saved = {pk: ctl.s[pk] for pk in pre}
    if pre:
        apply(**pre)
    old, before = ctl.s[k], reg(p["attr"])
    if k == "int_clamp_margin":                  # the margin acts through the next scan
        apply(int_clamp_margin=NEW[k])
        ctl.submit("int_switch", ("scan", True)); wait_idle(); time.sleep(1.5)
        ctl.submit("int_switch", ("lock", True)); wait_idle()
    else:
        apply(**{k: NEW[k]})
    after = reg(p["attr"])
    changed = before != after if not isinstance(after, float) else abs(after - before) > 1e-12
    hwv = it.hw.get(p["attr"])
    hw_ok = hwv == after or (isinstance(after, float) and hwv is not None and abs(hwv - after) < 1e-9)
    check(f"{bid}: {k} {old} -> {NEW[k]} changes {p['attr']}", changed and hw_ok, f"{before} -> {after}")
    apply(**{k: old})
    if k == "int_clamp_margin":
        ctl.submit("int_switch", ("scan", True)); wait_idle(); time.sleep(1.5)
        ctl.submit("int_switch", ("lock", True)); wait_idle()
    if pre:
        apply(**saved)


for bid, p in params:
    if p["live"] == "always":
        try_param(bid, p)
for bid, p in params:
    if p["live"] == "locked":
        if it.mode != "locked":
            ctl.submit("int_switch", ("lock", True)); wait_idle()
        try_param(bid, p)
ctl.submit("int_switch", ("scan", True)); wait_idle()
for bid, p in params:
    if p["live"] == "scanning":
        try_param(bid, p)
time.sleep(1.0)
ctl.submit("int_switch", ("lock", True)); wait_idle()
check("filter snaps to the ladder and shows used vs asked", True)

print("--- probes and switches")
ctl.submit("int_probe", "asg1"); wait_idle()
check("probe click asg1 -> replaces one channel", "asg1" in (ctl.s["int_probe_a"], ctl.s["int_probe_b"]))
ctl.submit("int_probe", "err"); wait_idle()
check("a grey probe point is refused", any("not a scope signal" in l for l in list(ctl.logs)[-3:]))
apply(int_probe_a="out2", int_probe_b="asg1")
time.sleep(1.0)
pr = it.probe or {}
check("probe capture of out2 / asg1 reaches the panel", pr.get("a_name") == "out2" and pr.get("a") is not None)
apply(int_probe_a="in2", int_probe_b="pid2")
ctl.submit("int_switch", ("output", False)); wait_idle()
check("Output off: OUT2 = 0 V, mode off", it.mode == "off" and r.pid2.output_direct == "off" and r.pid2.ival == 0
      and abs(np.mean(window()["out2"])) < 1e-9)
ctl.submit("int_switch", ("lock", True)); wait_idle()
check("Lock from off turns pid2 on and locks", it.mode == "locked" and r.pid2.output_direct == "out2")
ctl.submit("int_switch", ("reengage", False)); wait_idle()
check("Re-engage switch toggles the setting", ctl.s["int_auto_reengage"] is False)
ctl.submit("int_switch", ("reengage", True)); wait_idle()

print("--- Bode plot (asg1 on OUT2, scope on pid2 and in2)")
apply(int_bode_points=8, int_bode_start_hz=2e3, int_bode_stop_hz=400e3, int_bode_cycles=20)
ctl.submit("int_bode"); time.sleep(0.5); wait_idle(120)
B = it.bode
check("Bode: every point, still locked, asg1 off afterwards", B and B["done"] and B["n"] == 8 and it.mode == "locked"
      and r.asg1.output_direct == "off" and r.asg1.amplitude == 0, f"n={B and B['n']}")
f_ = np.array(B["curves"][0]["f"]); G_ = np.array(B["curves"][0]["c"])
K = aom.slope(float(np.clip(r.pid2.ival, r.pid2.min_voltage, r.pid2.max_voltage)))
H = np.exp(-2j * np.pi * f_ * aom.delay) / (1 + 1j * f_ / aom.PD_BW)
Gt = -K * H * (r.pid2.p + r.pid2.i / (1j * f_))
low = f_ < 1e5
check("measured G = the fake's true loop gain below 100 kHz (10 %, 10 deg)",
      np.all(np.abs(np.abs(G_[low] / Gt[low]) - 1) < 0.1)
      and np.all(np.abs(np.degrees(np.angle(G_[low] / Gt[low]))) < 10), f"{np.round(np.abs(G_ / Gt), 3)}")
check("UGF near the bandwidth setting (15 %)", B["ugf"] and abs(B["ugf"] / 30e3 - 1) < 0.15, f"{B['ugf']}")
apply(int_bode_points=40)
ctl.submit("int_bode"); time.sleep(1.0)
ctl.stop_measurement(); wait_idle(60)
check("Stop ends the sweep early, still locked", it.bode["done"] and it.bode["n"] < 40 and it.mode == "locked"
      and r.asg1.output_direct == "off", f"n={it.bode['n']}")
ctl.submit("int_switch", ("lock", False)); wait_idle()
ctl.submit("int_bode"); wait_idle()
check("Bode refused while not locked", any("works while locked" in l for l in list(ctl.logs)[-3:]))
ctl.submit("int_switch", ("lock", True)); wait_idle()

print("--- settings compatibility")
tmp = os.path.join(tempfile.mkdtemp(), "old.json")
old = {k: v for k, v in A.DEFAULTS.items() if k in A.CAV_KEYS}
old.update(out2_signal="photodiode", out2_gain=2.0)
json.dump(old, open(tmp, "w"))
A.SETTINGS_FILE = tmp
n_err = sum("ERROR" in l for l in ctl.logs)
ctl.submit("load"); wait_idle(); time.sleep(0.5)
check("an old settings file (out2_signal, out2_gain) loads without errors", sum("ERROR" in l for l in ctl.logs) == n_err
      and "out2_signal" not in ctl.s and "out2_gain" not in ctl.s)
check("missing intensity keys keep their defaults", all(ctl.s[k] == A.DEFAULTS[k] for k in
                                                         ("int_out2_limit_mv", "int_scan_lo_mv", "int_light_frac")))
check("the loaded file did not unlock the intensity servo", it.mode == "locked")

print("--- restart: adopt a running servo, reset otherwise")
p_before, i_before, iv_before = r.pid2.p, r.pid2.i, r.pid2.ival
n0 = len(F.WRITES)
it2 = INT.Intensity(ctl)
it2.startup()
pid2_writes = [w for w in F.WRITES[n0:] if w[1] == "pid2"]
check("a running servo (out2, gains != 0) is adopted as locked", it2.mode == "locked" and it2.lock_info.get("K"))
check("adoption writes nothing to pid2 or asg1", not [w for w in F.WRITES[n0:] if w[1] in ("pid2", "asg1")
                                                      and w[2] != "owner"], str(pid2_writes))
check("registers unchanged after adoption", (r.pid2.p, r.pid2.i) == (p_before, i_before))
stop.set(); time.sleep(0.5)
r.pid2.p, r.pid2.i = 0, 0                                # a held servo: out2, zero gains, ival != 0
r.pid2.ival = 0.02
it3 = INT.Intensity(ctl)
it3.startup()
check("a held servo (zero gains) is reset: OUT2 = 0 V, output off", it3.mode == "off" and r.pid2.ival == 0
      and r.pid2.output_direct == "off" and abs(np.mean(window()["out2"])) < 1e-9)

print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "ALL PASS")
