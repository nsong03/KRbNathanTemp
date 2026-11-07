#!/usr/bin/env python3
"""
extref_app.py -- dual-phase lock-in on an EXTERNAL dither reference, raw PI, error output, alignment ramp.

  IN1  <- photodiode
  IN2  <- the external dither sine (5 Vpp: use the HV jumper, +/-20 V range; the FPGA sees IN2 / 20)
  OUT1 -> PZT driver: the PI correction (PI switch) or the alignment ramp (Ramp switch) + the DC offset.
          The dither is NOT made here: the external dither is summed with OUT1 outside the Red Pitaya.
  OUT2 -> the error signal (Error -> OUT2 switch: gain x X or Y + offset, clamped), or the PI (PI -> OUT2).

PyRPL's FPGA (0.9.9.1) can only demodulate against an IQ module's own oscillator (NCO); it cannot multiply IN1 by
IN2. So the external sine is used as a PHASE REFERENCE:
  iq2      demodulates IN1 -> X ('iq2') and Y ('iq2_2'), the error signal, in real time in the FPGA. Its NCO runs at
           the reference frequency `ref_hz`; no dither output (amplitude 0, output off).
  iq0, iq1 demodulate IN2 with the same NCO (synchronized with iq.synchronize_iqs(); iq1 = iq0 + 90 deg): the
           reference's phase relative to the NCO.
  tracking the app reads iq0 / iq1 (r.sampler, one register each) about 20 times a second and shifts the phase of
           all three IQ modules by the same amount so that the reference phase stays where it was ("Track" switch),
           and corrects the NCO frequency when the phase keeps drifting (the NCO resolves 125 MHz / 2^32 = 0.03 Hz).
           So X / Y keep a fixed phase to the external dither even though the two oscillators are independent.
           The direction in which the phase register moves the measured phase is found once (the first lock of
           the reference), so no PyRPL sign convention is assumed.
  "Find f" measures the reference frequency from an IN2 scope trace (FFT peak, refined by the phase advance
           between the two halves of the trace) and sets the NCO to it.
  pid0     raw PI on X or Y (pid.p / pid.i exactly as typed; PyRPL PID widget arrows), filter switch, output clamps
           (min / max), routed to the OUT1 summer (PI switch, exclusive with the ramp) and / or OUT2 (PI -> OUT2).
  pid1     the error signal to OUT2 (I = 0: gain x X or Y + offset, clamped): Error -> OUT2 switch. PI -> OUT2 and
           Error -> OUT2 are exclusive (OUT2 carries one of them).
  asg0     OUT1 offset (DC under the PI) or the alignment ramp.
  scope    as lockin_pi2_app: IN1, X / Y / R, OUT1 (triggered on the ramp, `ramp_periods` periods, or live) + probes.

Run (close the other apps first: one owner per Red Pitaya)
  python extref_app.py --host rp-f0fa92.local          -> http://127.0.0.1:8105
Settings: extref_settings.json next to this file (Save / Load).
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

import pi_app as PA                         # envelope, _scope_set (no hardware on import)
from errsig2ph_app import xy_theta          # display only: the signal angle in the X-Y plane

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "extref_settings.json")
LIM = 1.0
PROBES = ["in1", "in2", "iq2", "iq2_2", "iq0", "iq1", "pid0", "pid1", "asg0", "out1", "out2", "off"]

FIELDS = [   # key, label, default, group, kind, options
    # external reference (iq0 / iq1 on IN2)
    ("ref_hz", "Reference (dither) frequency [Hz]", 1000.0, "Reference", "num", {"min": 0.1}),
    ("ref_lpf_hz", "Reference low-pass [Hz]", 100.0, "Reference", "num", {"min": 1}),
    ("in2_scale", "IN2 scale [V at the connector per FPGA V] (20 = HV jumper)", 20.0, "Reference", "num",
     {"min": 0.01}),
    ("ref_track", "Track the reference phase", True, "Reference", "bool", {}),
    # lock-in (iq2 on IN1)
    ("phase_deg", "Phase [deg]", 0.0, "Lock-in", "num", {}),
    ("lpf_hz", "Low-pass corner [Hz]", 250.0, "Lock-in", "num", {"min": 1}),
    ("lpf_order", "Low-pass order", 2, "Lock-in", "select", {"options": [1, 2, 3, 4]}),
    ("ac_hz", "Input high-pass [Hz] (0 = off)", 100.0, "Lock-in", "num", {"min": 0}),
    ("lia_gain", "Gain (integer)", 1, "Lock-in", "num", {"min": 1}),
    # lock PI (pid0)
    ("pi_input", "PI input", "X", "Lock PI", "select", {"options": ["X", "Y"]}),
    ("p", "P (PyRPL pid.p)", 0.0, "Lock PI", "num", {}),
    ("i", "I (PyRPL pid.i)", 0.0, "Lock PI", "num", {}),
    ("setpoint_mv", "Setpoint [mV]", 0.0, "Lock PI", "num", {}),
    ("clamp_lo_mv", "Output min [mV]", -1000.0, "Lock PI", "num", {"min": -1000, "max": 1000}),
    ("clamp_hi_mv", "Output max [mV]", 1000.0, "Lock PI", "num", {"min": -1000, "max": 1000}),
    ("filt_on", "Filter on", False, "Lock PI", "bool", {}),
    ("pi_out2", "PI output to OUT2", False, "Lock PI", "bool", {}),
    ("filt1_hz", "Filter stage 1 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt2_hz", "Filter stage 2 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt3_hz", "Filter stage 3 [Hz]", 0.0, "Lock filter", "num", {}),
    ("filt4_hz", "Filter stage 4 [Hz]", 0.0, "Lock filter", "num", {}),
    # error output (pid1 -> OUT2)
    ("eo_on", "Error signal to OUT2", False, "Error output", "bool", {}),
    ("eo_source", "Error output source", "X", "Error output", "select", {"options": ["X", "Y"]}),
    ("eo_gain", "Error output gain (pid1.p)", 1.0, "Error output", "num", {}),
    ("eo_offset_mv", "Error output offset [mV]", 0.0, "Error output", "num", {}),
    ("eo_limit_mv", "Error output clamp [+/- mV]", 1000.0, "Error output", "num", {"min": 1, "max": 1000}),
    # ramp (asg0)
    ("offset_mv", "Offset / ramp centre [mV]", 0.0, "Ramp", "num", {"min": -1000, "max": 1000}),
    ("ramp_hz", "Ramp frequency [Hz]", 20.0, "Ramp", "num", {"min": 0.1}),
    ("ramp_mvpp", "Ramp amplitude [mVpp]", 500.0, "Ramp", "num", {"min": 0, "max": 2000}),
    ("ramp_shape", "Ramp shape", "triangle", "Ramp", "select", {"options": ["triangle", "sawtooth"]}),
    # scope
    ("live_ms", "Lock live trace [ms]", 20.0, "Scope", "num", {"min": 0.1}),
    ("xaxis", "Ramp plot x axis", "time", "Scope", "select", {"options": ["time", "OUT1"]}),
    ("ramp_periods", "Ramp periods per trace", 2, "Scope", "num", {"min": 1, "max": 16}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Scope", "num", {"min": 1, "max": 30}),
    ("probe_every", "Probe capture every N frames", 3, "Scope", "num", {"min": 1, "max": 100}),
    ("probe_a", "Probe A", "off", "Scope", "select", {"options": PROBES}),
    ("probe_b", "Probe B", "off", "Scope", "select", {"options": PROBES}),
]
KEYS = [f[0] for f in FIELDS if f[4] != "bool"]          # bool keys are switches (switch commands)
DEFAULTS = {f[0]: f[2] for f in FIELDS}
FILT = ["filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz"]
REF_KEYS = {"ref_hz", "ref_lpf_hz"}
LOCKIN_KEYS = {"phase_deg", "lpf_hz", "lpf_order", "ac_hz", "lia_gain"}
PID_KEYS = {"pi_input", "p", "i", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv"} | set(FILT)
EO_KEYS = {"eo_source", "eo_gain", "eo_offset_mv", "eo_limit_mv"}
RAMP_KEYS = {"offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"}
QUAD = {"X": "iq2", "Y": "iq2_2"}
REF_MIN = 0.002            # reference demod level below this [FPGA V]: "NO REFERENCE"
SERVICE_S = 0.05           # reference tracking period [s]


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def load_settings(d):
    return {k: v for k, v in d.items() if k in DEFAULTS}


def measure_freq(t, y):
    """Frequency [Hz] and peak-peak of a sine trace: FFT peak (parabolic), refined by the phase advance between the
    two halves of the trace."""
    y = np.asarray(y, float) - np.mean(y)
    n, dt = len(y), float(t[-1] - t[0]) / (len(t) - 1)
    w = np.hanning(n)
    F = np.abs(np.fft.rfft(y * w))
    k = int(np.argmax(F[1:])) + 1
    if 1 <= k < len(F) - 1:
        a, b, c = F[k - 1], F[k], F[k + 1]
        k = k + 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else k
    f = k / (n * dt)
    h = n // 2
    tt = np.arange(n) * dt

    def ph(seg):
        A = np.column_stack([np.cos(2 * np.pi * f * tt[seg]), np.sin(2 * np.pi * f * tt[seg])])
        c, s = np.linalg.lstsq(A, y[seg], rcond=None)[0]
        return math.atan2(-s, c)
    d = wrap(math.degrees(ph(slice(h, n)) - ph(slice(0, h))))
    f = f + d / 360.0 / (h * dt)
    return f, float(np.ptp(y))


class ExtRefController:
    """Owns the Red Pitaya; runs in the main thread. Lock modes: ramp, dc (offset only), pi (PI in the OUT1 summer),
    off (OUT1 = 0 V). OUT2: error (pid1), PI (pid0 out2/both) or nothing."""

    _scope_set = PA.PIController._scope_set

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
        self.theta = None
        self.fps = 0.0
        self._t_last = None
        self._scope_cfg = {}
        self.probe = None
        self._probe_last = "b"
        self._frames = 0
        self._long_msg = None
        # reference tracking
        self.corr = 0.0            # phase added to iq0 / iq1 / iq2 [deg]
        self.f_nco = float(self.s["ref_hz"])
        self.ref = dict(level=None, ok=False, theta=None, rate=None, locked=False, find=None)
        self._target = None
        self._sign = None          # d(measured ref phase) / d(phase register) = +1 or -1, found once
        self._fsign = 1.0          # direction of the frequency correction (checked when it is first used)
        self._hist = collections.deque(maxlen=200)     # (t, unwrapped corr)
        self._last_fll = time.time()
        self._fll_prev = None
        self._t_service = 0.0
        self._busy = False
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("iq0", "iq1", "iq2", "pid0", "pid1", "asg0", "scope"):
            try:
                getattr(self.r, name).owner = "extref_app"
            except Exception:
                pass
        for name in ("pid2", "asg1"):                 # OUT1 and OUT2 are this app's alone
            try:
                m = getattr(self.r, name)
                was = m.output_direct
                if was != "off":
                    m.output_direct = "off"
                    self.log(f"{name} was routed to {was}: switched off")
            except Exception:
                pass
        self.log("connected: IN1 -> iq2 (X, Y; NCO locked to the IN2 reference by iq0 / iq1) -> pid0 -> OUT1; "
                 "error -> OUT2 (pid1); no dither output")
        p0 = self.r.pid0
        if p0.input in ("iq2", "iq2_2") and p0.output_direct in ("out1", "out2", "both") and (p0.p != 0 or p0.i != 0):
            self._adopt_lock()
        else:
            self._write_ref()
            self._write_lockin()
            self._sync()
            self._write_eo()
            self._start_ramp()
        self._startup_find()

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
                        version=self.version, seq=self.seq, hw=dict(self.hw), ival=self.ival, fps=self.fps,
                        theta=self.theta, probe=self.probe, ref=dict(self.ref), corr=self.corr, f_nco=self.f_nco)

    def switches(self):
        s = self.s
        return dict(output=self.mode != "off", ramp=self.mode == "ramp", pi=self.mode == "pi",
                    filt=bool(s["filt_on"]), pi_out2=bool(s["pi_out2"]), eo=bool(s["eo_on"]),
                    track=bool(s["ref_track"]))

    def _readback(self):
        r, hw = self.r, {}
        for mod, attrs in (("iq2", ("frequency", "amplitude", "phase", "bandwidth", "acbandwidth",
                                    "quadrature_factor", "output_direct")),
                           ("iq0", ("frequency", "phase", "bandwidth", "input")),
                           ("pid0", ("input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter",
                                     "output_direct")),
                           ("pid1", ("input", "p", "ival", "min_voltage", "max_voltage", "output_direct")),
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

    # ---- reference (iq0 / iq1 on IN2) and lock-in (iq2 on IN1) ----
    def _write_ref(self):
        s = self.s
        for name, ph in (("iq0", 0.0), ("iq1", 90.0)):
            iq = getattr(self.r, name)
            iq.input = "in2"
            iq.gain = 0
            iq.amplitude = 0
            iq.output_direct = "off"
            iq.output_signal = "quadrature"
            iq.frequency = self.f_nco
            iq.phase = self.corr + ph
            iq.bandwidth = [s["ref_lpf_hz"]] * 2
            iq.acbandwidth = 0
            iq.quadrature_factor = 1

    def _write_lockin(self):
        s, iq = self.s, self.r.iq2
        iq.input = "in1"
        iq.gain = 0
        iq.amplitude = 0                           # no dither from the Red Pitaya
        iq.output_direct = "off"
        iq.output_signal = "quadrature"
        iq.frequency = self.f_nco
        iq.phase = s["phase_deg"] + self.corr
        iq.bandwidth = [s["lpf_hz"]] * max(1, min(int(s["lpf_order"]), 4))
        iq.acbandwidth = s["ac_hz"]
        iq.quadrature_factor = s["lia_gain"]

    def _write_phases(self):
        r = self.r
        r.iq0.phase = self.corr
        r.iq1.phase = self.corr + 90.0
        r.iq2.phase = self.s["phase_deg"] + self.corr

    def _write_freq(self, f):
        self.f_nco = float(f)
        for name in ("iq0", "iq1", "iq2"):
            getattr(self.r, name).frequency = self.f_nco

    def _sync(self):
        """Zero phase offset between the three NCOs (PyRPL iq.synchronize_iqs). The reference phase jumps: tracking
        restarts from the new phase."""
        try:
            self.r.iq0.synchronize_iqs()
        except Exception as e:
            self.log(f"synchronize_iqs failed: {e!r}")
        self._target = None
        self._hist.clear()
        self._fll_prev = None

    def _ref_read(self):
        try:
            x, y = float(self.r.sampler.iq0), float(self.r.sampler.iq1)
        except Exception:
            x, y = float(self.r.iq0.current_output_signal), float(self.r.iq1.current_output_signal)
        return x, y

    def _settle(self):
        bw = max(float(self.s["ref_lpf_hz"]), 1.0)
        return min(0.2, 6.0 / (2 * math.pi * bw))

    def _ref_service(self, force=False):
        """Reference tracking step (called about every 50 ms, also while the scope waits)."""
        now = time.time()
        if self._busy or (not force and now - self._t_service < SERVICE_S):
            return
        self._t_service = now
        self._busy = True
        try:
            x, y = self._ref_read()
            level = math.hypot(x, y)
            ok = level > REF_MIN
            th = math.degrees(math.atan2(y, x))
            st = dict(self.ref, level=level, ok=ok, theta=th)
            if not ok or not self.s["ref_track"]:
                st["locked"] = False
                self._target = None
                with self._lock:
                    self.ref = st
                return
            if self._sign is None:
                self._calibrate_sign(th)
                with self._lock:
                    self.ref = st
                return
            if self._target is None:
                self._target = th
            err = wrap(th - self._target)
            self.corr = wrap(self.corr - self._sign * 0.8 * err)
            self._write_phases()
            unwrapped = (self._hist[-1][1] + wrap(self.corr - self._hist[-1][2])) if self._hist else self.corr
            self._hist.append((now, unwrapped, self.corr))
            st["locked"] = abs(err) < 10
            st["err"] = err
            if len(self._hist) > 10:
                t0, c0, _ = self._hist[0]
                st["rate"] = (unwrapped - c0) / max(now - t0, 1e-3)          # deg/s the tracking has to supply
            with self._lock:
                self.ref = st
            self._fll(now)
        finally:
            self._busy = False

    def _calibrate_sign(self, th0):
        """Which way does a phase-register step move the measured reference phase? Step the reference modules
        (iq0 / iq1 only: the error signal of iq2, maybe locked, is not touched) by +30 deg, look, step back."""
        step = 30.0
        r = self.r
        r.iq0.phase, r.iq1.phase = self.corr + step, self.corr + step + 90.0
        time.sleep(self._settle())
        x, y = self._ref_read()
        th1 = math.degrees(math.atan2(y, x))
        d = wrap(th1 - th0)
        r.iq0.phase, r.iq1.phase = self.corr, self.corr + 90.0
        time.sleep(self._settle())
        if abs(abs(d) - step) < 12:
            self._sign = 1.0 if d > 0 else -1.0
            self.log(f"reference tracking: a phase step of +{step:.0f} deg moves the reference phase by {d:+.1f} deg "
                     f"-> tracking on")
            self._target = None
        else:
            self.log(f"reference tracking: phase step test gave {d:+.1f} deg (expected +-{step:.0f}); the reference "
                     f"phase is drifting fast: check the frequency (Find f)")

    def _fll(self, now):
        """Frequency correction: if the tracking keeps adding phase at rate r [deg/s], the NCO is off by r / 360 Hz.
        Applied every 3 s when |r| > 5 deg/s; the direction is checked on the first correction."""
        if now - self._last_fll < 3.0 or len(self._hist) < 20:
            return
        self._last_fll = now
        t0, c0, _ = self._hist[0]
        t1, c1, _ = self._hist[-1]
        rate = (c1 - c0) / max(t1 - t0, 1e-3)
        prev = self._fll_prev
        if prev is not None and abs(rate) > 0.7 * abs(prev):
            self._fsign = -self._fsign             # the last correction made it worse or did nothing: other sign
            self.log(f"reference frequency correction direction flipped (drift {prev:+.1f} -> {rate:+.1f} deg/s)")
        if abs(rate) > 5.0:
            df = self._fsign * rate / 360.0
            self._write_freq(self.f_nco + df)
            self._fll_prev = rate
            self._hist.clear()
            self.log(f"reference drift {rate:+.1f} deg/s: NCO frequency {self.f_nco - df:.4f} -> {self.f_nco:.4f} Hz")
        else:
            self._fll_prev = None

    def find_ref(self, quiet=False):
        """Measure the IN2 reference frequency and level from a scope trace; set the NCO to it."""
        sc = self.r.scope
        f0 = max(float(self.s["ref_hz"]), 1.0)
        want = max(200.0 / f0, 0.01)                     # >= 200 periods
        opts = sorted(getattr(sc, "durations", []) or [want])
        dur = min([d for d in opts if d >= want] or [opts[-1]])
        if dur / 16384 > 1 / (8 * f0):                   # at least 8 points per period
            dur = max([d for d in opts if d / 16384 <= 1 / (8 * f0)] or [opts[0]])
        t, a, _ = self.capture("in2", "in2", dur)
        if np.ptp(a) < 0.005:
            self.ref["find"] = dict(ok=False)
            if not quiet:
                self.log("Find f: no sine on IN2 (peak-peak < 5 mV at the FPGA)")
            return None
        f, pp = measure_freq(t, a)
        self.ref["find"] = dict(ok=True, f=f, vpp=pp * self.s["in2_scale"], t=time.time())
        if not quiet:
            self.s["ref_hz"] = round(f, 4)
            self.version += 1
            self._write_freq(f)
            self._sync()
            self.log(f"Find f: IN2 reference {f:.4f} Hz, {pp*1e3:.0f} mVpp at the FPGA ({pp*self.s['in2_scale']:.2f} "
                     f"Vpp at the connector with IN2 scale {self.s['in2_scale']:g}); NCOs set and synchronized")
        return f

    def _startup_find(self):
        try:
            f = self.find_ref(quiet=True)
        except Exception as e:
            self.log(f"IN2 reference check failed: {e!r}")
            return
        if f is None:
            self.log("IN2: no reference sine found at start-up (connect the external dither; then Find f)")
        elif abs(f - self.f_nco) > max(1.0, 0.1 * self.s["lpf_hz"]):
            self.log(f"IN2 reference measured at {f:.4f} Hz, NCO at {self.f_nco:.4f} Hz: press Find f (or type "
                     f"the frequency)")
        else:
            self.log(f"IN2 reference {f:.4f} Hz, NCO {self.f_nco:.4f} Hz: tracking")

    # ---- PI (pid0), error output (pid1), ramp (asg0) ----
    def _write_pid(self):
        s, p = self.s, self.r.pid0
        p.input = QUAD[s["pi_input"]]
        lo, hi = sorted((s["clamp_lo_mv"] * 1e-3, s["clamp_hi_mv"] * 1e-3))
        p.min_voltage, p.max_voltage = max(lo, -LIM), min(hi, LIM)
        p.setpoint = s["setpoint_mv"] * 1e-3
        p.inputfilter = [s[k] for k in FILT] if s["filt_on"] else [0, 0, 0, 0]
        p.p = s["p"]                       # PyRPL's own units, unchanged
        p.i = s["i"]

    def _pi_route(self):
        o1, o2 = self.mode == "pi", bool(self.s["pi_out2"])
        return "both" if (o1 and o2) else "out1" if o1 else "out2" if o2 else "off"

    def _route_pid(self):
        p0 = self.r.pid0
        new, old = self._pi_route(), p0.output_direct
        if new != "off" and old == "off":
            self._write_pid()
            p0.ival = 0
        p0.output_direct = new
        if new == "off":
            p0.ival = 0

    def _write_eo(self):
        """pid1: OUT2 = gain x (X or Y) + offset, clamped; I = 0 (a gain stage, no feedback)."""
        s, p = self.s, self.r.pid1
        lim = min(abs(s["eo_limit_mv"]), 1000.0) * 1e-3
        p.i = 0
        p.input = QUAD[s["eo_source"]]
        p.setpoint = 0
        p.p = s["eo_gain"]
        p.min_voltage, p.max_voltage = -lim, lim
        p.ival = max(min(s["eo_offset_mv"] * 1e-3, lim), -lim)
        try:
            p.inputfilter = [0, 0, 0, 0]
        except Exception:
            pass
        p.output_direct = "out2" if s["eo_on"] else "off"

    def _ramp_limits(self):
        s = self.s
        c, pp = s["offset_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3
        return max(c - pp / 2, -LIM), min(c + pp / 2, LIM)

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

    # ---- lock modes ----
    def _start_ramp(self):
        self.mode = "ramp"
        self._route_pid()
        self._write_pid()
        self._write_asg()
        lo, hi = self._ramp_limits()
        self.log(f"RAMP: OUT1 {self.s['ramp_shape']} {lo*1e3:+.0f}..{hi*1e3:+.0f} mV at {self.s['ramp_hz']:g} Hz "
                 f"(PI not on OUT1" + (", PI → OUT2" if self.s["pi_out2"] else "") + ")")

    def _start_dc(self):
        self.mode = "dc"
        self._route_pid()
        self._write_asg()
        self.log(f"OUT1 = offset {self.s['offset_mv']:+.1f} mV (PI not on OUT1"
                 + (", PI → OUT2" if self.s["pi_out2"] else "") + ")")

    def _start_pi(self):
        s = self.s
        self.mode = "pi"
        self._write_asg()
        self._write_pid()
        self._route_pid()
        self.log(f"LOCK PI → OUT1: OUT1 = {s['offset_mv']:+.1f} mV + PI({s['pi_input']} - {s['setpoint_mv']:+.1f} mV)"
                 + (" (and PI → OUT2)" if s["pi_out2"] else "")
                 + f"; p = {s['p']:g}, i = {s['i']:g}, filter {'on' if s['filt_on'] else 'off'}")
        if s["p"] == 0 and s["i"] == 0:
            self.log("  note: p = i = 0, the PI outputs 0")

    def _adopt_lock(self):
        """pid0 already running on X / Y: keep everything, read the settings back, keep tracking from here."""
        r, s = self.r, self.s
        p0, iq, a = r.pid0, r.iq2, r.asg0
        s.update(pi_input="Y" if p0.input == "iq2_2" else "X", p=float(p0.p), i=float(p0.i),
                 setpoint_mv=float(p0.setpoint) * 1e3, clamp_lo_mv=float(p0.min_voltage) * 1e3,
                 clamp_hi_mv=float(p0.max_voltage) * 1e3)
        f = [float(x) for x in p0.inputfilter]
        s["filt_on"] = any(f)
        if any(f):
            s.update({k: round(v, 3) for k, v in zip(FILT, f + [0.0] * 4)})
        self.f_nco = float(iq.frequency)
        s["ref_hz"] = round(self.f_nco, 4)
        self.corr = float(r.iq0.phase) if r.iq0.input == "in2" else 0.0
        bw = [float(b) for b in np.atleast_1d(iq.bandwidth) if b > 0]
        s.update(phase_deg=round(wrap(float(iq.phase) - self.corr), 3), ac_hz=float(iq.acbandwidth),
                 lia_gain=int(round(float(iq.quadrature_factor))))
        if bw:
            s.update(lpf_hz=round(bw[0], 3), lpf_order=len(bw))
        s["pi_out2"] = p0.output_direct in ("out2", "both")
        p1 = r.pid1
        s["eo_on"] = p1.output_direct == "out2" and p1.input in ("iq2", "iq2_2")
        if p0.output_direct in ("out1", "both"):
            self.mode = "pi"
            s["offset_mv"] = float(a.offset) * 1e3 if a.output_direct == "out1" and a.amplitude == 0 else 0.0
        elif a.output_direct == "out1":
            self.mode = "ramp" if a.amplitude > 0 else "dc"
            s["offset_mv"] = float(a.offset) * 1e3
        else:
            self.mode = "off"
        if r.iq0.input != "in2":                        # reference modules not set up: set them up (no sync)
            self._write_ref()
        self.version += 1
        self.log(f"pid0 was running ({s['pi_input']} -> {p0.output_direct.upper()}, p = {s['p']:g}, "
                 f"i = {s['i']:g}): adopted untouched; reference tracking continues from the present phase")

    # ---- scope ----
    def capture(self, in1, in2, duration, trigger="immediately"):
        """One scope trace; the reference tracking keeps running while it records / waits for the trigger."""
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
        start()
        t_arm = time.time()
        while time.time() - t_arm < duration - 0.002:
            self._ref_service()
            time.sleep(min(0.01, max(0.0, duration - 0.002 - (time.time() - t_arm))))
        while not sc.curve_ready():
            if time.time() - t_arm > timeout:
                raise TimeoutError(f"scope did not trigger on '{trigger}'")
            self._ref_service()
            time.sleep(0.001)
        data = np.array(read())
        n = data.shape[-1]
        return np.arange(n) * (duration / n), data[0], data[1]

    def _ramp_window(self):
        s = self.s
        want = max(int(s["ramp_periods"]), 1) / s["ramp_hz"]
        opts = sorted(getattr(self.r.scope, "durations", []) or [])
        if opts and want > opts[-1] * 1.0001:
            if self._long_msg != want:
                self.log(f"{int(s['ramp_periods'])} ramp periods ({want:.2f} s) are longer than the scope's longest "
                         f"trace ({opts[-1]:.2f} s): showing {opts[-1]*s['ramp_hz']:.2f} periods")
                self._long_msg = want
            return opts[-1]
        return want

    def _probe_frame(self, have, dur, trigger):
        s = self.s
        a, b = s["probe_a"], s["probe_b"]
        real = [x for x in (a, b) if x != "off"]
        if not real:
            return None
        if all(x in have for x in real):
            return dict(t=have["t"], a=have.get(a), b=have.get(b), a_name=a, b_name=b)
        if self._frames % max(int(s["probe_every"]), 1) and self.probe is not None and \
                (self.probe.get("a_name"), self.probe.get("b_name")) == (a, b):
            return self.probe
        t, ya, yb = self.capture(a if a != "off" else real[0], b if b != "off" else real[0], dur, trigger)
        return dict(t=t, a=None if a == "off" else ya, b=None if b == "off" else yb, a_name=a, b_name=b)

    def _toggle_probe(self, sig):
        s = self.s
        a, b = s["probe_a"], s["probe_b"]
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
        s["probe_a"], s["probe_b"] = a, b
        self.version += 1
        self.log(f"probes: A = {a}, B = {b}")

    def _tick(self):
        s = self.s
        self._frames += 1
        if self.mode == "ramp":
            dur = self._ramp_window()
            trig = "asg0"
            t, pd, o = self.capture("in1", "out1", dur, trigger=trig)
            _, x, y = self.capture("iq2", "iq2_2", dur, trigger=trig)
            n = min(int(np.searchsorted(t, dur * 0.9999)), len(x))
            t, pd, o, x, y = t[:n], pd[:n], o[:n], x[:n], y[:n]
            kind = "ramp"
            th, q = xy_theta(x, y)
            theta = dict(theta=th, q=q) if th is not None else None
        else:
            dur = s["live_ms"] * 1e-3
            trig = "immediately"
            t, pd, o = self.capture("in1", "out1", dur)
            _, x, y = self.capture("iq2", "iq2_2", dur)
            kind = "live"
            theta = None
        have = {"t": t, "in1": pd, "out1": o, "iq2": x, "iq2_2": y}
        probe = self._probe_frame(have, dur, trig)
        iv = None
        try:
            if self.r.pid0.output_direct != "off":
                iv = float(self.r.pid0.ival)
        except Exception:
            pass
        now = time.time()
        with self._lock:
            self.trace = dict(kind=kind, t=t, pd=pd, out1=o, x=x, y=y)
            self.probe = probe
            self.ival, self.theta = iv, theta
            self.seq += 1
            if self._t_last:
                dt = max(now - self._t_last, 1e-3)
                self.fps = 0.8 * self.fps + 0.2 / dt if self.fps else 1 / dt
            self._t_last = now
        if kind == "live":
            t_end = time.time() + 0.02
            while time.time() < t_end:
                self._ref_service()
                time.sleep(0.005)

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
                self._ref_service()
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
            if "ref_hz" in changed:
                self._write_freq(s["ref_hz"])
                self._sync()
            if "ref_lpf_hz" in changed:
                for name in ("iq0", "iq1"):
                    getattr(self.r, name).bandwidth = [s["ref_lpf_hz"]] * 2
            if changed & LOCKIN_KEYS:
                self._write_lockin()
            if changed & PID_KEYS:
                self._write_pid()
            if changed & EO_KEYS:
                self._write_eo()
            if changed & RAMP_KEYS and self.mode != "off":
                self._write_asg()
            if changed - {"ui_hz", "xaxis", "live_ms", "ramp_periods", "probe_every", "probe_a", "probe_b",
                          "in2_scale"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "switch":
            self._switch(*arg)
        elif cmd == "find_ref":
            self.find_ref()
        elif cmd == "spin":                     # P / I arrows (PyRPL PID widget): value as stepped, unchanged
            k, v = arg
            if k in ("p", "i") and s.get(k) != v:
                s[k] = v
                self._write_pid()
        elif cmd == "probe":
            self._toggle_probe(arg)
        elif cmd in ("offset", "offset_nudge"):
            v = float(arg) if cmd == "offset" else s["offset_mv"] + float(arg)
            v = round(max(min(v, 1000.0), -1000.0), 3)
            if v != s["offset_mv"]:
                s["offset_mv"] = v
                self.version += 1
                if self.mode != "off":
                    self._write_asg()
                self.log(f"offset {v:+g} mV")
        elif cmd == "reset_i":
            self.r.pid0.ival = 0
            self.log("PI integrator reset")
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update(load_settings(json.load(f)))
            self._write_freq(s["ref_hz"])
            self._write_ref()
            self._write_lockin()
            self._sync()
            self._write_pid()
            self._write_eo()
            self._route_pid()
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
            self.log(f"PI filter {'ON: ' + str([s[k] for k in FILT]) if on else 'off'}")
        elif name == "track":
            s["ref_track"] = on
            self._target = None
            self.version += 1
            self.log("reference tracking " + ("on" if on else "off (the NCO runs free: X / Y rotate at the "
                                                             "frequency difference)"))
        elif name == "eo":
            if on == bool(s["eo_on"]):
                return
            if on and s["pi_out2"]:
                s["pi_out2"] = False
                self._route_pid()
                self.log("PI → OUT2 off: OUT2 now carries the error signal")
            s["eo_on"] = on
            self.version += 1
            self._write_eo()
            self.log(f"error signal → OUT2 {'on' if on else 'off'}")
        elif name == "pi_out2":
            if on == bool(s["pi_out2"]):
                return
            if on and s["eo_on"]:
                s["eo_on"] = False
                self._write_eo()
                self.log("Error → OUT2 off: OUT2 now carries the PI")
            s["pi_out2"] = on
            self.version += 1
            self._route_pid()
            self.log(f"PI → OUT2 {'on' if on else 'off'}: pid0 output {self.r.pid0.output_direct}")
        elif name == "output":
            if on and self.mode == "off":
                self._start_dc()
            elif not on and self.mode != "off":
                self.mode = "off"
                self._route_pid()
                self._write_asg()
                self.log("OUT1 off: 0 V (ramp / offset and PI off OUT1" + ("; PI → OUT2 stays" if s["pi_out2"]
                                                                         else "") + ")")
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
    ap = argparse.ArgumentParser(description="Dual-phase lock-in on an external IN2 reference, PI, error -> OUT2")
    ap.add_argument("--host", default="rp-f0fa92.local", help="Red Pitaya hostname or IP")
    ap.add_argument("--config", default="extref_app", help="PyRPL config name")
    ap.add_argument("--port", type=int, default=8105)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update(load_settings(json.load(f)))
    ctl = ExtRefController(args.host, args.config, settings)
    import extref_ui
    app = extref_ui.build_app(ctl, sys.modules[__name__])
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
