import os, sys, time, threading, cProfile, pstats, json
os.environ["FAKE_DT"] = "0.004"
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import cavity_lock_app as A
import plotly.utils
ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
stop = threading.Event(); threading.Thread(target=ctl.run, args=(stop,), daemon=True).start()
app = A.build_app(ctl)
time.sleep(4)
cb = [v for k, v in app.callback_map.items() if "frame.data" in k and "frame-seq" in k][0]["callback"]
fn = getattr(cb, "__wrapped__", None)
print("have wrapped:", fn is not None)
def one():
    out = fn(1, ctl.version, -5, ctl.meas_seq, "", -1)   # force a new frame (and diagram state)
    return json.dumps(out[0], cls=plotly.utils.PlotlyJSONEncoder)
for _ in range(3): one()
t0 = time.time(); n = 20
for _ in range(n): s = one()
print(f"tick incl. JSON of the frame: {(time.time()-t0)/n*1e3:.1f} ms, frame {len(s)/1e3:.0f} kB")
pr = cProfile.Profile(); pr.enable()
for _ in range(10): one()
pr.disable(); pstats.Stats(pr).sort_stats("cumulative").print_stats(12)
stop.set()
