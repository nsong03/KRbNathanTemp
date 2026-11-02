import os, sys, threading
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import cavity_lock_app as A
import pyrpl as F
ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
app = A.build_app(ctl)


@app.server.route("/_test/writes")                 # TEST ONLY: register-write counters of the fake
def _writes():
    import json
    return json.dumps(dict(all=F.WRITE_N[0], nonscope=F.WRITE_N_NONSCOPE[0], n=len(F.WRITES),
                           last=[list(map(str, w)) for w in F.WRITES[-20:]]))


@app.server.route("/_test/stats")                  # TEST ONLY: frame counters per loop
def _stats():
    import json
    return json.dumps(dict(cav_seq=ctl.seq, int_seq=ctl.ints.seq, view=ctl.view, cav_mode=ctl.mode,
                           int_mode=ctl.ints.mode, int_bode_seq=ctl.ints.bode_seq))


port = int(os.environ.get("PORT", "8070"))
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(float(os.environ.get("SERVE_S", "900")), stop.set).start()
ctl.run(stop)
