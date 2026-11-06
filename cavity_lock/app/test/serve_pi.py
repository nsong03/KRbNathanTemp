import json, os, sys, threading
os.environ.setdefault("FAKE_PLANT", "errin")       # IN1 = external error signal of the fake cavity
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import pi_app as E
import pi_ui
ctl = E.PIController("fake", "t", dict(E.DEFAULTS))
app = pi_ui.build_app(ctl, E)


@app.server.route("/_test/stats")                  # TEST ONLY
def _stats():
    r = ctl.r
    return json.dumps(dict(mode=ctl.mode, p=r.pid0.p, i=r.pid0.i, out=r.pid0.output_direct, filt=list(r.pid0.inputfilter),
                           amp=r.asg0.amplitude, offset=r.asg0.offset, asg_out=r.asg0.output_direct,
                           vc=r.eng.vc, setpoint=r.pid0.setpoint))


port = int(os.environ.get("PORT", "8076"))
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(float(os.environ.get("SERVE_S", "600")), stop.set).start()
ctl.run(stop)
