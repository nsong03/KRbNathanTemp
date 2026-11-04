#!/usr/bin/env python3
"""
errsig2ph_app.py -- spin-off of errsig_app.py with a 2-PHASE (dual-quadrature) lock-in for the error signal.
Same wiring and workflow; no PID feedback runs on the Red Pitaya.

  IN1  <- cavity transmission photodiode
  OUT1 -> servo box error input:   OUT1 = gain x X (or Y) + offset              (pid1 as a pass-through amplifier)
  IN2  <- servo box correction output (HV jumper for +/-15 V; LV needs a limiter)
  OUT2 -> PZT driver:  servo mode:      OUT2 = gain x IN2 + DC offset + dither   (pid0 pass-through, asg0, iq2)
                       ramp (align):    OUT2 = ramp + dither (the IN2 correction is off)

The lock-in is iq2, the only PyRPL IQ module whose FPGA instance exposes both quadratures:
  X = 'iq2' (in-phase), Y = 'iq2_2' (90 deg), one shared low-pass (up to 4 stages) and integer gain.
The app shows X, Y and R = sqrt(X^2 + Y^2) and measures theta, the angle of the error signal in the X-Y plane, from
one sweep (principal axis of X vs Y across the target peak). Auto phase rotates the LO by theta so the whole error
signal is in X (Y = 0) with a negative slope: one measurement instead of the 4-phase fit of errsig_app.py. In servo
mode theta is estimated live from the residual error fluctuations (they lie along the error axis), so a drifting
demodulation phase shows up while the servo box holds the lock.

Run (close the other apps first: one owner per Red Pitaya)
  pip install "dash>=2.9" scipy
  python errsig2ph_app.py --host rp-f0fa92.local          -> http://127.0.0.1:8065
Settings: errsig2ph_settings.json next to this file (Save / Load).
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

import cavity_lock_app as CL                    # analysis helpers only (no hardware is touched on import)
import errsig2ph_schematic as ES

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "errsig2ph_settings.json")
OUT_LIMIT = ES.OUT_LIMIT

FIELDS = [
    ("ramp_shape", "Ramp shape", "triangle", "Ramp", "select", {"options": ["triangle", "sawtooth"]}),
    ("ramp_hz", "Ramp frequency [Hz]", 20.0, "Ramp", "num", {"min": 0.2}),
    ("ramp_mvpp", "Ramp amplitude [mVpp]", 500.0, "Ramp", "num", {"min": 0}),
    ("ramp_center_mv", "Setpoint: ramp centre / DC offset in servo mode [mV]", 0.0, "Ramp", "num", {}),
    ("dither_hz", "Dither frequency [Hz]", 1000.0, "Dither", "num", {"min": 1}),
    ("dither_mvpp", "Dither amplitude [mVpp]", 30.0, "Dither", "num", {"min": 0}),
    ("phase_deg", "Demodulation phase [deg]", 0.0, "Demodulation", "num", {}),
    ("lpf_hz", "Error low-pass [Hz]", 250.0, "Demodulation", "num", {"min": 1}),
    ("lpf_order", "Low-pass order (1 to 4, X and Y)", 2, "Demodulation", "num", {"min": 1, "max": 4}),
    ("ac_hz", "Photodiode high-pass [Hz]", 100.0, "Demodulation", "num", {"min": 0}),
    ("err_gain", "Error gain (integer, 1 = lowest)", 1, "Demodulation", "num", {"min": 1}),
    ("eo_source", "Error output source (X = in-phase, Y = quadrature)", "X", "Error output", "select",
     {"options": ["X", "Y"]}),
    ("eo_gain", "Error output gain (OUT1 = gain x error + offset)", 1.0, "Error output", "num", {}),
    ("eo_offset_mv", "Error output offset [mV]", 0.0, "Error output", "num", {}),
    ("eo_limit_mv", "Error output clamp [+/- mV]", 1000.0, "Error output", "num", {"min": 1, "max": 1000}),
    ("in2_gain", "Correction gain (OUT2 += gain x IN2)", 1.0, "Correction", "num", {}),
    ("in2_scale", "IN2 scale [V per raw V] (20 = HV jumper, 1 = LV)", 20.0, "Correction", "num", {"min": 0.01}),
    ("averages", "Traces averaged", 1, "Scope", "num", {"min": 1, "max": 50}),
    ("prominence", "Peak threshold [x tallest]", 0.1, "Scope", "num", {"min": 0.01, "max": 1}),
    ("live_window_ms", "Live trace length in servo mode [ms]", 33.0, "Scope", "num", {"min": 1}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Scope", "num", {"min": 1, "max": 30}),
    ("probe_every", "Probe capture every N traces", 3, "Scope", "num", {"min": 1, "max": 100}),
    ("in1_scale", "IN1 scale [V per raw V] (1 = LV, 20 = HV)", 1.0, "Scope", "num", {"min": 0.01}),
    ("probe_a", "Probe A", "out2", "Scope", "select", {"options": ES.PROBE_SIGNALS + ["off"]}),
    ("probe_b", "Probe B", "in2", "Scope", "select", {"options": ES.PROBE_SIGNALS + ["off"]}),
]
KEYS = [f[0] for f in FIELDS]
DEFAULTS = {f[0]: f[2] for f in FIELDS}
DEFAULTS["target_mv"] = None
SNAP_KEYS = ("lpf_hz", "lpf_order", "ac_hz", "err_gain")


def xy_theta(x, y):
    """Angle of the signal axis in the X-Y plane [deg, -90..90] (principal axis of the centred scatter) and the
    axis quality q = variance along / across the axis (1 = no preferred direction)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 4:
        return None, 0.0
    x, y = x - x.mean(), y - y.mean()
    sxx, syy, sxy = float(np.dot(x, x)), float(np.dot(y, y)), float(np.dot(x, y))
    th = 0.5 * math.atan2(2 * sxy, sxx - syy)
    d = math.hypot(sxx - syy, 2 * sxy)
    l1, l2 = (sxx + syy + d) / 2, (sxx + syy - d) / 2
    return math.degrees(th), (l1 / l2 if l2 > 1e-30 * max(l1, 1e-30) else 1e9)


