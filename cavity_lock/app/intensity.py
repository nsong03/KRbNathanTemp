"""
intensity.py -- AOM intensity stabilization on the Red Pitaya's second channel: IN2 (pickoff photodiode) ->
pid2 -> OUT2 (-> inverting bias driver -> mixer IF -> RF amplifier -> AOM).

Same architecture as the cavity lock: the Controller (cavity_lock_app.py) owns PyRPL and runs this from its main
thread; settings live in ctl.s (keys int_*), are version-checked, and show what the hardware uses; the diagram
model and the register writers are in intensity_schematic.py.

Modules: pid2 (servo, input in2, output_direct out2) and asg1 (power scan / Bode test sine on out2) only.
This file never touches iq0, asg0, pid0 or OUT1.

Modes: off -> scanning -> locking -> locked, plus held, measuring and the faults railed / nolight.
  off       pid2 p = i = ival = 0, output off, asg1 off: OUT2 = 0 V = bias level (the safe default)
  scanning  loop open, asg1 triangle on OUT2: light vs OUT2 -> efficiency peak, clamp, K at the setpoint
  locked    PI closed; watchdog: output at a clamp too long -> railed (hold); light low -> nolight (hold,
            re-engage when the light is back)
"""
import collections
import json
import math
import os
import time

import numpy as np

import intensity_schematic as ISCH

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_FILE = os.path.join(HERE, "intensity_scan.json")
I_PANELS = ["traces", "history", "power scan", "Bode (swept)"]
PANEL_LABEL = {}

# (key, label, default, group, kind, options) -- UI units
I_FIELDS = [
    ("int_out2_limit_mv", "OUT2 absolute limit [mV] (scan and clamps)", 300.0, "Intensity", "num",
     {"min": 10, "max": 950}),
    ("int_scan_lo_mv", "Scan from [mV of OUT2]", -200.0, "Intensity", "num", {}),
    ("int_scan_hi_mv", "Scan to [mV of OUT2]", 200.0, "Intensity", "num", {}),
    ("int_scan_hz", "Scan frequency [Hz]", 50.0, "Intensity", "num", {"min": 0.5, "max": 5000}),
    ("int_scan_avg", "Scan traces averaged", 4, "Intensity", "num", {"min": 1, "max": 50}),
    ("int_setpoint_mv", "Setpoint [mV of IN2]", 500.0, "Intensity", "num", {"min": 0, "max": 1000}),
    ("int_filter_hz", "Input filter corner [Hz] (<0 high-pass, 0 off)", 0.0, "Intensity", "num", {}),
    ("int_filter_stages", "Input filter stages", 1, "Intensity", "select", {"options": [1, 2, 3, 4]}),
    ("int_gain_mode", "Gains from", "bandwidth", "Intensity", "select", {"options": ["bandwidth", "manual"]}),
    ("int_ugf_khz", "Loop bandwidth [kHz]", 30.0, "Intensity", "num", {"min": 0.001}),
    ("int_corner_khz", "PI corner f (p = i / f) [kHz]", 1000.0, "Intensity", "num", {"min": 0.1}),
    ("int_p", "P (manual, magnitude)", 0.001, "Intensity", "select", {"options": ISCH.P_LADDER}),
    ("int_i_hz", "I [Hz] (manual, magnitude)", 1000.0, "Intensity", "select", {"options": ISCH.I_LADDER}),
    ("int_gain_ramp", "Ramp the gains up when locking", True, "Intensity", "check", {}),
    ("int_clamp_auto", "Clamp from the power scan", True, "Intensity", "check", {}),
    ("int_clamp_margin", "Clamp margin below the efficiency peak [x peak light]", 0.05, "Intensity", "num",
     {"min": 0, "max": 0.5}),
    ("int_clamp_lo_mv", "Clamp low [mV] (auto off)", -150.0, "Intensity", "num", {}),
    ("int_clamp_hi_mv", "Clamp high [mV] (auto off)", 150.0, "Intensity", "num", {}),
    ("int_rail_s", "Railed after [s] at a clamp", 0.2, "Intensity", "num", {"min": 0.01}),
    ("int_light_frac", "No light below [x setpoint]", 0.3, "Intensity", "num", {"min": 0, "max": 1}),
    ("int_auto_reengage", "Re-engage when the light is back", True, "Intensity", "check", {}),
    ("int_mw_per_v", "Photodiode [mW per V] (0 = volts only)", 0.0, "Intensity", "num", {"min": 0}),
    ("int_window_us", "Live trace length [us]", 1050.0, "Intensity", "num", {"min": 100, "max": 1e6}),
    ("int_bode_start_hz", "Bode: start frequency [Hz]", 1000.0, "Intensity", "num", {"min": 10}),
    ("int_bode_stop_hz", "Bode: stop frequency [Hz]", 1e6, "Intensity", "num", {"min": 100, "max": 2e7}),
    ("int_bode_points", "Bode: points", 30, "Intensity", "num", {"min": 3, "max": 200}),
    ("int_bode_cycles", "Bode: sine cycles per point", 20, "Intensity", "num", {"min": 2, "max": 500}),
    ("int_bode_amp_frac", "Bode: test sine [x setpoint of light]", 0.02, "Intensity", "num",
     {"min": 0.001, "max": 0.3}),
    ("int_probe_a", "Probe A", "in2", "Intensity", "select", {"options": ISCH.PROBE_SIGNALS + ["off"]}),
    ("int_probe_b", "Probe B", "pid2", "Intensity", "select", {"options": ISCH.PROBE_SIGNALS + ["off"]}),
    ("int_panels", "Show", ["traces", "history", "power scan"], "Intensity", "multi", {"options": I_PANELS}),
    # external hardware (display only / loop model)
    ("int_bias_v", "Bias |V_bias| [V] (display)", 1.2, "Intensity", "num", {"min": 0, "max": 5}),
    ("int_if_full_ma", "Driver current limit [mA]", 25.0, "Intensity", "num", {"min": 1, "max": 40}),
    ("int_rf_src_dbm", "80 MHz source [dBm]", 4.0, "Intensity", "num", {}),
    ("int_amp_gain_db", "RF amplifier gain [dB]", 32.0, "Intensity", "num", {}),
    ("int_aom_max_dbm", "AOM RF rating [dBm]", 31.0, "Intensity", "num", {}),
    ("int_pd_bw_mhz", "Photodiode bandwidth [MHz]", 5.0, "Intensity", "num", {"min": 0.01}),
    ("int_aom_delay_us", "AOM acoustic delay [us]", 0.25, "Intensity", "num", {"min": 0}),
]
I_KEYS = [f[0] for f in I_FIELDS]
GAIN_KEYS = {"int_gain_mode", "int_ugf_khz", "int_corner_khz", "int_p", "int_i_hz"}


