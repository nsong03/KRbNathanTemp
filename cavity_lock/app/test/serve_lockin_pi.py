import json, os, sys, threading
os.environ.setdefault("FAKE_LOCKIN", "iq2")        # dual-phase lock-in on iq2, PZT on OUT1, AOM on OUT2
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import lockin_pi_app as E
import lockin_pi_ui
ctl = E.LockinPIController("fake", "t", dict(E.DEFAULTS))
app = lockin_pi_ui.build_app(ctl, E)


@app.server.route("/_test/stats")                  # TEST ONLY
def _stats():
    r = ctl.r
    return json.dumps(dict(mode=ctl.mode, int_on=ctl.int_on, p=r.pid0.p, i=r.pid0.i, pin=r.pid0.input,
                           out=r.pid0.output_direct, filt=list(r.pid0.inputfilter), ifilt=list(r.pid2.inputfilter),
                           ip=r.pid2.p, ii=r.pid2.i, iout=r.pid2.output_direct, phase=r.iq2.phase,
                           dither=r.iq2.output_direct, amp=r.asg0.amplitude, vc=r.eng.vc))


port = int(os.environ.get("PORT", "8086"))
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(float(os.environ.get("SERVE_S", "600")), stop.set).start()
ctl.run(stop)
