"""Intensity DC output (bypass): constant OUT2 level for calibration, steps, clip, Bode around it, lock from it.
Run from app/test:  python test_dc_output.py"""
import sys, threading, time, os
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np, cavity_lock_app as A, intensity as INT, intensity_schematic as ISCH
if os.path.exists(INT.PLANT_FILE): os.remove(INT.PLANT_FILE)
ctl = A.Controller("fake", "t", dict(A.DEFAULTS)); it = ctl.ints; r = ctl.r; aom = r.eng.aom
stop = threading.Event(); threading.Thread(target=ctl.run, args=(stop,), daemon=True).start(); ctl.view = "intensity"
F = []
def check(n, ok, info=""): print(("PASS " if ok else "FAIL ") + n, info); ok or F.append(n)
def idle(t=60):
    t0 = time.time()
    while time.time()-t0 < t and (not ctl.cmds.empty() or it.mode in ("locking", "measuring")): time.sleep(0.05)
    time.sleep(0.3)
time.sleep(1)
ctl.submit("int_dc", 20.0); idle()
check("level set while DC off: OUT2 untouched", it.mode == "off" and r.pid2.output_direct == "off" and ctl.s["int_dc_mv"] == 20)
ctl.submit("int_switch", ("dc", True)); idle()
check("DC on: loop open, OUT2 = level", it.mode == "dc" and r.pid2.output_direct == "out2" and r.pid2.p == 0 and r.pid2.i == 0
      and abs(r.pid2.ival - 0.020) < 1e-9, f"{r.pid2.ival}")
time.sleep(0.4); pd20 = it.pd
check("photodiode follows the DC level (fake plant)", abs(pd20 - aom.pd(0.020)) < 0.01, f"{pd20:.4f} vs {float(aom.pd(0.020)):.4f}")
ctl.submit("int_dc_nudge", -10.0); idle(); time.sleep(0.4)
check("step -10 mV moves OUT2 at once", abs(r.pid2.ival - 0.010) < 1e-9 and it.pd > pd20, f"{r.pid2.ival} pd {it.pd:.3f}")
ctl.submit("int_dc", 5000.0); idle()
check("clipped to the OUT2 limit", ctl.s["int_dc_mv"] == 300 and abs(r.pid2.ival - 0.3) < 1e-9)
ctl.submit("int_dc", 0.0); idle()
st = ISCH.diagram_state(it.snapshot())
check("diagram: DC switch on, DC wire live, box readback", st["sw"]["dc"]["on"] and st["wires"]["dc_out"] and "ON" in st["boxes"]["i_dc"][0][0])
ctl.submit("apply", ({**{k: ctl.s[k] for k in A.KEYS}, "int_bode_points": 6, "int_bode_start_hz": 2e3, "int_bode_stop_hz": 3e5}, ctl.version)); idle()
ctl.submit("int_bode"); idle()
check("Bode in DC mode = open-loop plant, back to DC at the same level", it.bode["mode"] == "open loop" and it.mode == "dc"
      and abs(r.pid2.ival) < 1e-9 and r.asg1.output_direct == "off" and (it.plant or {}).get("K", 0) < 0, f"K {(it.plant or {}).get('K')}")
ctl.submit("int_dc", 15.0); idle()
ctl.submit("int_switch", ("lock", True)); idle(); time.sleep(0.4)
check("Lock from DC starts at the DC level and locks", it.mode == "locked" and abs(it.lock_info["u_sp"] - 0.015) < 1e-9
      and not it.dc_on and abs(it.pd / 0.5 - 1) < 0.01, f"{it.mode} {it.pd}")
ctl.submit("int_switch", ("dc", True)); idle()
check("DC on while locked: unlocks, OUT2 = level", it.mode == "dc" and r.pid2.i == 0 and abs(r.pid2.ival - 0.015) < 1e-9)
ctl.submit("int_switch", ("dc", False)); idle()
check("DC off: held at the level, loop open", it.mode == "held" and not it.dc_on and abs(r.pid2.ival - 0.015) < 1e-9)
ctl.submit("int_switch", ("dc", True)); idle(); ctl.submit("int_switch", ("output", False)); idle()
check("Output off ends DC: OUT2 = 0 V", it.mode == "off" and not it.dc_on and r.pid2.output_direct == "off")
stop.set(); print("FAILED", F) if F else print("ALL PASS")