class TwoPhaseController:
    """Owns the Red Pitaya; runs in the main thread (ctl.run). Modes: acquire (Ramp on: OUT2 = ramp + dither),
    servo (Ramp off: OUT2 = gain x IN2 + DC offset + dither), busy (auto phase), off (Output off: OUT2 = 0 V).
    Demodulation on iq2 with both quadratures: X = 'iq2', Y = 'iq2_2'."""

    def __init__(self, host, config, settings):
        self.s = dict(settings)
        self.cmds = queue.Queue()
        self._pub_lock = threading.Lock()
        self.logs = collections.deque(maxlen=300)
        self.version = 0
        self.seq = 0
        self.mode = "connecting"
        self.traces = None
        self.drive = None
        self.analysis = {}
        self.slope = None                    # error slope at the parked crossing [V of iq2 per V of OUT2]
        self.in2_now = None                  # last IN2 sample (servo correction, raw volts)
        self.kappa = None
        self.amp_cal = 1.0
        self.dither_meas = None
        self.dither_on = True
        self.errout_on = True
        self.fps = 0.0
        self._t_last = None
        self._cal_checks = 2
        self._last_dither = None
        self._clip_msg = None
        self._scope_cfg = {}
        self.ramp = None
        self._ramp_cache = None
        self._tick = 0
        self.hw, self.req = {}, {}
        self._probe_last = "b"
        self.xy = {}                         # theta [deg], quality q, source ("sweep" / "live")
        self._phase_sign = None              # + or -: which way iq2.phase rotates theta (found by Auto phase)
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("iq2", "asg0", "pid0", "pid1", "scope"):
            try:
                getattr(self.r, name).owner = "errsig2ph_app"
            except Exception:
                pass
        for name in ("pid2",):                           # this app runs no feedback: the unused PID off
            try:
                m = getattr(self.r, name)
                m.output_direct = "off"
                m.p, m.i = 0, 0
            except Exception:
                pass
        for name in ("asg1", "iq0", "iq1"):
            try:
                getattr(self.r, name).output_direct = "off"
            except Exception:
                pass
        self.log("connected (2-phase lock-in on iq2: X = iq2, Y = iq2_2; no PID feedback on the Red Pitaya: "
                 "error signal on OUT1, PZT summer on OUT2)")
        self._start_acquire()

    # ---- utilities ----
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def submit(self, cmd, arg=None):
        self.cmds.put((cmd, arg))

    def snapshot(self):
        with self._pub_lock:
            return dict(mode=self.mode, traces=self.traces, drive=self.drive, analysis=self.analysis,
                        logs=list(self.logs), settings=dict(self.s), version=self.version, seq=self.seq,
                        ramp=self.ramp, kappa=self.kappa, dither_meas=self.dither_meas, amp_cal=self.amp_cal,
                        fps=self.fps, hw=dict(self.hw), req=dict(self.req), errout_on=self.errout_on,
                        dither_on=self.dither_on, slope=self.slope, in2_now=self.in2_now, xy=dict(self.xy))

    def switches(self):
        return ES.switch_states(self.mode, self.hw, self.s, self.errout_on)

    def _readback(self):
        hw = ES.read_hw(self.r)
        with self._pub_lock:
            self.hw = hw

    def _publish(self, **kw):
        with self._pub_lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if "traces" in kw:
                self.seq += 1
                now = time.time()
                if self._t_last:
                    dt = now - self._t_last
                    self.fps = 0.8 * self.fps + 0.2 / max(dt, 1e-3) if self.fps else 1 / max(dt, 1e-3)
                self._t_last = now

    def _filter_settle(self):
        return 6 * self.s["lpf_order"] / (2 * math.pi * self.s["lpf_hz"])

    def _wait(self, dt):
        time.sleep(max(dt, 0.0))

    # ---- configuration ----
    def configure(self):
        ES.write_static(self)
        self._snap_to_hardware()

    def _snap_to_hardware(self):
        s, iq, snapped = self.s, self.r.iq2, {}
        bw = [float(b) for b in np.atleast_1d(iq.bandwidth) if b > 0]
        if bw and abs(bw[0] - s["lpf_hz"]) > 0.01 * s["lpf_hz"]:
            snapped["lpf_hz"] = round(bw[0], 1)
        if bw and len(bw) != int(s["lpf_order"]):
            snapped["lpf_order"] = len(bw)
        ac = float(iq.acbandwidth)
        if abs(ac - s["ac_hz"]) > 0.01 * max(s["ac_hz"], 1.0):
            snapped["ac_hz"] = round(ac, 1)
        qf = float(iq.quadrature_factor)
        if abs(qf - s["err_gain"]) > 1e-9:
            snapped["err_gain"] = int(round(qf)) if abs(qf - round(qf)) < 1e-9 else qf
        if snapped:
            self.log("hardware values: " + ", ".join(f"{k} {s[k]} -> {v}" for k, v in snapped.items()))
            for k in snapped:
                self.req[k] = s[k]
            s.update(snapped)
            self.version += 1

    # ---- ramp / servo mode ----
    def _ramp_limits(self, center, pp):
        lim = OUT_LIMIT - self.s["dither_mvpp"] * 1e-3 / 2
        lo, hi = center - pp / 2, center + pp / 2
        lo2, hi2 = float(np.clip(lo, -lim, lim)), float(np.clip(hi, -lim, lim))
        msg = f"ramp clipped to {lo2*1e3:+.0f}..{hi2*1e3:+.0f} mV (OUT1 limit)" if (lo2, hi2) != (lo, hi) else None
        if msg and msg != self._clip_msg:
            self.log(msg)
        self._clip_msg = msg
        return lo2, hi2

    def _ramp_on(self, lo, hi, hz, shape):
        self.r.asg0.setup(waveform="ramp" if shape == "triangle" else "halframp", frequency=hz,
                          amplitude=max(hi - lo, 0) / 2, offset=(hi + lo) / 2, trigger_source="immediately",
                          output_direct="out2")
        self.ramp = dict(lo=lo, hi=hi, hz=hz, shape=shape)
        self._ramp_cache = None

    def _dc(self, v):
        """The setpoint as a DC offset on OUT2 (asg0 offset, amplitude 0), under the IN2 correction."""
        lim = OUT_LIMIT - self.s["dither_mvpp"] * 1e-3 / 2
        v = float(np.clip(v, -lim, lim))
        a = self.r.asg0
        a.amplitude = 0
        a.offset = v
        a.output_direct = "out2"
        return v

    def _servo_mode(self, v, quiet=False):
        """Ramp off: OUT2 = gain x IN2 (pid0) + DC offset v (asg0) + dither. No feedback on the Red Pitaya."""
        self.mode = "servo"
        v = self._dc(v)
        ES.WRITERS["corr"](self)
        ES.WRITERS["dither"](self)
        if not quiet:
            self.log(f"servo mode: OUT2 = {self.s['in2_gain']:g} x IN2 + {v*1e3:+.2f} mV DC + dither "
                     f"(the servo box closes the loop)")

    def _start_acquire(self, quiet=False):
        s = self.s
        self.mode = "acquire"
        self.configure()                                  # the IN2 correction goes off in this mode
        lo, hi = self._ramp_limits(s["ramp_center_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3)
        self._ramp_on(lo, hi, s["ramp_hz"], s["ramp_shape"])
        if not quiet:
            self.log(f"ramp (alignment): OUT2 {s['ramp_shape']} {lo*1e3:+.0f}..{hi*1e3:+.0f} mV at {s['ramp_hz']:g} Hz, "
                     f"dither {s['dither_mvpp']:g} mVpp at {s['dither_hz']:g} Hz")
        self._readback()

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

    def capture_sweep(self, averages=1, want_drive=False):
        rp = self.ramp
        hz = rp["hz"]
        dur = 1.02 / hz
        key = (rp["lo"], rp["hi"], hz, rp["shape"])
        drive = None
        if want_drive or self._ramp_cache is None or self._ramp_cache[0] != key:
            td, drv, ramp = self.capture("out2", "asg0", dur, trigger="asg0")
            self._ramp_cache = (key, ramp)
            drive = dict(t=td, out=drv, ref=ramp)
        ramp = self._ramp_cache[1]
        pd = err = xb = yb = None
        for _ in range(int(averages)):
            t, p, e = self.capture("in1", "iq2", dur, trigger="asg0")
            _, x2, y2 = self.capture("iq2", "iq2_2", dur, trigger="asg0")     # X and Y from the same instant
            pd = p if pd is None else pd + p
            err = e if err is None else err + e
            xb = x2 if xb is None else xb + x2
            yb = y2 if yb is None else yb + y2
        pd, err, xb, yb = pd / averages, err / averages, xb / averages, yb / averages
        n = min(len(ramp), len(pd), len(yb), int(np.searchsorted(t, 1.0 / hz)))
        ramp, pd, err, t, xb, yb = ramp[:n], pd[:n], err[:n], t[:n], xb[:n], yb[:n]
        k = max(3, n // 200)
        sm = np.convolve(np.pad(ramp, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
        up = np.gradient(sm) > 0
        return dict(t=t, v=ramp, pd=pd * self.s["in1_scale"], err=err, xb=xb, y=yb, up=up), drive

    def _half_window(self, an, tgt):
        half = max(1.2 * tgt["fwhm"], 3 * (self.kappa or 0))
        if an.get("mode_spacing"):
            half = min(half, 0.45 * an["mode_spacing"])
        return half

    def _theta_window(self, tr, v_t, half):
        w = np.abs(tr["v"] - v_t) < half
        return xy_theta(tr["xb"][w], tr["y"][w])

    def _analyze(self, tr, target_mv):
        """analyze_sweep plus the 2-phase numbers at the target peak: theta and its quality."""
        an = CL.analyze_sweep({k: tr[k] for k in ("t", "v", "pd", "err", "up")}, self._ctx(target_mv))
        if an.get("target") is not None and "y" in tr:
            tgt = an["peaks"][an["target"]]
            th, q = self._theta_window(tr, tgt["v"], self._half_window(an, tgt))
            an["theta"], an["theta_q"] = th, q
            self.xy = dict(theta=th, q=q, src="sweep")
        else:
            self.xy = {}
        return an

    def _ctx(self, target_mv):
        s, rp = self.s, self.ramp
        pp = rp["hi"] - rp["lo"]
        rate = (2 if rp["shape"] == "triangle" else 1) * pp * rp["hz"]
        return dict(prominence=s["prominence"], target_mv=target_mv, rate=rate, lpf_hz=s["lpf_hz"],
                    lpf_order=s["lpf_order"], dither_hz=s["dither_hz"], dither_mvpp=s["dither_mvpp"],
                    kappa=self.kappa, ramp_hz=rp["hz"], ramp_mvpp=pp * 1e3)

    def _check_drive(self, drive):
        """Measure the dither on OUT2 (OUT2 minus the ramp) and calibrate its amplitude (twice after a change)."""
        if not self.dither_on or self.mode == "off":
            return
        want = self.s["dither_mvpp"]
        pp = CL.sine_pp(drive["t"], drive["out"] - drive["ref"], self.s["dither_hz"])
        if pp is None:
            return
        self.dither_meas = pp * 1e3
        if self._cal_checks > 0 and want > 0 and pp > 0:
            self._cal_checks -= 1
            ratio = pp * 1e3 / want
            if abs(ratio - 1) > 0.02:
                self.amp_cal *= ratio
                self.log(f"dither on OUT2 measured {pp*1e3:.2f} mVpp for {want:g} requested: corrected")
                ES.WRITERS["dither"](self)
                self._cal_checks = max(self._cal_checks, 1)

    # ---- main loop ----
    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                while True:
                    cmd, arg = self.cmds.get_nowait()
                    try:
                        self._do(cmd, arg)
                    except Exception as e:
                        self.log(f"ERROR in {cmd}: {e!r}")
                        if self.mode == "busy":
                            self.mode = "acquire"
                    try:
                        self._readback()
                    except Exception as e:
                        self.log(f"readback error: {e!r}")
            except queue.Empty:
                pass
            try:
                if self.mode == "acquire":
                    self._acquire_tick()
                elif self.mode in ("servo", "off"):
                    self._live_tick()
                else:
                    time.sleep(0.05)
            except Exception as e:
                self.log(f"acquisition error: {e!r}")
                time.sleep(0.5)

    def _do(self, cmd, arg):
        s = self.s
        if cmd == "apply":
            arg, base_version = arg
            if base_version != self.version:
                return
            changed = {k for k, v in arg.items() if s.get(k) != v}
            if not changed:
                return
            for k in changed & set(SNAP_KEYS):
                self.req[k] = arg[k]
            s.update(arg)
            written = ES.apply_changed(self, changed)
            if set(written) & {"lowpass", "bandpass", "amp"}:
                self._snap_to_hardware()
            if changed - {"ui_hz", "probe_every", "probe_a", "probe_b"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "switch":
            self._switch(*arg)
        elif cmd == "probe":
            self._toggle_probe(arg)
        elif cmd in ("ramp_center", "ramp_nudge"):
            v = float(arg) if cmd == "ramp_center" else s["ramp_center_mv"] + float(arg)
            v = round(max(min(v, OUT_LIMIT * 1e3), -OUT_LIMIT * 1e3), 3)
            if v != s["ramp_center_mv"]:
                s["ramp_center_mv"] = v
                self.version += 1
                ES.apply_changed(self, {"ramp_center_mv"})
                self.log(f"setpoint {v:+g} mV" + (" (OUT2 DC offset now)" if self.mode == "servo" else ""))
        elif cmd == "park":
            self._park_at_peak()
        elif cmd == "target":
            self._pick_target(float(arg))
        elif cmd == "target_v":
            self._target_at(float(arg))
        elif cmd == "tallest":
            s["target_mv"] = None
            self.log("target: tallest peak")
        elif cmd == "autophase":
            self._auto_phase()
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            self.req.clear()
            self.configure()
            ES.apply_changed(self, {"ramp_center_mv"})
            self.version += 1
            self.log("settings loaded")

    def _switch(self, name, on):
        on = bool(on)
        if self.mode in ES.BUSY:
            self.log(f"{name} switch ignored while {self.mode}")
            return
        if name == "errout":
            self.errout_on = on
            ES.WRITERS["errout"](self)
            self.log(f"error output {'ON: OUT1 = gain x error + offset' if on else 'off: OUT1 = 0 V'}")
        elif name == "output":
            if on and self.mode == "off":
                self._start_acquire()
            elif not on and self.mode != "off":
                a = self.r.asg0
                a.output_direct, a.amplitude, a.offset = "off", 0, 0
                self.mode = "off"
                ES.WRITERS["corr"](self)
                ES.WRITERS["dither"](self)
                self.log("Output OFF: OUT2 = 0 V (ramp/DC, correction and dither off)")
        elif on and self.mode == "off":
            self.log(f"{name}: Output is off; switch Output on first")
        elif name == "ramp":
            if on and self.mode != "acquire":
                self._start_acquire()
            elif not on and self.mode == "acquire":
                self._servo_mode(self.s["ramp_center_mv"] * 1e-3)
        elif name == "dither":
            self.dither_on = on
            ES.WRITERS["dither"](self)
            self.log(f"dither {'on' if on else 'off'}")

    def _toggle_probe(self, sig):
        if sig not in ES.PROBE_SIGNALS:
            self.log(f"'{sig}' is not a scope signal")
            return
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

    # ---- frames ----
    def _probe_frame(self, main, dur, trigger):
        s = self.s
        a, b = s["probe_a"], s["probe_b"]
        real = {x for x in (a, b) if x != "off"}
        if not real:
            return None
        if real <= ({"in1", "iq2"} if "iq2_2" not in main else {"in1", "iq2", "iq2_2"}):
            return dict(t=main["t"], a=main.get(a), b=main.get(b), a_name=a, b_name=b, pair=(a, b))
        if self._tick % max(int(s["probe_every"]), 1) and self.drive is not None and \
                self.drive.get("pair") == (a, b):
            return None
        t, ya, yb = self.capture(a if a != "off" else "in1", b if b != "off" else "in1", dur, trigger)

        def sc(n, y):
            return None if n == "off" else (y * s["in1_scale"] if n == "in1" else y)
        return dict(t=t, a=sc(a, ya), b=sc(b, yb), a_name=a, b_name=b, pair=(a, b))

    def _acquire_tick(self):
        s = self.s
        self._tick += 1
        tr, drive = self.capture_sweep(s["averages"], want_drive=self._cal_checks > 0 and self.dither_on)
        an = self._analyze(tr, s["target_mv"])
        if s["target_mv"] is not None and an.get("target_v") is not None:
            s["target_mv"] = an["target_v"] * 1e3
        if drive is not None:
            self._check_drive(drive)
        probe = self._probe_frame(dict(t=tr["t"], in1=tr["pd"], iq2=tr["err"], iq2_2=tr["y"]), 1.02 / self.ramp["hz"],
                                  "asg0")
        self._publish(traces=dict(kind="sweep", **tr), analysis=an, **({"drive": probe} if probe else {}))

    def _live_tick(self):
        s = self.s
        dur = s["live_window_ms"] * 1e-3
        try:
            self.in2_now = float(self.r.sampler.in2)      # the servo correction, one register read
        except Exception:
            self.in2_now = None
        t, pd, err = self.capture("in1", "iq2", dur)
        _, xb, yb = self.capture("iq2", "iq2_2", dur)          # X and Y together: the live theta estimate
        pd = pd * s["in1_scale"]
        self._tick += 1
        th, q = xy_theta(xb, yb)
        self.xy = dict(theta=th, q=q, src="live") if self.mode == "servo" else {}
        probe = self._probe_frame(dict(t=t, in1=pd, iq2=err), dur, "immediately")
        self._publish(traces=dict(kind="time", t=t, pd=pd, err=err, xb=xb, y=yb),
                      **({"drive": probe} if probe else {}))
        time.sleep(0.02)

    # ---- targets, park, auto phase ----
    def _pick_target(self, t_ms):
        tr, an = self.traces, self.analysis or {}
        if self.mode != "acquire" or tr is None or tr.get("kind") != "sweep" or not an.get("peaks"):
            self.log("pick a peak while ramping (Ramp on)")
            return
        k = int(np.argmin(np.abs(tr["t"] * 1e3 - t_ms)))
        p = min(an["peaks"], key=lambda q: abs(q["v"] - float(tr["v"][k])))
        self.s["target_mv"] = p["v"] * 1e3
        self.log(f"target: peak at {p['v']*1e3:+.1f} mV (height {p['height']:.3f} V)")
        self._publish(analysis=self._analyze(tr, self.s["target_mv"]))

    def _target_at(self, v):
        tr, an = self.traces, self.analysis or {}
        if self.mode != "acquire" or tr is None or tr.get("kind") != "sweep" or not an.get("peaks"):
            self.log("pick a peak while ramping (Ramp on)")
            return
        p = min(an["peaks"], key=lambda q: abs(q["v"] - v))
        self.s["target_mv"] = p["v"] * 1e3
        self.log(f"target (crosshair at {v*1e3:+.1f} mV): peak at {p['v']*1e3:+.1f} mV")
        self._publish(analysis=self._analyze(tr, self.s["target_mv"]))

    def _park_at_peak(self):
        """DC offset on the target peak's error zero crossing, then servo mode (ramp off, IN2 correction on)."""
        an = self.analysis or {}
        if self.mode != "acquire" or an.get("v0") is None:
            self.log("Park at peak: needs a ramp with an error zero crossing at the target peak")
            return
        v0, S = an["v0"], an.get("slope")
        self.s["ramp_center_mv"] = round(v0 * 1e3, 3)       # the screen shows exactly what OUT2 holds
        self.version += 1
        self.slope = S
        self._servo_mode(self.s["ramp_center_mv"] * 1e-3)
        g = self.s["eo_gain"]
        self.log(f"  error slope at the crossing: {S:+.2f} V of error per V of OUT2 -> {S*g:+.2f} V of OUT1 per V of "
                 f"OUT2 (gain {g:g}). The servo box must feed back with the opposite sign.")

    def _grab_and_show(self):
        tr, _ = self.capture_sweep(1)
        self._publish(traces=dict(kind="sweep", **tr), analysis=self._analyze(tr, self.s["target_mv"]))
        return tr

    def _fit_phase(self, grab, v_t, half):
        s = self.s
        phi0 = s["phase_deg"]
        energies = []
        for k in range(4):
            self.r.iq2.phase = phi0 + 45 * k
            self._wait(self._filter_settle())
            tr = grab()
            w = np.abs(tr["v"] - v_t) < half
            e = tr["err"][w]
            energies.append(float(np.sum((e - e.mean()) ** 2)) if len(e) else 0.0)
        c, sn = energies[0] - energies[2], energies[1] - energies[3]
        phi = phi0 + 0.5 * math.degrees(math.atan2(sn, c))
        self.r.iq2.phase = phi
        self._wait(self._filter_settle())
        an = CL.analyze_sweep(grab(), self._ctx(v_t * 1e3))
        if an.get("slope") is not None and an["slope"] > 0:
            phi += 180
        s["phase_deg"] = round(float((phi + 180) % 360 - 180), 1)
        ES.WRITERS["phase"](self)
        return s["phase_deg"], max(energies) / max(min(energies), 1e-30)

    def _rotate(self, phi):
        self.r.iq2.phase = phi
        self._wait(self._filter_settle())

    def _auto_phase(self):
        """2-phase auto phase: theta from one sweep, rotate the LO by it (Y -> 0), then pick the sign that gives a
        negative error slope. The rotation direction of iq2.phase is found once and remembered (no assumption about
        PyRPL's phase sign). Falls back to the 4-phase fit (logged) when the X-Y scatter has no clear axis."""
        s = self.s
        if self.mode != "acquire" or not self.dither_on:
            self.log("Auto phase works while ramping with the dither on")
            return
        an = self.analysis or {}
        if an.get("target") is None:
            self.log("auto phase: no target peak in the sweep")
            return
        tgt = an["peaks"][an["target"]]
        v_t = tgt["v"]
        self.mode = "busy"
        try:
            self.r.iq2.output_direct = "off"
            tr, _ = self.capture_sweep(s["averages"])
            ES.WRITERS["dither"](self)
            off = CL.analyze_sweep(tr, dict(self._ctx(v_t * 1e3), dither_mvpp=0))
            if off.get("target") is not None:
                self.kappa = off["peaks"][off["target"]]["fwhm"]
                self.log(f"auto phase: linewidth {self.kappa*1e3:.2f} mV of drive (dither off)")
            half = self._half_window(an, tgt)
            self._wait(self._filter_settle())
            phi0 = float(s["phase_deg"])
            th, q = self._theta_window(self._grab_and_show(), v_t, half)
            if th is None or q < 3:
                self.log(f"auto phase: no clear error axis in X-Y (quality {q:.1f} < 3): 4-phase fit instead")
                phi, contrast = self._fit_phase(self._grab_and_show, v_t, half)
                self.log(f"auto phase done (4-phase fit): phase {phi:+.1f} deg (contrast {contrast:.0f}:1)")
                self.version += 1
                return
            sign = self._phase_sign or 1
            phi = phi0 + sign * th
            self._rotate(phi)
            th2, q2 = self._theta_window(self._grab_and_show(), v_t, half)
            if self._phase_sign is None and abs(th) > 5 and th2 is not None and abs(th2) > 0.5 * abs(th):
                sign = -sign
                phi = phi0 + sign * th
                self._rotate(phi)
                th2, q2 = self._theta_window(self._grab_and_show(), v_t, half)
            if self._phase_sign is None and abs(th) > 5:
                self._phase_sign = sign
                self.log(f"auto phase: iq2.phase {'+' if sign > 0 else '-'}theta rotates the error into X")
            new = self._analyze(self._grab_and_show(), v_t * 1e3)
            if new.get("slope") is not None and new["slope"] > 0:
                phi += 180
                self._rotate(phi)
            s["phase_deg"] = round(float((phi + 180) % 360 - 180), 1)
            ES.WRITERS["phase"](self)
            self._wait(self._filter_settle())
            new = self._analyze(self._grab_and_show(), s["target_mv"])
            self.log(f"auto phase done: theta {th:+.1f} deg -> phase {s['phase_deg']:+.1f} deg, residual theta "
                     f"{(new.get('theta') or 0):+.1f} deg (axis quality {new.get('theta_q', 0):.0f}), error peak "
                     f"{new.get('err_max', 0):.2f} V")
            self.version += 1
        finally:
            if self.mode == "busy":
                self.mode = "acquire"


def main():
    ap = argparse.ArgumentParser(description="Red Pitaya 2-phase lock-in error signal (OUT1) + PZT summer (OUT2) for an external servo")
    ap.add_argument("--host", default="rp-f0fa92.local", help="Red Pitaya hostname or IP")
    ap.add_argument("--config", default="errsig2ph_app", help="PyRPL config name")
    ap.add_argument("--port", type=int, default=8065)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    ctl = TwoPhaseController(args.host, args.config, settings)
    import errsig2ph_ui
    app = errsig2ph_ui.build_app(ctl, sys.modules[__name__])
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
