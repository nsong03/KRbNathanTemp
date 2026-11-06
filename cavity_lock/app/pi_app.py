#!/usr/bin/env python3
"""
pi_app.py -- the Red Pitaya as a plain PI servo for an EXTERNAL error signal. IN1 and OUT1 only.

  IN1  <- error signal (from your own lock-in / PDH / servo electronics)
  OUT1 -> actuator (e.g. PZT driver):  OUT1 = offset (asg0) + PI(IN1)  (pid0)
          Ramp toggle (acquisition):  OUT1 = ramp (asg0) around the offset, PI off

The PI is PyRPL's pid0 with its raw settings, nothing derived:
  e = (IN1 through the optional input filter) - setpoint
  pid0 output = p * e + ival,   d(ival)/dt = 2 pi * i * e     (i in Hz), clamped to [min, max]
Signs are yours: set p and i with the sign that gives negative feedback for your error slope.
Input filter: up to 4 first-order stages in front of the PI (> 0 low-pass corner, < 0 high-pass corner, 0 off).
All off by default. The FPGA rounds p, i and the filter corners; the boxes show the values it really uses.
Nothing else on the Red Pitaya is touched, except that other modules routed to OUT1 are switched off at start-up.

Run (in the Python environment where PyRPL works; close the other apps first: one owner per Red Pitaya)
  pip install "dash>=2.9" scipy
  python pi_app.py --host rp-f0fa92.local          -> http://127.0.0.1:8075
Settings: pi_settings.json next to this file (Save / Load).
"""
import argparse
import collections
import json
import math
import os
import queue
import sys
import threading
import time
import webbrowser

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "pi_settings.json")
LIM = 1.0                                   # the DAC range [V]

