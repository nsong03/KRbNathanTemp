"""Register isolation between the two loops, with both running at once (fake that logs every register write
with the app source file that made it). Also: the scope schedule keeps the hidden loop's watchdog alive.
Run from app/test:  python test_isolation.py"""
import sys
import threading
import time

sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import cavity_lock_app as A
import pyrpl as F

FAILS = []
CAV_FILES = {"cavity_lock_app.py", "schematic.py"}
INT_FILES = {"intensity.py", "intensity_schematic.py"}


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
eng, it, r = ctl.r.eng, ctl.ints, ctl.r
stop = threading.Event()
threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()


def idle(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout and (not ctl.cmds.empty() or ctl.mode in ("locking", "busy", "measuring")
                                          or it.mode in ("locking", "measuring")):
        time.sleep(0.05)
    time.sleep(0.2)


def wait_for(cond, timeout=20):
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
    idle()


print("--- both loops up")
time.sleep(2.0)
ctl.submit("int_switch", ("output", True)); ctl.submit("int_switch", ("scan", True)); idle()
time.sleep(2.0)                                   # the cavity scans and the intensity scans at the same time
ctl.submit("lock"); ctl.submit("int_switch", ("lock", True)); idle(90)
check("cavity and intensity both locked", wait_for(lambda: ctl.mode == "locked" and it.mode == "locked", 30),
      f"{ctl.mode} / {it.mode}")

print("--- hidden-loop watchdogs (scope schedule)")
for view, hid in (("intensity", "cav"), ("cavity", "int")):
    ctl.view = view
    time.sleep(0.5)
    f0, b0, t0 = dict(ctl.frames), dict(ctl.bg_count), time.time()
    time.sleep(4.0)
    dt = time.time() - t0
    fr = (ctl.frames[hid] - f0[hid]) / dt
    bg = (ctl.bg_count[hid] - b0[hid]) / dt
    vis = "int" if hid == "cav" else "cav"
    fv = (ctl.frames[vis] - f0[vis]) / dt
    check(f"view {view}: hidden {hid} gets >= 5 watchdog checks/s (frames {fr:.1f}/s + register checks {bg:.1f}/s)",
          fr + bg >= 5 and fr >= 0.8, f"visible {vis} {fv:.1f} frames/s")

print("--- without a sampler the hidden loop gets scope watchdog frames")
ctl.has_sampler = it.has_sampler = False
for view, hid in (("intensity", "cav"), ("cavity", "int")):
    ctl.view = view
    time.sleep(0.5)
    f0, t0 = dict(ctl.frames), time.time()
    time.sleep(3.0)
    fr = (ctl.frames[hid] - f0[hid]) / (time.time() - t0)
    check(f"no sampler, view {view}: hidden {hid} gets >= 4 scope frames/s", fr >= 4, f"{fr:.1f}/s")
ctl.has_sampler = it.has_sampler = True

print("--- a cavity unlock while the intensity view is shown is caught and relocked")
ctl.view = "intensity"
time.sleep(0.5)
before = set(ctl.logs)
kick = 0.045                                      # beyond the +/-34 mV clamp: a real unlock
eng.vc += kick
ok = wait_for(lambda: any("UNLOCK" in l and l not in before for l in list(ctl.logs)), 5)
eng.vc -= kick                                    # the resonance comes back for the relock search
check("unlock detected while the cavity view is hidden", ok)
check("relocked while hidden", wait_for(lambda: ctl.mode == "locked", 60), ctl.mode)
check("the intensity servo stayed locked meanwhile", it.mode == "locked")

print("--- measurements in one loop, the other keeps running")
n_mark = len(F.WRITES)
apply(int_bode_points=10, int_bode_start_hz=2e3, int_bode_stop_hz=300e3)
b0, t0 = ctl.bg_count["cav"], time.time()
ctl.submit("int_bode"); time.sleep(0.5); idle(120)
dt = time.time() - t0
check("intensity Bode completed, cavity still locked", it.bode and it.bode["done"] and ctl.mode == "locked"
      and it.mode == "locked")
check("cavity register checks ran during the intensity Bode (>= 5/s)", (ctl.bg_count["cav"] - b0) / dt >= 5,
      f"{(ctl.bg_count['cav'] - b0) / dt:.1f}/s")
w_bode = [w for w in F.WRITES[n_mark:] if w[1] not in ("scope",) and w[4] in INT_FILES]
check("the intensity Bode wrote only pid2 / asg1", {w[1] for w in w_bode} <= {"pid2", "asg1"},
      str({w[1] for w in w_bode}))
n_mark = len(F.WRITES)
apply(bode_points=6, bode_start_hz=5.0, bode_stop_hz=200.0, bode_mode="closed loop")
b0, t0 = ctl.bg_count["int"], time.time()
ctl.submit("bode"); time.sleep(0.5); idle(180)
dt = time.time() - t0
check("cavity closed-loop Bode completed, intensity still locked", ctl.bode and ctl.bode["done"] and it.mode == "locked"
      and ctl.mode == "locked")
check("intensity register checks ran during the cavity Bode (>= 5/s)", (ctl.bg_count["int"] - b0) / dt >= 5,
      f"{(ctl.bg_count['int'] - b0) / dt:.1f}/s")
apply(bode_mode="circuit only")
ctl.submit("bode"); time.sleep(0.5); idle(180)
check("cavity circuit-only Bode completed, intensity still locked", ctl.bode["done"] and it.mode == "locked")
w_cav = [w for w in F.WRITES[n_mark:] if w[4] in CAV_FILES and w[1] != "scope"]
check("the cavity Bodes wrote only cavity modules (+ iq1/pid1 copies)",
      {w[1] for w in w_cav} <= {"iq0", "asg0", "pid0", "iq1", "pid1"}, str({w[1] for w in w_cav}))
ctl.submit("noise"); idle(60)
ctl.submit("loop"); idle(60)
check("cavity noise + loop measurements done, intensity still locked", it.mode == "locked" and ctl.mode == "locked")

print("--- the intensity watchdog acts while the cavity view is shown")
ctl.view = "cavity"
time.sleep(0.3)
eng.aom.blocked = True
check("blocked beam caught while hidden", wait_for(lambda: it.mode == "nolight", 3), it.mode)
eng.aom.blocked = False
check("re-engaged while hidden", wait_for(lambda: it.mode == "locked", 5), it.mode)

print("--- switching views writes nothing to the hardware")
time.sleep(0.5)
n0 = F.WRITE_N_NONSCOPE[0]
for v in ("intensity", "cavity") * 5:
    ctl.view = v
    time.sleep(0.3)
check("10 view switches with both loops locked: no register write but the scope", F.WRITE_N_NONSCOPE[0] == n0,
      f"{F.WRITE_N_NONSCOPE[0] - n0} writes")

print("--- the register-write log (both directions)")
W = list(F.WRITES)
cav_w = [w for w in W if w[4] in CAV_FILES]
int_w = [w for w in W if w[4] in INT_FILES]
other = sorted({w[4] for w in W} - CAV_FILES - INT_FILES)
check(f"log has writes from both loops ({len(cav_w)} cavity, {len(int_w)} intensity)", cav_w and int_w)
check("no writes from other app files", not other, str(other))
bad = [w for w in cav_w if w[1] in ("pid2", "asg1")]
check("cavity code never writes pid2 or asg1", not bad, str(bad[:3]))
bad = [w for w in cav_w if w[2] == "output_direct" and w[3] in ("out2", "both")]
check("cavity code never routes anything to out2 / both", not bad, str(bad[:3]))
bad = [w for w in cav_w if w[1] == "networkanalyzer" and w[2] == "output_direct" and w[3] != "out1"]
check("the cavity network analyzer injects on out1 only", not bad, str(bad[:3]))
bad = [w for w in int_w if w[1] in ("iq0", "asg0", "pid0")]
check("intensity code never writes iq0, asg0 or pid0", not bad, str(bad[:3]))
bad = [w for w in int_w if w[2] == "output_direct" and w[3] in ("out1", "both")]
check("intensity code never routes anything to out1 / both", not bad, str(bad[:3]))
bad = [w for w in int_w if w[1] not in ("pid2", "asg1", "scope")]
check("intensity code writes only pid2, asg1 (and the shared scope)", not bad, str({w[1] for w in bad}))
bad = [w for w in W if w[1] == "pid1" and w[2] == "output_direct" and w[3] != "off"]
check("pid1 output never on (kept free; borrowed only with output off)", not bad, str(bad[:3]))
check("scope writes come from the shared capture code only",
      {w[4] for w in W if w[1] == "scope"} <= {"cavity_lock_app.py"})

stop.set()
print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "ALL PASS")