def nearest(ladder, v):
    pos = [x for x in ladder if x > 0]
    if v <= 0 or not pos:
        return 0.0
    return min(pos, key=lambda x: abs(math.log(x / v)))


# ---------------------------------------------------------------------------------------------
# Power scan analysis (pure numpy)
# ---------------------------------------------------------------------------------------------
def analyze_scan(u, pd, s, binned):
    """Light vs OUT2 from one (averaged) scan. Returns the binned curve, the efficiency peak, the auto clamp
    (bright side where light = (1 - margin) x peak, dark side at the scan limit), K = dV_IN2/dV_OUT2 at the
    setpoint, the OUT2 value for the setpoint and the most light the clamp allows."""
    ub, (pb,) = binned(np.asarray(u), [np.asarray(pd)], nbins=300)
    if len(ub) < 20:
        return dict(ok=False, notes=["scan too short"])
    k = max(3, len(pb) // 60)
    ps = np.convolve(np.pad(pb, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
    lim = s["int_out2_limit_mv"] * 1e-3
    i_pk, i_dk = int(np.argmax(ps)), int(np.argmin(ps))
    pk, dk = float(ps[i_pk]), float(ps[i_dk])
    res = dict(ok=True, u=np.round(ub, 6).tolist(), pd=np.round(pb, 5).tolist(), peak=pk, dark=dk, notes=[],
               u_lo=float(ub[0]), u_hi=float(ub[-1]), t=time.time())
    if pk - dk < 0.02:
        res.update(ok=False, notes=["no light change in the scan: check the AOM, RF and photodiode"])
        return res
    bright = 1 if ub[i_pk] > ub[i_dk] else -1          # direction of more light along OUT2
    res["bright_dir"] = bright
    edge = 0.03 * (ub[-1] - ub[0])
    at_edge = (ub[i_pk] > ub[-1] - edge) if bright > 0 else (ub[i_pk] < ub[0] + edge)
    if at_edge:
        res["u_peak"] = None
        u_bright = float(ub[-1] if bright > 0 else ub[0])
        res["notes"].append("no efficiency peak inside the scan range: the bright clamp is the scan limit")
    else:
        res["u_peak"] = float(ub[i_pk])
        level = (1 - s["int_clamp_margin"]) * pk
        idx = range(i_dk, i_pk + 1) if i_pk > i_dk else range(i_dk, i_pk - 1, -1)
        u_bright = float(ub[i_pk])
        for j in idx:                                   # first crossing walking from dark towards the peak
            if ps[j] >= level:
                j0 = j - (1 if i_pk > i_dk else -1)
                if 0 <= j0 < len(ps) and ps[j] != ps[j0]:
                    u_bright = float(ub[j0] + (level - ps[j0]) * (ub[j] - ub[j0]) / (ps[j] - ps[j0]))
                else:
                    u_bright = float(ub[j])
                break
    u_dark = float(ub[0] if bright > 0 else ub[-1])
    lo, hi = sorted((u_bright, u_dark))
    lo, hi = max(lo, -lim), min(hi, lim)
    res.update(clamp=(lo, hi), u_bright=u_bright, u_dark=u_dark)
    res["max_lock"] = float(np.interp(u_bright, ub, ps))
    res.update(setpoint_info(res, s["int_setpoint_mv"] * 1e-3, ub, ps))
    return res


def setpoint_info(sc, sp, ub=None, ps=None):
    """OUT2 value and plant gain K for the setpoint, on the monotonic branch between the dark limit and the
    bright clamp."""
    if ub is None:
        ub, ps = np.asarray(sc["u"]), np.asarray(sc["pd"])
        k = max(3, len(ps) // 60)
        ps = np.convolve(np.pad(ps, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
    a, b = sorted((sc["u_dark"], sc["u_bright"]))
    m = (ub >= a) & (ub <= b)
    uu, pp = ub[m], ps[m]
    if len(uu) < 5:
        return dict(u_sp=None, K=None)
    if sc["bright_dir"] < 0:
        uu, pp = uu[::-1], pp[::-1]                     # dark -> bright
    above = np.flatnonzero(pp >= sp)
    if len(above) == 0 or above[0] == 0:
        return dict(u_sp=None, K=None, sp_note="setpoint above the most light the clamp allows" if len(above) == 0
                    else "setpoint below the darkest point of the scan")
    j = int(above[0])
    u_sp = float(uu[j - 1] + (sp - pp[j - 1]) * (uu[j] - uu[j - 1]) / (pp[j] - pp[j - 1]))
    w = np.abs(uu - u_sp) <= max(0.06 * abs(uu[-1] - uu[0]), 3 * abs(uu[1] - uu[0]))
    K = float(np.polyfit(uu[w], pp[w], 1)[0]) if w.sum() >= 3 else float((pp[j] - pp[j - 1]) / (uu[j] - uu[j - 1]))
    return dict(u_sp=u_sp, K=K)


def loop_model(f, K, p, i, filt, delay, pd_bw):
    """Open-loop gain G = -K (p + i/jf) F(f) exp(-j 2 pi f delay) / (1 + jf/f_pd); G > 0 = negative feedback."""
    f = np.asarray(f, dtype=float)
    C = p + i / (1j * f)
    for b in filt:
        if b > 0:
            C = C / (1 + 1j * f / b)
        elif b < 0:
            C = C * (1j * f / -b) / (1 + 1j * f / -b)
    H = np.exp(-2j * math.pi * f * delay) / (1 + 1j * f / pd_bw)
    return -K * C * H


# ---------------------------------------------------------------------------------------------
# The servo controller (runs in the Controller's main thread)
# ---------------------------------------------------------------------------------------------
class Intensity:
    def __init__(self, ctl):
        self.ctl, self.A, self.r = ctl, ctl.A, ctl.r
        self.mode = "off"
        self.hw, self.req = {}, {}
        self.scan = None                 # last completed power scan (analysis)
        self.scan_live = None            # analysis of the running scan
        self.scan_seq = 0
        self.lock_info = {}
        self.traces = None
        self.probe = None
        self.history = collections.deque(maxlen=20000)   # (t, mean photodiode V, mean drive V, mode)
        self.seq = 0
        self.fps = 0.0
        self._t_last = None
        self._tick = 0
        self.pd = None                   # last photodiode mean [V]
        self.drive = None                # last pid2 output mean [V]
        self.bode, self.bode_seq = None, 0
        self.meas_progress = ""
        self._good = collections.deque(maxlen=60)
        self._rail_since = None
        self._dark_n = 0
        self._light_since = None
        self._probe_last = "b"
        self.has_sampler = False
        self._pm_cache = {}
        self._warned_bg = False

    # ---- helpers ----
    @property
    def s(self):
        return self.ctl.s

    def log(self, msg):
        self.ctl.log("[int] " + msg)

    def bump(self):
        self.ctl.version += 1

    def _publish(self, **kw):
        with self.ctl._pub_lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if "traces" in kw:
                self.seq += 1
                now = time.time()
                if self._t_last:
                    dt = now - self._t_last
                    self.fps = 0.8 * self.fps + 0.2 / max(dt, 1e-3) if self.fps else 1 / max(dt, 1e-3)
                self._t_last = now

    def readback(self):
        hw = ISCH.read_hw(self.r)
        with self.ctl._pub_lock:
            self.hw = hw

    def snapshot(self):
        with self.ctl._pub_lock:
            return dict(mode=self.mode, hw=dict(self.hw), req=dict(self.req), settings=dict(self.s),
                        lock=dict(self.lock_info), scan=self.scan_live if self.mode == "scanning" else self.scan,
                        scan_done=self.scan, scan_seq=self.scan_seq, traces=self.traces, probe=self.probe,
                        history=list(self.history), seq=self.seq, fps=self.fps, pd=self.pd, drive=self.drive,
                        bode=self.bode, bode_seq=self.bode_seq, meas_progress=self.meas_progress,
                        pm=self.predicted_margin())

    def status(self):
        """(label, colour, dot class) for the status bar and the view icon."""
        m = self.mode
        if m == "locked":
            return "LOCKED", "#1e8e3e", "ok"
        if m in ("scanning", "locking", "measuring", "busy"):
            return m.upper(), "#2471a3", "busy"
        if m in ("railed", "nolight"):
            return {"railed": "RAILED (fault)", "nolight": "NO LIGHT (held)"}[m], "#c0392b", "fault"
        if m == "held":
            return "HELD", "#b9770e", "held"
        return "OUTPUT OFF", "#777777", "off"

    def K(self):
        return self.lock_info.get("K") or (self.scan or {}).get("K")

    def filt_list(self):
        return [b for b in (self.hw.get("pid2.inputfilter") or []) if b]

    def predicted_margin(self):
        K = self.K()
        if not K:
            return None
        p, i = self.gains_for(1.0, K)
        key = (K, p, i, tuple(self.filt_list()), self.s["int_aom_delay_us"], self.s["int_pd_bw_mhz"])
        if key not in self._pm_cache:
            fs = np.logspace(1, 8, 2500)
            G = loop_model(fs, K, p, i, key[3], ISCH.RP_DELAY + key[4] * 1e-6, key[5] * 1e6)
            self._pm_cache = {key: self.A.margins(fs, G)}
        return self._pm_cache[key]

    # ---- start-up and configuration ----
    def startup(self):
        r = self.r
        for name in ("pid2", "asg1"):
            try:
                getattr(r, name).owner = "intensity"
            except Exception:
                pass
        try:
            float(r.sampler.in2)
            self.has_sampler = True
        except Exception:
            self.has_sampler = False
            self.log("note: no sampler module: background light checks use the scope frames only")
        saved = self._load_scan()
        p2 = r.pid2
        od, p, i = p2.output_direct, float(p2.p), float(p2.i)
        if od == "out2" and (p != 0 or i != 0):
            # never reset a running servo: adopt it, read its setpoint and filter instead of writing them
            self.mode = "locked"
            self.s["int_setpoint_mv"] = round(float(p2.setpoint) * 1e3, 3)
            filt = [b for b in (p2.inputfilter or []) if b]
            self.s["int_filter_hz"] = round(float(filt[0]), 1) if filt else 0.0
            self.s["int_filter_stages"] = max(len(filt), 1)
            self.bump()
            if saved:
                self.scan = saved
                self.scan.update(setpoint_info(saved, float(p2.setpoint)))
            K = (self.scan or {}).get("K")
            self.lock_info = dict(K=K, clamp=(float(p2.min_voltage), float(p2.max_voltage)), t0=time.time(),
                                  adopted=True, i_hw=i, p_hw=p)
            self.log(f"adopted a running servo on OUT2 (p {p:+.4g}, i {i:+.4g} Hz, setpoint "
                     f"{float(p2.setpoint)*1e3:.1f} mV); "
                     + (f"K {K:+.2f} V/V from the saved scan" if K else "K unknown: gain changes need a power scan"))
        else:
            if od == "both":
                self.log("WARN pid2 was routed to both outputs; switching it off")
            a1 = r.asg1
            a1.output_direct = "off"
            a1.amplitude = 0
            p2.p, p2.i = 0, 0
            p2.ival = 0
            p2.output_direct = "off"
            self.mode = "off"
            if saved:
                self.scan = saved
            self.log("OUT2 = 0 V (bias level); intensity servo output off")
        if self.mode == "off":
            self.configure()                     # an adopted servo keeps its registers untouched
        self.readback()

    def configure(self):
        ISCH.write_static(self)
        self._snap()

    def _snap(self):
        """pid2.inputfilter snaps to a factor-2 ladder: show what the FPGA uses."""
        s = self.s
        filt = [b for b in (self.r.pid2.inputfilter or []) if b]
        hz = float(s["int_filter_hz"])
        if hz and filt and abs(abs(filt[0]) - abs(hz)) > 0.01 * abs(hz):
            self.req["int_filter_hz"] = hz
            s["int_filter_hz"] = round(float(filt[0]), 1)
            self.log(f"hardware values: int_filter_hz {hz:g} -> {s['int_filter_hz']}")
            self.bump()

    def _load_scan(self):
        try:
            with open(SCAN_FILE) as f:
                d = json.load(f)
            d["clamp"] = tuple(d["clamp"])
            return d
        except Exception:
            return None

    def _save_scan(self):
        try:
            with open(SCAN_FILE, "w") as f:
                json.dump(self.scan, f)
        except Exception as e:
            self.log(f"could not save the scan: {e!r}")

    def apply(self, changed):
        s = self.s
        if "int_gain_mode" in changed and s["int_gain_mode"] == "manual" and self.mode == "locked":
            p, i = float(self.r.pid2.p), float(self.r.pid2.i)   # start manual from the present gains
            s["int_p"], s["int_i_hz"] = nearest(ISCH.P_LADDER, abs(p)), nearest(ISCH.I_LADDER, abs(i))
            self.bump()
            self.log(f"manual gains start at the ladder values nearest the present ones: P {s['int_p']:g}, "
                     f"I {s['int_i_hz']:g} Hz")
        if "int_setpoint_mv" in changed:
            self._setpoint_changed()
        written = ISCH.apply_changed(self, changed)
        if "i_filter" in written:
            self._snap()
        if changed & GAIN_KEYS and self.K():
            fu, pm = self.predicted_margin() or (None, None)
            if pm is not None and pm < 45:
                self.log(f"WARN model phase margin {pm:.0f} deg at {fu/1e3:.0f} kHz: the loop may oscillate")
            self._cap_warning()

    def _setpoint_changed(self):
        sc = self.scan
        if sc and sc.get("ok"):
            sc.update(setpoint_info(sc, self.s["int_setpoint_mv"] * 1e-3))
            if sc.get("K") and self.mode in ("locked", "measuring"):
                self.lock_info["K"] = sc["K"]
                if self.mode == "locked":
                    self.set_gains(1.0)           # same bandwidth with K at the new setpoint
            if sc.get("max_lock") is not None and self.s["int_setpoint_mv"] * 1e-3 > sc["max_lock"]:
                self.log(f"WARN setpoint above the most light the clamp allows ({sc['max_lock']*1e3:.0f} mV): "
                         f"it will rail")

    def _cap_warning(self):
        K = self.K()
        if K and self.s["int_gain_mode"] == "bandwidth":
            cap = ISCH.I_MAX * abs(K)
            if self.s["int_ugf_khz"] * 1e3 > cap:
                self.log(f"WARN {self.s['int_ugf_khz']:g} kHz needs i > 38.9 kHz: the integrator bandwidth is "
                         f"capped at {cap/1e3:.1f} kHz with |K| = {abs(K):.2f}")

    # ---- commands ----
    def do(self, cmd, arg):
        s = self.s
        if cmd == "int_switch":
            self.switch(*arg)
        elif cmd == "int_probe":
            self.toggle_probe(arg)
        elif cmd in ("int_setpoint", "int_nudge"):
            v = float(arg) if cmd == "int_setpoint" else s["int_setpoint_mv"] + float(arg)
            v = round(min(max(v, 0.0), 1000.0), 3)
            if v != s["int_setpoint_mv"]:
                s["int_setpoint_mv"] = v
                self.bump()
                self.apply({"int_setpoint_mv"})
                self.log(f"setpoint {v:g} mV")
        elif cmd == "int_bode":
            self.measure_bode()

    def switch(self, name, on):
        on = bool(on)
        if self.mode in ISCH.BUSY:
            self.log(f"{name} switch ignored while {self.mode}")
            return
        if name == "reengage":
            self.s["int_auto_reengage"] = on
            self.bump()
            self.log(f"re-engage {'on' if on else 'off'}")
        elif name == "output":
            if on and self.r.pid2.output_direct != "out2":
                p2 = self.r.pid2
                p2.p, p2.i = 0, 0
                p2.ival = 0
                p2.output_direct = "out2"
                if self.mode != "scanning":
                    self.mode = "held"
                self.log("Output on: pid2 -> OUT2 (output 0 V = bias level, loop open)")
            elif not on:
                self.output_off()
        elif name == "scan":
            if on:
                if self.mode in ("locked", "railed", "nolight", "held"):
                    self.unlock("Scan on")
                self.scan_on()
            elif self.mode == "scanning":
                self.scan_off()
        elif name == "lock":
            if on:
                if self.mode != "locked":
                    self.engage()
            elif self.mode in ("locked", "railed", "nolight"):
                self.unlock("Lock off")

    def toggle_probe(self, sig):
        if sig not in ISCH.PROBE_SIGNALS:
            self.log(f"'{sig}' is not a scope signal")
            return
        s = self.s
        a, b = s["int_probe_a"], s["int_probe_b"]
        if sig == a:
            a = "off"
        elif sig == b:
            b = "off"
        elif a == "off":
            a, self._probe_last = sig, "a"
        elif b == "off":
            b, self._probe_last = sig, "b"
        elif self._probe_last == "b":
            a, self._probe_last = sig, "a"
        else:
            b, self._probe_last = sig, "b"
        s["int_probe_a"], s["int_probe_b"] = a, b
        if "traces" not in s["int_panels"]:
            s["int_panels"] = [p for p in I_PANELS if p in s["int_panels"] or p == "traces"]
        self.bump()
        self.log(f"probes: A = {a}, B = {b}")

    # ---- output, scan ----
    def output_off(self):
        r = self.r
        a1 = r.asg1
        a1.output_direct = "off"
        a1.amplitude = 0
        p2 = r.pid2
        p2.p, p2.i = 0, 0
        p2.ival = 0
        p2.output_direct = "off"
        if self.mode == "scanning":
            self._finish_scan()
        self.mode = "off"
        self.log("Output OFF: OUT2 = 0 V (bias level)")

    def _scan_limits(self):
        s = self.s
        lim = s["int_out2_limit_mv"] * 1e-3
        lo, hi = sorted((s["int_scan_lo_mv"] * 1e-3, s["int_scan_hi_mv"] * 1e-3))
        return max(lo, -lim), min(hi, lim)

    def scan_on(self, quiet=False):
        s, p2, a1 = self.s, self.r.pid2, self.r.asg1
        p2.p, p2.i = 0, 0
        p2.ival = 0
        lo, hi = self._scan_limits()
        a1.setup(waveform="ramp", frequency=float(s["int_scan_hz"]), amplitude=(hi - lo) / 2, offset=(hi + lo) / 2,
                 trigger_source="immediately", output_direct="out2")
        self.mode = "scanning"
        if not quiet:
            self.log(f"power scan: OUT2 {lo*1e3:+.0f}..{hi*1e3:+.0f} mV, triangle {s['int_scan_hz']:g} Hz, loop open")

    def scan_off(self):
        a1 = self.r.asg1
        a1.output_direct = "off"
        a1.amplitude = 0
        self._finish_scan()
        self.mode = "held" if self.r.pid2.output_direct == "out2" else "off"
        self.log("Scan off: OUT2 = " + ("pid2 output (0 V, loop open)" if self.mode == "held" else "0 V (bias level)"))

    def _finish_scan(self):
        sc = self.scan_live
        if sc and sc.get("ok"):
            self.scan = sc
            self.scan_live = None
            self._save_scan()
            with self.ctl._pub_lock:
                self.scan_seq += 1
            msg = (f"scan: peak {sc['peak']*1e3:.0f} mV" + (f" at {sc['u_peak']*1e3:+.0f} mV" if sc.get("u_peak")
                                                             is not None else " (at the scan edge)")
                   + f", clamp {sc['clamp'][0]*1e3:+.0f}..{sc['clamp'][1]*1e3:+.0f} mV, max lockable "
                   f"{sc['max_lock']*1e3:.0f} mV")
            if sc.get("K"):
                msg += f", K {sc['K']:+.2f} V/V at the setpoint (OUT2 {sc['u_sp']*1e3:+.1f} mV)"
            else:
                msg += f"; {sc.get('sp_note', 'no K at the setpoint')}"
            self.log(msg)

    # ---- frames (the Controller's scheduler calls frame()) ----
    def frame(self):
        if self.mode == "scanning":
            self._scan_frame()
        elif self.mode in ISCH.BUSY:
            self.ctl._wait(0.05)
        else:
            self._live_frame()

    def _scan_frame(self):
        s = self.s
        f = float(self.r.asg1.frequency) or s["int_scan_hz"]
        dur = 1.02 / f
        pd = uu = None
        n = int(s["int_scan_avg"])
        for _ in range(n):
            t, p, u = self.ctl.capture("in2", "asg1", dur, trigger="asg1")
            pd = p if pd is None else pd + p
            uu = u if uu is None else uu + u
        pd, uu = pd / n, uu / n
        m = min(len(t), int(np.searchsorted(t, 1.0 / f)))
        t, pd, uu = t[:m], pd[:m], uu[:m]
        sc = analyze_scan(uu, pd, s, self.A.binned)
        self.pd = float(np.mean(pd))
        self._publish(traces=dict(kind="scan", t=t, u=uu, pd=pd), scan_live=sc)

    def _probe_due(self):
        return self._tick % max(int(self.s.get("drive_every", 3)), 1) == 0

    MIN_FRAME_S = 1 / 15                 # live frames at most 15/s (the browser draws ~10/s)

    def _live_frame(self):
        s = self.s
        if self._t_last is not None:
            rest = self.MIN_FRAME_S - (time.time() - self._t_last)
            if rest > 0:
                self.ctl._wait(rest)
        dur = s["int_window_us"] * 1e-6
        t, pd, out = self.ctl.capture("in2", "pid2", dur)
        self._tick += 1
        pdm, drv = float(np.mean(pd)), float(np.mean(out))
        now = time.time()
        a, b = s["int_probe_a"], s["int_probe_b"]
        probe = None
        real = {x for x in (a, b) if x != "off"}
        if "traces" in s["int_panels"]:
            if real <= {"in2", "pid2"}:
                m = {"in2": pd, "pid2": out}
                probe = dict(t=t, a=m.get(a), b=m.get(b), a_name=a, b_name=b)
            elif self._probe_due() or self.probe is None or (self.probe.get("a_name"), self.probe.get("b_name")) \
                    != (a, b):
                tp, ya, yb = self.ctl.capture(a if a != "off" else "in2", b if b != "off" else "in2", dur)
                probe = dict(t=tp, a=None if a == "off" else ya, b=None if b == "off" else yb, a_name=a, b_name=b)
        self.history.append((now, pdm, drv, self.mode))
        self._publish(traces=dict(kind="time", t=t, pd=pd, out=out), pd=pdm, drive=drv,
                      **({"probe": probe} if probe is not None else {}))
        self.watchdog(pdm, drv, main=True)

    def bg_check(self):
        """Watchdog from register reads only (no scope), while the other loop has the scope or runs a sequence."""
        if self.mode not in ("locked", "nolight"):
            return
        try:
            drv = float(self.r.pid2.ival)
            pd = None
            if self.has_sampler:
                pd = float(np.mean([float(self.r.sampler.in2) for _ in range(3)]))
            self.watchdog(pd, drv, main=False)
        except Exception as e:
            if not self._warned_bg:
                self._warned_bg = True
                self.log(f"background check failed: {e!r}")

    # ---- lock ----
    def gains_for(self, frac, K):
        s = self.s
        if s["int_gain_mode"] == "manual":
            sg = -math.copysign(1.0, K)
            return sg * frac * float(s["int_p"]), sg * frac * float(s["int_i_hz"])
        i = -frac * s["int_ugf_khz"] * 1e3 / K
        return i / (s["int_corner_khz"] * 1e3), i

    def set_gains(self, frac):
        K = self.lock_info["K"]
        p, i = self.gains_for(frac, K)
        p = max(min(p, ISCH.P_MAX), -ISCH.P_MAX)
        i = max(min(i, ISCH.I_MAX), -ISCH.I_MAX)
        p2 = self.r.pid2
        p2.i = i
        p2.p = p
        i_hw, p_hw = float(p2.i), float(p2.p)
        self.lock_info.update(i_hw=i_hw, p_hw=p_hw, ugf_actual=abs(i_hw * K), p_req=p, i_req=i)
        return i_hw, p_hw

    def clamp(self):
        s = self.s
        lim = s["int_out2_limit_mv"] * 1e-3
        sc = self.scan
        if s["int_clamp_auto"] and sc and sc.get("ok"):
            lo, hi = sc["clamp"]
        else:
            lo, hi = sorted((s["int_clamp_lo_mv"] * 1e-3, s["int_clamp_hi_mv"] * 1e-3))
        return max(lo, -lim), min(hi, lim)

    def apply_clamp(self):
        lo, hi = self.clamp()
        p2 = self.r.pid2
        p2.min_voltage, p2.max_voltage = lo, hi
        self.lock_info["clamp"] = (lo, hi)

    def engage(self, u0=None, why="LOCK"):
        """asg1 off -> setpoint -> clamp -> park at the scan's OUT2 for the setpoint -> gains in steps."""
        s, r = self.s, self.r
        sc = self.scan_live if (self.mode == "scanning" and self.scan_live and self.scan_live.get("ok")) else self.scan
        if self.mode == "scanning":
            self.scan_off()
            sc = self.scan
        if not sc or not sc.get("ok"):
            self.log(f"{why} refused: run a power scan first (Scan on, then off)")
            return False
        if sc.get("t") and time.time() - sc["t"] > 3600 and u0 is None:
            self.log(f"note: the power scan is {(time.time()-sc['t'])/3600:.1f} h old")
        sc.update(setpoint_info(sc, s["int_setpoint_mv"] * 1e-3))
        K = sc.get("K") if u0 is None else (self.lock_info.get("K") or sc.get("K"))
        if not K:
            self.log(f"{why} refused: {sc.get('sp_note', 'no plant gain at the setpoint')} "
                     f"(max lockable {sc['max_lock']*1e3:.0f} mV)")
            return False
        lo, hi = self.clamp()
        u_start = sc["u_sp"] if u0 is None else u0
        u_start = min(max(u_start, lo), hi)
        prev = self.mode
        self.mode = "locking"
        a1, p2 = r.asg1, r.pid2
        a1.output_direct = "off"
        a1.amplitude = 0
        p2.p, p2.i = 0, 0
        ISCH.w_setpoint(self)
        p2.min_voltage, p2.max_voltage = lo, hi
        p2.input = "in2"
        p2.ival = u_start
        p2.output_direct = "out2"
        self.lock_info = dict(K=K, u_sp=u_start, clamp=(lo, hi), t0=time.time())
        self._good.clear()
        self._good.append(u_start)
        self.log(f"{why}: clamp {lo*1e3:+.1f}..{hi*1e3:+.1f} mV, park at {u_start*1e3:+.2f} mV, K {K:+.2f} V/V")
        steps = [0.1, 1 / 3, 1.0] if s["int_gain_ramp"] else [1.0]
        for fr in steps:
            i_hw, p_hw = self.set_gains(fr)
            if i_hw == 0 and p_hw == 0:
                p2.p, p2.i = 0, 0
                self.mode = "held"
                self.log("  the gains round to 0 on the hardware: raise the bandwidth / manual gains")
                return False
            self.ctl._wait(0.03)
            iv = float(p2.ival)
            if iv <= lo + 0.02 * (hi - lo) or iv >= hi - 0.02 * (hi - lo):
                p2.p, p2.i = 0, 0
                p2.ival = u_start
                self.mode = "railed"
                self.log(f"  ran to the clamp while engaging ({iv*1e3:+.1f} mV): setpoint unreachable or wrong K "
                         f"sign. Holding at {u_start*1e3:+.2f} mV")
                return False
        li = self.lock_info
        self.log(f"  p {li['p_hw']:+.4g}, i {li['i_hw']:+.4g} Hz -> |i·K| = {li['ugf_actual']/1e3:.2f} kHz; LOCKED")
        self._cap_warning()
        self.mode = "locked"
        self._rail_since, self._dark_n, self._light_since = None, 0, None
        del prev
        return True

    def unlock(self, why):
        p2 = self.r.pid2
        p2.p, p2.i = 0, 0
        if self.mode != "scanning":
            self.mode = "held" if p2.output_direct == "out2" else "off"
        self.log(f"{why}: loop open, drive held at {float(p2.ival)*1e3:+.2f} mV")

    def _hold_good(self, mode, why):
        p2 = self.r.pid2
        p2.p, p2.i = 0, 0
        u = float(np.median(self._good)) if self._good else self.lock_info.get("u_sp", 0.0)
        p2.ival = u
        self.mode = mode
        self.history.append((time.time(), self.pd or 0.0, u, mode))
        self.log(f"{why}: holding the last good drive {u*1e3:+.2f} mV")

    def watchdog(self, pd, drv, main):
        s = self.s
        now = time.time()
        sp = s["int_setpoint_mv"] * 1e-3
        thr = s["int_light_frac"] * sp
        if self.mode == "nolight":
            if pd is not None and pd >= thr:
                self._light_since = self._light_since or now
                if main and s["int_auto_reengage"] and now - self._light_since >= 0.2:
                    self.log("light is back: re-engaging")
                    self.engage(u0=float(self.r.pid2.ival), why="RE-ENGAGE")
            else:
                self._light_since = None
            return
        if self.mode != "locked":
            return
        if pd is not None and pd < thr:
            self._dark_n += 1
            if self._dark_n >= 2:
                self._hold_good("nolight", f"NO LIGHT (photodiode {pd*1e3:.0f} mV < {thr*1e3:.0f} mV)")
                self._light_since = None
            return
        if pd is not None:
            self._dark_n = 0
        lo, hi = self.lock_info.get("clamp", (-1.0, 1.0))
        at = drv <= lo + 0.02 * (hi - lo) or drv >= hi - 0.02 * (hi - lo)
        if at:
            self._rail_since = self._rail_since or now
            if now - self._rail_since >= s["int_rail_s"]:
                self._hold_good("railed", f"RAILED: output at the clamp ({drv*1e3:+.1f} mV) for "
                                          f">{s['int_rail_s']:g} s")
        else:
            self._rail_since = None
            self._good.append(drv)

    # ---- swept-sine Bode plot (asg1 on OUT2 + scope) ----
    def _bode_publish(self, B):
        B = dict(B, curves=[dict(cv, f=list(cv["f"]), c=list(cv["c"])) for cv in B["curves"]])
        with self.ctl._pub_lock:
            self.bode = B
            self.bode_seq += 1

    def measure_bode(self):
        """Closed loop (locked): asg1 adds a small sine on OUT2; the scope records it with the servo output
        (pid2) and with the photodiode (in2). c/x = T gives G = -T/(1+T), S = 1+T, closed loop -T and the plant
        (in2/x)/S."""
        s, r, A = self.s, self.r, self.A
        if self.mode != "locked":
            self.log("Bode works while locked")
            return
        li = self.lock_info
        K = li["K"]
        lo, hi = li["clamp"]
        iv = float(r.pid2.ival)
        sp = s["int_setpoint_mv"] * 1e-3
        amp = float(np.clip(s["int_bode_amp_frac"] * sp / abs(K), 2e-4, 0.4 * min(iv - lo, hi - iv)))
        if amp <= 2e-4 * 1.01 and 0.4 * min(iv - lo, hi - iv) < 2e-4:
            self.log("Bode refused: the drive sits at the clamp")
            return
        fs = np.geomspace(s["int_bode_start_hz"], max(s["int_bode_stop_hz"], 2 * s["int_bode_start_hz"]),
                          int(s["int_bode_points"]))
        p_hw, i_hw = float(r.pid2.p), float(r.pid2.i)
        filt = self.filt_list()
        delay = ISCH.RP_DELAY + s["int_aom_delay_us"] * 1e-6
        Gm = loop_model(fs, K, p_hw, i_hw, filt, delay, s["int_pd_bw_mhz"] * 1e6)
        Hm = np.exp(-2j * math.pi * fs * delay) / (1 + 1j * fs / (s["int_pd_bw_mhz"] * 1e6))
        B = A.bode_init("closed loop", fs, [
            ("G open loop", "#1f5fa8", Gm), ("S = 1/(1+G): disturbance suppression", "#c0392b", 1 / (1 + Gm)),
            ("T = G/(1+G): closed loop", "#1e8e3e", Gm / (1 + Gm)), ("plant P / K", "#d35400", Hm)],
            title=f"Intensity loop Bode (sine {amp*1e3:.2f} mV on OUT2, |i·K| {abs(i_hw*K)/1e3:.1f} kHz)",
            note=f"measured: solid with markers; model (K {K:+.2f} V/V, p {p_hw:+.3g}, i {i_hw:+.4g} Hz, delay "
                 f"{delay*1e6:.2f} us, photodiode pole {s['int_pd_bw_mhz']:g} MHz): dotted")
        self._bode_publish(B)
        self.ctl._stop_meas.clear()
        if "Bode (swept)" not in s["int_panels"]:
            s["int_panels"] = [p for p in I_PANELS if p in s["int_panels"] or p == "Bode (swept)"]
            self.bump()
        self.log(f"Bode: {len(fs)} points {fs[0]:.3g}-{fs[-1]:.3g} Hz, sine {amp*1e3:.2f} mV on OUT2")
        self.mode = "measuring"
        a1 = r.asg1
        try:
            for k, f in enumerate(fs):
                if self.ctl._stop_meas.is_set():
                    self.log(f"Bode stopped after {k} points")
                    break
                self.meas_progress = f"Bode {k+1}/{len(fs)} at {f:.3g} Hz"
                a1.setup(waveform="sin", frequency=float(f), amplitude=amp, offset=0.0, trigger_source="immediately",
                         output_direct="out2")
                with self.ctl._pub_lock:
                    self.hw.update({"asg1.frequency": float(f), "asg1.amplitude": amp, "asg1.output_direct": "out2",
                                    "asg1.waveform": "sin"})
                self.ctl._wait(min(max(5 / f, 2e-4), 0.05))
                dur = float(np.clip(s["int_bode_cycles"] / f, 1e-4, 0.5))
                t, x, c = self.ctl.capture("asg1", "pid2", dur)
                X, C = A.fit_sine(t, x, f), A.fit_sine(t, c, f)
                t, x2, e = self.ctl.capture("asg1", "in2", dur)
                X2, E = A.fit_sine(t, x2, f), A.fit_sine(t, e, f)
                iv = float(r.pid2.ival)
                if iv <= lo + 0.02 * (hi - lo) or iv >= hi - 0.02 * (hi - lo):
                    a1.output_direct = "off"
                    self.mode = "locked"
                    self._hold_good("railed", f"Bode: the output reached the clamp at {f:.3g} Hz; stopped")
                    break
                T = C / X
                G, S = -T / (1 + T), 1 + T
                P = (E / X2) / S
                for cv, val in zip(B["curves"], (G, S, -T, P / K)):
                    cv["f"].append(float(f))
                    cv["c"].append(val)
                fu, pm = A.margins(np.array(B["curves"][0]["f"]), np.array(B["curves"][0]["c"])) \
                    if len(B["curves"][0]["f"]) > 1 else (None, None)
                B.update(ugf=fu, pm=pm, n=k + 1)
                self._bode_publish(dict(B))
        finally:
            a1.output_direct = "off"
            a1.amplitude = 0
            self.meas_progress = ""
            if self.mode == "measuring":
                self.mode = "locked"
            self._bode_publish(dict(B, done=True))
        fu, pm = B.get("ugf"), B.get("pm")
        self.log("Bode: " + (f"unity gain {fu/1e3:.1f} kHz, phase margin {pm:.0f} deg" if fu else
                             "no unity-gain crossing in the measured range"))
