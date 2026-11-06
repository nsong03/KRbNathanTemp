#!/usr/bin/env python3
"""
lockin_pi_app.py -- dual-phase lock-in + raw PI + ramp on IN1/OUT1, and a raw-PI intensity servo on OUT2.

LOCK (IN1 -> OUT1)
  IN1  <- photodiode
  iq2  =  dual-phase lock-in: dither (NCO, to OUT1) and demodulation of IN1 into X ('iq2') and Y ('iq2_2'),
          one high-pass, one low-pass (1-4 stages) and one integer gain for both. The phase is set by hand.
  pid0 =  PI on X or Y (your choice):  e = (X or Y, optionally filtered) - setpoint,
          out = p * e + ival,  d(ival)/dt = 2 pi i e  (i in Hz), clamped. Raw p and i only: nothing is derived.
  asg0 =  OUT1 offset under the PI, or (Ramp toggle) a ramp around the offset for acquiring the signals, PI off.
  OUT1 =  offset (or ramp) + PI + dither
INTENSITY (-> OUT2)
  pid2 =  PI on IN2 (or IN1, setting int_input):  same raw p / i / setpoint / clamps / optional filter -> OUT2.
          Servo off: pid2 output off, OUT2 = 0 V. It starts off unless it is already running (then it is adopted).

The filters in front of each PI (4 first-order stages: > 0 low-pass, < 0 high-pass, 0 off) are switched on and off
with their Filter toggles; they are off by default. The FPGA rounds p, i and the filter corners: the boxes show the
values it really uses.

Run (close the other apps first: one owner per Red Pitaya)
  pip install "dash>=2.9" scipy
  python lockin_pi_app.py --host rp-f0fa92.local          -> http://127.0.0.1:8085
Settings: lockin_pi_settings.json next to this file (Save / Load).
"""
import argparse
import collections
import json
import os
import queue
import sys
import threading
import time
import webbrowser

import numpy as np

import pi_app as PA                         # envelope and the scope capture code (no hardware on import)
from errsig2ph_app import xy_theta          # display only: the signal angle in the X-Y plane

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "lockin_pi_settings.json")
LIM = 1.0

