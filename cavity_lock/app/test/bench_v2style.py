"""Before/after: v3 controller with the v2 capture() (writes every scope setting each shot, polls every 3 ms,
reads scope.times)."""
import os, sys, time, threading
os.environ["FAKE_LAN_S"] = "0.00025"
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import numpy as np
import cavity_lock_app as A
import pyrpl as F

def capture_v2(self, in1, in2, duration, trigger="immediately"):
    sc = self.r.scope
    sc.input1, sc.input2 = in1, in2
    opts = sorted(getattr(sc, "durations", []) or [])
    if opts:
        duration = min([d for d in opts if d >= duration * 0.999] or [opts[-1]])
    sc.duration = duration
    sc.average = True
    sc.rolling_mode = False
    sc.trigger_source = trigger
    if trigger != "immediately":
        sc.trigger_delay = sc.duration / 2
    start = getattr(sc, "_start_trace_acquisition", None) or getattr(sc, "_start_acquisition", None)
    read = getattr(sc, "_get_trace", None) or getattr(sc, "_get_curve", None)
    start()
    t_end = time.time() + 3 * sc.duration + 2.0
    while not sc.curve_ready():
        if time.time() > t_end:
            raise TimeoutError
        time.sleep(0.003)
    data = np.array(read())
    n = data.shape[-1]
    t = np.array(sc.times)[:n]
    return t - t[0], data[0], data[1]

if sys.argv[1] == "v2":
    A.Controller.capture = capture_v2
ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
stop = threading.Event(); threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()
def rate(sec):
    r0, t0, frames, last = F.REG_COUNT[0], time.time(), 0, None
    while time.time() - t0 < sec:
        tr = ctl.traces
        if tr is not None and tr is not last:
            frames += 1; last = tr
        time.sleep(0.002)
    return frames / (time.time() - t0), (F.REG_COUNT[0] - r0) / max(frames, 1)
time.sleep(3); fa, ra = rate(6)
ctl.submit("lock"); t0 = time.time()
while ctl.mode != "locked" and time.time() - t0 < 60: time.sleep(0.1)
time.sleep(1.5); fl, rl = rate(6)
print(f"{sys.argv[1]}-style capture: acquisition {fa:.1f} traces/s ({ra:.0f} register accesses each), locked {fl:.1f} traces/s ({rl:.0f} each)")
stop.set()
