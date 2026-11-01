import sys, threading
sys.path.insert(0, "fake"); sys.path.insert(0, "..")
import cavity_lock_app as A
ctl = A.Controller("fake", "t", dict(A.DEFAULTS))
app = A.build_app(ctl)
threading.Thread(target=lambda: app.run(host="127.0.0.1", port=8070, debug=False), daemon=True).start()
stop = threading.Event(); threading.Timer(170, stop.set).start()
ctl.run(stop)