FIELDS = [   # key, label, default, group, kind, options
    # lock-in (iq2)
    ("dither_hz", "Dither frequency [Hz]", 1000.0, "Lock-in", "num", {"min": 1}),
    ("dither_mvpp", "Dither amplitude on OUT1 [mVpp]", 30.0, "Lock-in", "num", {"min": 0}),
    ("phase_deg", "Phase [deg]", 0.0, "Lock-in", "num", {}),
    ("lpf_hz", "Low-pass corner [Hz]", 250.0, "Lock-in", "num", {"min": 1}),
    ("lpf_order", "Low-pass order", 2, "Lock-in", "select", {"options": [1, 2, 3, 4]}),
    ("ac_hz", "Input high-pass [Hz] (0 = off)", 100.0, "Lock-in", "num", {"min": 0}),
    ("lia_gain", "Gain (integer)", 1, "Lock-in", "num", {"min": 1}),
    # lock PI (pid0)
    ("pi_input", "PI input", "X", "Lock PI", "select", {"options": ["X", "Y"]}),
    ("p", "P (raw)", 0.0, "Lock PI", "num", {}),
    ("i_hz", "I (raw) [Hz]", 0.0, "Lock PI", "num", {}),
    ("setpoint_mv", "Setpoint [mV]", 0.0, "Lock PI", "num", {}),
    ("clamp_lo_mv", "Output min [mV]", -1000.0, "Lock PI", "num", {"min": -1000, "max": 1000}),
    ("clamp_hi_mv", "Output max [mV]", 1000.0, "Lock PI", "num", {"min": -1000, "max": 1000}),
    ("filt_on", "Filter on", False, "Lock PI", "bool", {}),
    ("filt1_hz", "Filter stage 1 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt2_hz", "Filter stage 2 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt3_hz", "Filter stage 3 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt4_hz", "Filter stage 4 [Hz]", 0.0, "Lock filter", "num", {}),
    # ramp (asg0)
    ("offset_mv", "Offset / ramp centre [mV]", 0.0, "Ramp", "num", {"min": -1000, "max": 1000}),
    ("ramp_hz", "Ramp frequency [Hz]", 20.0, "Ramp", "num", {"min": 0.1}),
    ("ramp_mvpp", "Ramp amplitude [mVpp]", 500.0, "Ramp", "num", {"min": 0, "max": 2000}),
    ("ramp_shape", "Ramp shape", "triangle", "Ramp", "select", {"options": ["triangle", "sawtooth"]}),
    # intensity PI (pid2)
    ("int_input", "Input", "in2", "Intensity PI", "select", {"options": ["in2", "in1"]}),
    ("int_p", "P (raw)", 0.0, "Intensity PI", "num", {}),
    ("int_i_hz", "I (raw) [Hz]", 0.0, "Intensity PI", "num", {}),
    ("int_setpoint_mv", "Setpoint [mV]", 0.0, "Intensity PI", "num", {}),
    ("int_clamp_lo_mv", "Output min [mV]", -1000.0, "Intensity PI", "num", {"min": -1000, "max": 1000}),
    ("int_clamp_hi_mv", "Output max [mV]", 1000.0, "Intensity PI", "num", {"min": -1000, "max": 1000}),
    ("int_filt_on", "Filter on", False, "Intensity PI", "bool", {}),
    ("int_filt1_hz", "Filter stage 1 [Hz]", 0.0, "Intensity filter", "num", {}),
    ("int_filt2_hz", "Filter stage 2 [Hz]", 0.0, "Intensity filter", "num", {}),
    ("int_filt3_hz", "Filter stage 3 [Hz]", 0.0, "Intensity filter", "num", {}),
    ("int_filt4_hz", "Filter stage 4 [Hz]", 0.0, "Intensity filter", "num", {}),
    # scope
    ("live_ms", "Lock live trace [ms]", 20.0, "Scope", "num", {"min": 0.1}),
    ("int_live_ms", "Intensity live trace [ms]", 5.0, "Scope", "num", {"min": 0.01}),
    ("xaxis", "Ramp plot x axis", "time", "Scope", "select", {"options": ["time", "OUT1"]}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Scope", "num", {"min": 1, "max": 30}),
]
KEYS = [f[0] for f in FIELDS if f[4] != "bool"]          # the bool keys are toggles (switch commands)
DEFAULTS = {f[0]: f[2] for f in FIELDS}
FILT = ["filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz"]
IFILT = ["int_" + k for k in FILT]
LOCKIN_KEYS = {"dither_hz", "dither_mvpp", "phase_deg", "lpf_hz", "lpf_order", "ac_hz", "lia_gain"}
PID_KEYS = {"pi_input", "p", "i_hz", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv"} | set(FILT)
RAMP_KEYS = {"offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"}
INT_KEYS = {"int_input", "int_p", "int_i_hz", "int_setpoint_mv", "int_clamp_lo_mv", "int_clamp_hi_mv"} | set(IFILT)
QUAD = {"X": "iq2", "Y": "iq2_2"}


class LockinPIController:
    """Owns the Red Pitaya; runs in the main thread. Lock modes: ramp, dc (offset only), pi, off (OUT1 = 0 V).
    Intensity: int_on (pid2 -> OUT2) or off (OUT2 = 0 V)."""

    capture = PA.PIController.capture
    _scope_set = PA.PIController._scope_set

    def __init__(self, host, config, settings):
        self.s = dict(settings)
        self.cmds = queue.Queue()
        self._lock = threading.Lock()
        self.logs = collections.deque(maxlen=300)
        self.version = 0
        self.seq = 0
        self.mode = "connecting"
        self.dither_on = True
        self.int_on = False
        self.trace = self.itrace = None
        self.hw = {}
        self.ival = self.int_ival = None
        self.theta = None
        self.fps = 0.0
        self._t_last = None
        self._scope_cfg = {}
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("iq2", "pid0", "pid2", "asg0", "scope"):
            try:
                getattr(self.r, name).owner = "lockin_pi_app"
            except Exception:
                pass
        for name in ("iq0", "iq1", "pid1", "asg1"):                 # OUT1 and OUT2 are this app's alone
            try:
                m = getattr(self.r, name)
                was = m.output_direct
                if was != "off":
                    m.output_direct = "off"
                    self.log(f"{name} was routed to {was}: switched off")
            except Exception:
                pass
        self.log("connected: lock IN1 -> iq2 (X, Y) -> pid0 -> OUT1 (+ asg0 offset/ramp, iq2 dither); "
                 "intensity pid2 -> OUT2")
        p0, p2 = self.r.pid0, self.r.pid2
        if p0.input in ("iq2", "iq2_2") and p0.output_direct == "out1" and (p0.p != 0 or p0.i != 0):
            self._adopt_lock()
        else:
            self._start_ramp()
        if p2.output_direct == "out2" and (p2.p != 0 or p2.i != 0):
            self._adopt_int()
        else:
            p2.output_direct = "off"
            p2.ival = 0
            self._write_int()
            self.log("intensity servo OFF at start-up: OUT2 = 0 V")

    # ---- utilities ----
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def submit(self, cmd, arg=None):
        self.cmds.put((cmd, arg))

    def snapshot(self):
        with self._lock:
            return dict(mode=self.mode, trace=self.trace, itrace=self.itrace, logs=list(self.logs),
                        settings=dict(self.s), version=self.version, seq=self.seq, hw=dict(self.hw), ival=self.ival,
                        int_ival=self.int_ival, fps=self.fps, int_on=self.int_on, dither_on=self.dither_on,
                        theta=self.theta)

    def switches(self):
        return dict(output=self.mode != "off", ramp=self.mode == "ramp", pi=self.mode == "pi",
                    dither=self.dither_on, filt=bool(self.s["filt_on"]), int_on=self.int_on,
                    int_filt=bool(self.s["int_filt_on"]))

    def _readback(self):
        r, hw = self.r, {}
        for mod, attrs in (("iq2", ("frequency", "amplitude", "phase", "bandwidth", "acbandwidth",
                                    "quadrature_factor", "output_direct")),
                           ("pid0", ("input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter",
                                     "output_direct")),
                           ("pid2", ("input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter",
                                     "output_direct")),
                           ("asg0", ("waveform", "frequency", "amplitude", "offset", "output_direct"))):
            m = getattr(r, mod)
            for a in attrs:
                try:
                    v = getattr(m, a)
                    hw[f"{mod}.{a}"] = [float(x) for x in np.atleast_1d(v)] if a in ("inputfilter", "bandwidth") \
                        else v
                except Exception:
                    hw[f"{mod}.{a}"] = None
        with self._lock:
            self.hw = hw

    def _snapped(self, new):
        if new:
            self.log("hardware values: " + ", ".join(f"{k} {self.s[k]:g} -> {v:g}" for k, v in new.items()))
            self.s.update(new)
            self.version += 1

    # ---- writers ----
    def _write_lockin(self):
        s, iq = self.s, self.r.iq2
        iq.input = "in1"
        iq.gain = 0
        iq.output_signal = "quadrature"
        iq.frequency = s["dither_hz"]
        iq.amplitude = s["dither_mvpp"] * 1e-3 / 2
        iq.phase = s["phase_deg"]
        iq.bandwidth = [s["lpf_hz"]] * max(1, min(int(s["lpf_order"]), 4))
        iq.acbandwidth = s["ac_hz"]
        iq.quadrature_factor = s["lia_gain"]
        self._write_dither()
        new = {}
        bw = [float(b) for b in np.atleast_1d(iq.bandwidth) if b > 0]
        if bw and abs(bw[0] - s["lpf_hz"]) > 1e-6 * s["lpf_hz"]:
            new["lpf_hz"] = round(bw[0], 3)
        if abs(float(iq.acbandwidth) - s["ac_hz"]) > 1e-6 * max(s["ac_hz"], 1):
            new["ac_hz"] = round(float(iq.acbandwidth), 3)
        if abs(float(iq.quadrature_factor) - s["lia_gain"]) > 1e-9:
            new["lia_gain"] = int(round(float(iq.quadrature_factor)))
        self._snapped(new)

    def _write_dither(self):
        self.r.iq2.output_direct = "out1" if (self.dither_on and self.mode != "off" and self.s["dither_mvpp"] > 0) \
            else "off"

    def _write_pi(self, p, keys, inp):
        """Raw PI settings into pid module p (not its output routing). keys: (p, i, setpoint, lo, hi, filt_on, filt)."""
        s = self.s
        kp, ki, ksp, klo, khi, kon, kf = keys
        p.input = inp
        lo, hi = sorted((s[klo] * 1e-3, s[khi] * 1e-3))
        p.min_voltage, p.max_voltage = max(lo, -LIM), min(hi, LIM)
        p.setpoint = s[ksp] * 1e-3
        p.inputfilter = [s[k] for k in kf] if s[kon] else [0, 0, 0, 0]
        p.p = s[kp]
        p.i = s[ki]
        new = {}
        if abs(float(p.p) - s[kp]) > 1e-12:
            new[kp] = float(p.p)
        if abs(float(p.i) - s[ki]) > 1e-9:
            new[ki] = float(p.i)
        if s[kon]:
            hwf = [float(x) for x in p.inputfilter] + [0.0] * 4
            for k, v in zip(kf, hwf):
                if abs(v - s[k]) > 1e-6 * max(abs(s[k]), 1.0):
                    new[k] = round(v, 3)
        self._snapped(new)

    def _write_pid(self):
        self._write_pi(self.r.pid0, ("p", "i_hz", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv", "filt_on", FILT),
                       QUAD[self.s["pi_input"]])

    def _write_int(self):
        self._write_pi(self.r.pid2, ("int_p", "int_i_hz", "int_setpoint_mv", "int_clamp_lo_mv", "int_clamp_hi_mv",
                                     "int_filt_on", IFILT), self.s["int_input"])

    def _ramp_limits(self):
        s = self.s
        lim = LIM - (s["dither_mvpp"] * 1e-3 / 2 if self.dither_on else 0)
        c, pp = s["offset_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3
        return max(c - pp / 2, -lim), min(c + pp / 2, lim)

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

    # ---- lock modes ----
    def _start_ramp(self):
        self._pid_off()
        self.mode = "ramp"
        self._write_lockin()
        self._write_pid()
        self._write_asg()
        lo, hi = self._ramp_limits()
        self.log(f"RAMP: OUT1 {self.s['ramp_shape']} {lo*1e3:+.0f}..{hi*1e3:+.0f} mV at {self.s['ramp_hz']:g} Hz "
                 f"+ dither (PI off)")

    def _start_dc(self):
        self._pid_off()
        self.mode = "dc"
        self._write_asg()
        self._write_dither()
        self.log(f"OUT1 = offset {self.s['offset_mv']:+.1f} mV + dither (PI off)")

    def _start_pi(self):
        p0, s = self.r.pid0, self.s
        self._pid_off()
        self.mode = "pi"
        self._write_asg()
        self._write_dither()
        self._write_pid()
        p0.ival = 0
        p0.output_direct = "out1"
        self.log(f"LOCK PI ON: OUT1 = {s['offset_mv']:+.1f} mV + PI({s['pi_input']} - {s['setpoint_mv']:+.1f} mV) "
                 f"+ dither; p = {s['p']:g}, i = {s['i_hz']:g} Hz, filter {'on' if s['filt_on'] else 'off'}")
        if s["p"] == 0 and s["i_hz"] == 0:
            self.log("  note: p = i = 0, the PI outputs 0")

    def _adopt_lock(self):
        p0, iq, a, s = self.r.pid0, self.r.iq2, self.r.asg0, self.s
        s.update(pi_input="Y" if p0.input == "iq2_2" else "X", p=float(p0.p), i_hz=float(p0.i),
                 setpoint_mv=float(p0.setpoint) * 1e3, clamp_lo_mv=float(p0.min_voltage) * 1e3,
                 clamp_hi_mv=float(p0.max_voltage) * 1e3)
        f = [float(x) for x in p0.inputfilter]
        s["filt_on"] = any(f)
        if any(f):
            s.update({k: round(v, 3) for k, v in zip(FILT, f + [0.0] * 4)})
        bw = [float(b) for b in np.atleast_1d(iq.bandwidth) if b > 0]
        s.update(dither_hz=float(iq.frequency), dither_mvpp=float(iq.amplitude) * 2e3, phase_deg=float(iq.phase),
                 ac_hz=float(iq.acbandwidth), lia_gain=int(round(float(iq.quadrature_factor))))
        if bw:
            s.update(lpf_hz=round(bw[0], 3), lpf_order=len(bw))
        self.dither_on = iq.output_direct == "out1"
        s["offset_mv"] = float(a.offset) * 1e3 if (a.output_direct == "out1" and a.amplitude == 0) else 0.0
        self.mode = "pi"
        self.version += 1
        self.log(f"pid0 was locking ({s['pi_input']} -> OUT1, p = {s['p']:g}, i = {s['i_hz']:g} Hz): adopted untouched")

    def _adopt_int(self):
        p2, s = self.r.pid2, self.s
        s.update(int_input=p2.input if p2.input in ("in1", "in2") else s["int_input"], int_p=float(p2.p),
                 int_i_hz=float(p2.i), int_setpoint_mv=float(p2.setpoint) * 1e3,
                 int_clamp_lo_mv=float(p2.min_voltage) * 1e3, int_clamp_hi_mv=float(p2.max_voltage) * 1e3)
        f = [float(x) for x in p2.inputfilter]
        s["int_filt_on"] = any(f)
        if any(f):
            s.update({k: round(v, 3) for k, v in zip(IFILT, f + [0.0] * 4)})
        self.int_on = True
        self.version += 1
        self.log(f"pid2 was running ({p2.input} -> OUT2, p = {s['int_p']:g}, i = {s['int_i_hz']:g} Hz): adopted "
                 f"untouched")

    # ---- intensity ----
    def _int_switch(self, on):
        p2, s = self.r.pid2, self.s
        if on and not self.int_on:
            p2.output_direct = "off"
            self._write_int()
            p2.ival = 0
            p2.output_direct = "out2"
            self.int_on = True
            self.log(f"INTENSITY PI ON: OUT2 = PI({s['int_input']} - {s['int_setpoint_mv']:+.1f} mV), "
                     f"p = {s['int_p']:g}, i = {s['int_i_hz']:g} Hz, filter {'on' if s['int_filt_on'] else 'off'}")
        elif not on and self.int_on:
            p2.output_direct = "off"
            p2.ival = 0
            self.int_on = False
            self.log("intensity PI OFF: OUT2 = 0 V")

    # ---- scope ----
    def _tick(self):
        s = self.s
        if self.mode == "ramp":
            dur = 1.0 / s["ramp_hz"]
            t, pd, o = self.capture("in1", "out1", dur, trigger="asg0")
            _, x, y = self.capture("iq2", "iq2_2", dur, trigger="asg0")
            n = min(int(np.searchsorted(t, dur)), len(x))
            t, pd, o, x, y = t[:n], pd[:n], o[:n], x[:n], y[:n]
            kind = "ramp"
            th, q = xy_theta(x, y)
            theta = dict(theta=th, q=q) if th is not None else None
        else:
            dur = s["live_ms"] * 1e-3
            t, pd, o = self.capture("in1", "out1", dur)
            _, x, y = self.capture("iq2", "iq2_2", dur)
            kind = "live"
            theta = None
        ti, vi, oi = self.capture(s["int_input"], "out2", s["int_live_ms"] * 1e-3)
        it = dict(t=ti, inp=vi, out2=oi)
        iv = jv = None
        try:
            if self.mode == "pi":
                iv = float(self.r.pid0.ival)
            if self.int_on:
                jv = float(self.r.pid2.ival)
        except Exception:
            pass
        now = time.time()
        with self._lock:
            self.trace = dict(kind=kind, t=t, pd=pd, out1=o, x=x, y=y)
            self.itrace = it
            self.ival, self.int_ival, self.theta = iv, jv, theta
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
            if changed & LOCKIN_KEYS:
                self._write_lockin()
                if self.mode == "ramp" and changed & {"dither_mvpp"}:
                    self._write_asg()
            if changed & PID_KEYS:
                self._write_pid()
            if changed & RAMP_KEYS and self.mode != "off":
                self._write_asg()
            if changed & INT_KEYS:
                self._write_int()
            if changed - {"ui_hz", "xaxis", "live_ms", "int_live_ms"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "switch":
            self._switch(*arg)
        elif cmd == "reset_i":
            self.r.pid0.ival = 0
            self.log("lock integrator reset")
        elif cmd == "int_reset_i":
            self.r.pid2.ival = 0
            self.log("intensity integrator reset")
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            self._write_lockin()
            self._write_pid()
            self._write_int()
            if self.mode != "off":
                self._write_asg()
            self.version += 1
            self.log("settings loaded")

    def _switch(self, name, on):
        on = bool(on)
        s = self.s
        if name == "filt":
            s["filt_on"] = on
            self._write_pid()
            self.version += 1
            self.log(f"lock PI filter {'ON: ' + str([s[k] for k in FILT]) if on else 'off'}")
        elif name == "int_filt":
            s["int_filt_on"] = on
            self._write_int()
            self.version += 1
            self.log(f"intensity PI filter {'ON: ' + str([s[k] for k in IFILT]) if on else 'off'}")
        elif name == "int_on":
            self._int_switch(on)
        elif name == "output":
            if on and self.mode == "off":
                self._start_dc()
            elif not on and self.mode != "off":
                self._pid_off()
                self.mode = "off"
                self._write_asg()
                self._write_dither()
                self.log("lock Output OFF: OUT1 = 0 V (ramp/offset, dither and PI off)")
        elif self.mode == "off":
            self.log(f"{name}: lock Output is off; switch Output on first")
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
        elif name == "dither":
            self.dither_on = on
            self._write_dither()
            if self.mode == "ramp":
                self._write_asg()
            self.log(f"dither {'on' if on else 'off'}")


def main():
    ap = argparse.ArgumentParser(description="Dual-phase lock-in + PI + ramp on IN1/OUT1, intensity PI on OUT2")
    ap.add_argument("--host", default="rp-f0fa92.local", help="Red Pitaya hostname or IP")
    ap.add_argument("--config", default="lockin_pi_app", help="PyRPL config name")
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    ctl = LockinPIController(args.host, args.config, settings)
    import lockin_pi_ui
    app = lockin_pi_ui.build_app(ctl, sys.modules[__name__])
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