FIELDS = [   # key, label, default, group, kind, options
    ("p", "P (proportional gain, raw)", 0.0, "PI", "num", {}),
    ("i_hz", "I (integral unity-gain frequency, raw) [Hz]", 0.0, "PI", "num", {}),
    ("setpoint_mv", "Setpoint (subtracted from IN1) [mV]", 0.0, "PI", "num", {}),
    ("clamp_lo_mv", "PI output min [mV]", -1000.0, "PI", "num", {"min": -1000, "max": 1000}),
    ("clamp_hi_mv", "PI output max [mV]", 1000.0, "PI", "num", {"min": -1000, "max": 1000}),
    ("filt1_hz", "Stage 1 [Hz]", 0.0, "Input filter", "num", {}),
    ("filt2_hz", "Stage 2 [Hz]", 0.0, "Input filter", "num", {}),
    ("filt3_hz", "Stage 3 [Hz]", 0.0, "Input filter", "num", {}),
    ("filt4_hz", "Stage 4 [Hz]", 0.0, "Input filter", "num", {}),
    ("offset_mv", "Offset / ramp centre [mV]", 0.0, "Ramp", "num", {"min": -1000, "max": 1000}),
    ("ramp_hz", "Ramp frequency [Hz]", 20.0, "Ramp", "num", {"min": 0.1}),
    ("ramp_mvpp", "Ramp amplitude [mVpp]", 500.0, "Ramp", "num", {"min": 0, "max": 2000}),
    ("ramp_shape", "Ramp shape", "triangle", "Ramp", "select", {"options": ["triangle", "sawtooth"]}),
    ("live_ms", "Live trace length [ms]", 20.0, "Scope", "num", {"min": 0.1}),
    ("xaxis", "Ramp plot x axis", "time", "Scope", "select", {"options": ["time", "OUT1"]}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Scope", "num", {"min": 1, "max": 30}),
]
KEYS = [f[0] for f in FIELDS]
DEFAULTS = {f[0]: f[2] for f in FIELDS}
FILT_KEYS = ["filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz"]
PID_KEYS = {"p", "i_hz", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv"} | set(FILT_KEYS)
RAMP_KEYS = {"offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"}


def envelope(x, y, n=1500):
    """Min/max decimation for display (keeps spikes)."""
    x, y = np.asarray(x), np.asarray(y)
    if len(y) <= 2 * n:
        return x, y
    k = len(y) // n
    m = k * n
    yy = y[:m].reshape(n, k)
    xs = x[:m].reshape(n, k)[:, 0]
    return np.repeat(xs, 2), np.column_stack([yy.min(1), yy.max(1)]).ravel()


class PIController:
    """Owns the Red Pitaya; runs in the main thread (ctl.run). Modes: ramp (OUT1 = ramp), dc (OUT1 = offset),
    pi (OUT1 = offset + PI), off (OUT1 = 0 V)."""

    def __init__(self, host, config, settings):
        self.s = dict(settings)
        self.cmds = queue.Queue()
        self._lock = threading.Lock()
        self.logs = collections.deque(maxlen=300)
        self.version = 0
        self.seq = 0
        self.mode = "connecting"
        self.trace = None
        self.hw = {}
        self.ival = None
        self.fps = 0.0
        self._t_last = None
        self._scope_cfg = {}
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("pid0", "asg0", "scope"):
            try:
                getattr(self.r, name).owner = "pi_app"
            except Exception:
                pass
        for name in ("pid1", "pid2", "asg1", "iq0", "iq1", "iq2"):      # OUT1 is ours alone; OUT2 is left alone
            try:
                m = getattr(self.r, name)
                if m.output_direct in ("out1", "both"):
                    m.output_direct = "off"
                    self.log(f"{name} was routed to OUT1: switched off")
            except Exception:
                pass
        self.log("connected: IN1 = error in, OUT1 = offset + PI (pid0) or the ramp (asg0)")
        p0 = self.r.pid0
        if p0.input == "in1" and p0.output_direct == "out1" and (p0.p != 0 or p0.i != 0):
            self._adopt()
        else:
            self._start_ramp()

    # ---- utilities ----
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def submit(self, cmd, arg=None):
        self.cmds.put((cmd, arg))

    def snapshot(self):
        with self._lock:
            return dict(mode=self.mode, trace=self.trace, logs=list(self.logs), settings=dict(self.s),
                        version=self.version, seq=self.seq, hw=dict(self.hw), ival=self.ival, fps=self.fps)

    def switches(self):
        return dict(output=self.mode != "off", ramp=self.mode == "ramp", pi=self.mode == "pi")

    def _readback(self):
        r, hw = self.r, {}
        for mod, attrs in (("pid0", ("input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter",
                                     "output_direct", "ival")),
                           ("asg0", ("waveform", "frequency", "amplitude", "offset", "output_direct"))):
            m = getattr(r, mod)
            for a in attrs:
                try:
                    v = getattr(m, a)
                    hw[f"{mod}.{a}"] = [float(x) for x in v] if a == "inputfilter" else v
                except Exception:
                    hw[f"{mod}.{a}"] = None
        with self._lock:
            self.hw = hw

    # ---- writers ----
    def _write_pid(self):
        """pid0 settings from s (gains, setpoint, clamps, input filter). Output routing is separate."""
        s, p0 = self.s, self.r.pid0
        p0.input = "in1"
        lo, hi = sorted((s["clamp_lo_mv"] * 1e-3, s["clamp_hi_mv"] * 1e-3))
        p0.min_voltage, p0.max_voltage = max(lo, -LIM), min(hi, LIM)
        p0.setpoint = s["setpoint_mv"] * 1e-3
        p0.inputfilter = [s[k] for k in FILT_KEYS]
        p0.p = s["p"]
        p0.i = s["i_hz"]
        self._snap()

    def _snap(self):
        """Show what the FPGA really uses for p, i and the filter corners."""
        s, p0, new = self.s, self.r.pid0, {}
        if abs(float(p0.p) - s["p"]) > 1e-12:
            new["p"] = float(p0.p)
        if abs(float(p0.i) - s["i_hz"]) > 1e-9:
            new["i_hz"] = float(p0.i)
        hwf = [float(x) for x in p0.inputfilter]
        for k, v in zip(FILT_KEYS, hwf + [0.0] * (4 - len(hwf))):
            if abs(v - s[k]) > 1e-6 * max(abs(s[k]), 1.0):
                new[k] = round(v, 3)
        if new:
            self.log("hardware values: " + ", ".join(f"{k} {s[k]:g} -> {v:g}" for k, v in new.items()))
            s.update(new)
            self.version += 1

    def _ramp_limits(self):
        s = self.s
        c, pp = s["offset_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3
        lo, hi = max(c - pp / 2, -LIM), min(c + pp / 2, LIM)
        return lo, hi

    def _write_asg(self):
        s, a = self.s, self.r.asg0
        if self.mode == "ramp":
            lo, hi = self._ramp_limits()
            a.setup(waveform="ramp" if s["ramp_shape"] == "triangle" else "halframp", frequency=s["ramp_hz"],
                    amplitude=max(hi - lo, 0) / 2, offset=(hi + lo) / 2, trigger_source="immediately",
                    output_direct="out1")
        elif self.mode in ("dc", "pi"):
            a.amplitude = 0
            a.offset = float(np.clip(s["offset_mv"] * 1e-3, -LIM, LIM))
            a.output_direct = "out1"
        else:
            a.output_direct, a.amplitude, a.offset = "off", 0, 0

    def _pid_off(self):
        p0 = self.r.pid0
        p0.output_direct = "off"
        p0.ival = 0

    # ---- modes ----
    def _start_ramp(self):
        self._pid_off()
        self.mode = "ramp"
        self._write_pid()
        self._write_asg()
        lo, hi = self._ramp_limits()
        self.log(f"RAMP: OUT1 {self.s['ramp_shape']} {lo*1e3:+.0f}..{hi*1e3:+.0f} mV at {self.s['ramp_hz']:g} Hz "
                 f"(PI off)")

    def _start_dc(self):
        self._pid_off()
        self.mode = "dc"
        self._write_asg()
        self.log(f"OUT1 = offset {self.s['offset_mv']:+.1f} mV (PI off)")

    def _start_pi(self):
        p0 = self.r.pid0
        self._pid_off()
        self.mode = "pi"
        self._write_asg()                       # ramp off: the offset as DC under the PI
        self._write_pid()
        p0.ival = 0
        p0.output_direct = "out1"
        s = self.s
        self.log(f"PI ON: OUT1 = {s['offset_mv']:+.1f} mV + PI(IN1 - {s['setpoint_mv']:+.1f} mV), "
                 f"p = {s['p']:g}, i = {s['i_hz']:g} Hz, clamp {s['clamp_lo_mv']:+.0f}..{s['clamp_hi_mv']:+.0f} mV, "
                 f"filter {[s[k] for k in FILT_KEYS]}")
        if s["p"] == 0 and s["i_hz"] == 0:
            self.log("  note: p = i = 0, the PI outputs 0")

    def _adopt(self):
        """pid0 already running IN1 -> OUT1 with gains: keep it, read the settings from the registers."""
        p0, a, s = self.r.pid0, self.r.asg0, self.s
        s.update(p=float(p0.p), i_hz=float(p0.i), setpoint_mv=float(p0.setpoint) * 1e3,
                 clamp_lo_mv=float(p0.min_voltage) * 1e3, clamp_hi_mv=float(p0.max_voltage) * 1e3)
        hwf = [float(x) for x in p0.inputfilter] + [0.0] * 4
        for k, v in zip(FILT_KEYS, hwf):
            s[k] = round(v, 3)
        if a.output_direct in ("out1", "both") and a.amplitude == 0:
            s["offset_mv"] = float(a.offset) * 1e3
        else:
            s["offset_mv"] = 0.0
            a.output_direct = "off"
        self.mode = "pi"
        self.version += 1
        self.log(f"pid0 was running IN1 -> OUT1 (p = {s['p']:g}, i = {s['i_hz']:g} Hz): adopted untouched")

    # ---- scope ----
    def _scope_set(self, sc, **kw):
        for k, v in kw.items():
            if self._scope_cfg.get(k) != v:
                try:
                    setattr(sc, k, v)
                except Exception:
                    if k not in ("average", "rolling_mode"):
                        raise
                self._scope_cfg[k] = v

    def capture(self, in1, in2, duration, trigger="immediately"):
        sc = self.r.scope
        opts = sorted(getattr(sc, "durations", []) or [])
        if opts:
            duration = min([d for d in opts if d >= duration * 0.999] or [opts[-1]])
        self._scope_set(sc, input1=in1, input2=in2, duration=duration, average=True, rolling_mode=False,
                        trigger_source=trigger)
        if trigger != "immediately":
            self._scope_set(sc, trigger_delay=duration / 2)
        start = getattr(sc, "_start_trace_acquisition", None) or getattr(sc, "_start_acquisition", None)
        read = getattr(sc, "_get_trace", None) or getattr(sc, "_get_curve", None)
        timeout = 3 * duration + 2.0
        if start and read and hasattr(sc, "curve_ready"):
            start()
            t_arm = time.time()
            time.sleep(max(0.0, duration - 0.002))
            while not sc.curve_ready():
                if time.time() - t_arm > timeout:
                    raise TimeoutError(f"scope did not trigger on '{trigger}'")
                time.sleep(0.001)
            data = np.array(read())
        else:
            try:
                data = np.array(sc.single(timeout=timeout))
            except TypeError:
                data = np.array(sc.single())
        n = data.shape[-1]
        return np.arange(n) * (duration / n), data[0], data[1]

    def _tick(self):
        s = self.s
        if self.mode == "ramp":
            dur = 1.0 / s["ramp_hz"]
            t, e, o = self.capture("in1", "out1", dur, trigger="asg0")
            n = int(np.searchsorted(t, dur))
            t, e, o = t[:n], e[:n], o[:n]
            kind = "ramp"
        else:
            t, e, o = self.capture("in1", "out1", s["live_ms"] * 1e-3)
            kind = "live"
        iv = None
        if self.mode == "pi":
            try:
                iv = float(self.r.pid0.ival)
            except Exception:
                iv = None
        now = time.time()
        with self._lock:
            self.trace = dict(kind=kind, t=t, in1=e, out1=o)
            self.ival = iv
            self.seq += 1
            if self._t_last:
                dt = max(now - self._t_last, 1e-3)
                self.fps = 0.8 * self.fps + 0.2 / dt if self.fps else 1 / dt
            self._t_last = now
        if kind == "live":
            time.sleep(0.02)

    # ---- main loop ----
    def run(self, stop_event):
        self._readback()
        while not stop_event.is_set():
            try:
                while True:
                    cmd, arg = self.cmds.get_nowait()
                    try:
                        self._do(cmd, arg)
                    except Exception as e:
                        self.log(f"ERROR in {cmd}: {e!r}")
                    try:
                        self._readback()
                    except Exception as e:
                        self.log(f"readback error: {e!r}")
            except queue.Empty:
                pass
            try:
                self._tick()
            except Exception as e:
                self.log(f"acquisition error: {e!r}")
                time.sleep(0.5)

    def _do(self, cmd, arg):
        s = self.s
        if cmd == "apply":
            vals, base = arg
            if base != self.version:
                return
            changed = {k for k, v in vals.items() if s.get(k) != v}
            if not changed:
                return
            s.update(vals)
            if changed & PID_KEYS:
                self._write_pid()
            if changed & RAMP_KEYS and self.mode != "off":
                self._write_asg()
            if changed - {"ui_hz", "xaxis", "live_ms"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "switch":
            name, on = arg
            self._switch(name, bool(on))
        elif cmd == "reset_i":
            self.r.pid0.ival = 0
            self.log("integrator reset (ival = 0)")
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            self._write_pid()
            if self.mode != "off":
                self._write_asg()
            self.version += 1
            self.log("settings loaded")

    def _switch(self, name, on):
        if name == "output":
            if on and self.mode == "off":
                self.mode = "dc"
                self._start_dc()
            elif not on and self.mode != "off":
                self._pid_off()
                self.mode = "off"
                self._write_asg()
                self.log("Output OFF: OUT1 = 0 V (ramp/offset and PI off)")
        elif self.mode == "off":
            self.log(f"{name}: Output is off; switch Output on first")
        elif name == "ramp":
            if on and self.mode != "ramp":
                self._start_ramp()
            elif not on and self.mode == "ramp":
                self._start_dc()
        elif name == "pi":
            if on and self.mode != "pi":
                self._start_pi()
            elif not on and self.mode == "pi":
                self._start_dc()


def main():
    ap = argparse.ArgumentParser(description="Red Pitaya PI servo: IN1 error -> OUT1 correction, with a ramp toggle")
    ap.add_argument("--host", default="rp-f0fa92.local", help="Red Pitaya hostname or IP")
    ap.add_argument("--config", default="pi_app", help="PyRPL config name")
    ap.add_argument("--port", type=int, default=8075)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    ctl = PIController(args.host, args.config, settings)
    import pi_ui
    app = pi_ui.build_app(ctl, sys.modules[__name__])
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=args.port, debug=False), daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"app running at {url}", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    stop = threading.Event()
    try:
        ctl.run(stop)
    except KeyboardInterrupt:
        print("\nquitting; the Red Pitaya keeps its present state.")


if __name__ == "__main__":
    main()
