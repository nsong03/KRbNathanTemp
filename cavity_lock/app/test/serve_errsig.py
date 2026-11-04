import json, os, sys, threading
os.environ.setdefault("FAKE_PZT_OUT", "out2")      # this app's wiring: OUT2 drives the PZT
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import errsig_app as E
import errsig_ui
import pyrpl as F
ctl = E.ErrsigController("fake", "t", dict(E.DEFAULTS))
app = errsig_ui.build_app(ctl, E)


@app.server.route("/_test/stats")                  # TEST ONLY
def _stats():
    r = ctl.r
    return json.dumps(dict(mode=ctl.mode, offset=r.asg0.offset, amp=r.asg0.amplitude, pid1_out=r.pid1.output_direct,
                           pid1_i=r.pid1.i, pid0_out=r.pid0.output_direct, pid0_i=r.pid0.i,
                           center=ctl.s["ramp_center_mv"]))


port = int(os.environ.get("PORT", "8061"))
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(float(os.environ.get("SERVE_S", "600")), stop.set).start()
ctl.run(stop)
