import sys, threading, time
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import cavity_lock_app as A
import pyrpl as F

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
print("\nLOG:"); print("\n".join(ctl.logs))
stop.set()
