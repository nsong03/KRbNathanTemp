"""Ramp / scan setpoint (ramp centre): moves the scan, moves OUT1 after Scan off, never touches a lock.
Run from app/test:  python test_ramp_setpoint.py"""
import sys, threading, time
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import cavity_lock_app as A, schematic as SCH
ctl = A.Controller("fake", "t", dict(A.DEFAULTS)); r = ctl.r
stop = threading.Event(); threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()
def idle(t=30):
    t0 = time.time()
    while time.time() - t0 < t and (not ctl.cmds.empty() or ctl.mode in SCH.BUSY): time.sleep(0.05)
    time.sleep(0.3)
F = []
def check(n, ok, info=""): print(("PASS " if ok else "FAIL ") + n, info); ok or F.append(n)
time.sleep(1.5)
ctl.submit("ramp_nudge", 10.0); idle()
check("scanning: +10 mV moves the ramp centre (asg0.offset)", abs(r.asg0.offset - 0.010) < 1e-9 and ctl.s["ramp_center_mv"] == 10, r.asg0.offset)
ctl.submit("ramp_center", -20.0); idle()
check("typed -20 mV", abs(r.asg0.offset + 0.020) < 1e-9)
ctl.submit("switch", ("scan", False)); idle()
check("Scan off parks OUT1 at the setpoint", ctl.mode == "held" and abs(r.pid0.ival + 0.020) < 1e-4, r.pid0.ival)
ctl.submit("ramp_nudge", 1.0); idle()
check("held after Scan off: setpoint moves the parked OUT1", abs(r.pid0.ival + 0.019) < 1e-4, r.pid0.ival)
check("diagram says held at the setpoint", "held at the setpoint" in SCH.diagram_state(ctl.snapshot())["boxes"]["ramp"][0][0])
ctl.submit("ramp_center", 5000.0); idle()
check("clipped to the OUT1 limit", ctl.s["ramp_center_mv"] == 950 and abs(r.pid0.ival - 0.95) < 1e-4)
ctl.submit("ramp_center", 0.0); ctl.submit("switch", ("scan", True)); idle(); time.sleep(2)
ctl.submit("lock"); idle(60)
iv = r.pid0.ival
ctl.submit("ramp_nudge", 10.0); idle()
check("locked: the ramp setpoint does not touch the lock", ctl.mode == "locked" and abs(r.pid0.ival - iv) < 0.002, f"{iv} {r.pid0.ival}")
ctl.submit("switch", ("lock", False)); idle(); iv = r.pid0.ival
ctl.submit("ramp_nudge", 10.0); idle()
check("held after an unlock: not moved", ctl.mode == "held" and abs(r.pid0.ival - iv) < 1e-6)
stop.set(); print("FAILED", F) if F else print("ALL PASS")
