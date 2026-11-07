import json, os, sys, threading
os.environ.setdefault("FAKE_LOCKIN", "iq2")
os.environ.setdefault("FAKE_EXTREF", "1")           # external dither at 1000.0137 Hz on the PZT and IN2
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import extref_app as E
import extref_ui
ctl = E.ExtRefController("fake", "t", dict(E.DEFAULTS))
app = extref_ui.build_app(ctl, E)


@app.server.route("/_test/stats")                  # TEST ONLY
def _stats():
    r = ctl.r
    return json.dumps(dict(mode=ctl.mode, p=r.pid0.p, i=r.pid0.i, out=r.pid0.output_direct, eo=r.pid1.output_direct,
                           eo_in=r.pid1.input, f=r.iq2.frequency, f_ext=r.eng.f_ext, ref=ctl.ref.get("locked"),
                           amp=r.asg0.amplitude, filt=list(r.pid0.inputfilter), vc=r.eng.vc,
                           lo=r.pid0.min_voltage, hi=r.pid0.max_voltage))


port = int(os.environ.get("PORT", "8106"))
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(float(os.environ.get("SERVE_S", "600")), stop.set).start()
ctl.run(stop)
