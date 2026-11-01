#!/usr/bin/env python3
"""
cavity_lock_app.py -- browser app for dither-locking a cavity PZT with a Red Pitaya (PyRPL).

Two modes
  Acquisition (on start-up)  OUT1 = slow ramp (asg0) + dither (iq0). iq0 demodulates the photodiode on IN1
                             against its own dither oscillator -> error signal. Click a transmission peak to
                             choose the lock target; "Auto-optimize phase" sets the demodulation phase.
  Lock                       "LOCK!" stops the big ramp, switches to the lock dither, sweeps slowly across the
                             chosen peak to find the exact zero crossing and slope (and re-fits the phase),
                             parks OUT1 there and closes the loop with pid0: OUT1 = setpoint + lock dither.
Diagnostics (while locked)
  "Measure noise"  spectra of the residual detuning (from the error signal) and of the transmitted intensity
  "Measure loop"   open-loop gain, phase margin and plant response with PyRPL's network analyzer

Wiring
  IN1  <- cavity transmission photodiode   (LV jumper for signals under 1 V, else HV + set "IN1 scale" to 20)
  IN2     free (an external dither is not needed: the demodulation must use the Red Pitaya's own oscillator)
  OUT1 -> PZT driver MOD IN                (ramp + dither, or setpoint + dither)
  OUT2 -> scope                            (monitor: error, setpoint, ramp or photodiode)

Run (in the Python environment where PyRPL works)
  pip install "dash>=2.9" scipy
  python cavity_lock_app.py --host rp-f0fa92.local
  -> the app opens at http://127.0.0.1:8050 . Ctrl-C in the terminal quits (the hardware keeps its state).
  Settings are saved to / loaded from cavity_lock_settings.json next to this file (Save / Load buttons).
"""
import argparse
import collections
import json
import math
import os
import queue
import threading
import time
import webbrowser

import numpy as np
from scipy.signal import find_peaks, peak_widths, welch

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "cavity_lock_settings.json")
OUT_LIMIT = 0.95                      # never command OUT1 beyond +/- this [V]
RP_LATENCY = 211e-9                   # analog-in to analog-out delay of the lock-in path (FPGA simulation)
PANELS = ["transmission", "error", "drive", "setpoint history", "noise spectrum", "loop (Bode)"]

# ---------------------------------------------------------------------------------------------
# Settings: (key, label, default, group, kind, options)   -- UI units (mV, Hz, deg)
# ---------------------------------------------------------------------------------------------
FIELDS = [
    ("ramp_shape", "Ramp shape", "triangle", "Acquisition", "select", {"options": ["triangle", "sawtooth"]}),
    ("ramp_hz", "Ramp frequency [Hz]", 20.0, "Acquisition", "num", {"min": 0.2}),
    ("ramp_mvpp", "Ramp amplitude [mVpp]", 500.0, "Acquisition", "num", {"min": 0}),
    ("ramp_center_mv", "Ramp center [mV]", 0.0, "Acquisition", "num", {}),
    ("acq_dither_hz", "Dither frequency [Hz]", 1000.0, "Acquisition", "num", {"min": 1}),
    ("acq_dither_mvpp", "Dither amplitude [mVpp]", 30.0, "Acquisition", "num", {"min": 0}),
    ("averages", "Traces averaged", 1, "Acquisition", "num", {"min": 1, "max": 50}),
    ("phase_deg", "Demodulation phase [deg]", 0.0, "Demodulation", "num", {}),
    ("lpf_hz", "Error low-pass [Hz]", 250.0, "Demodulation", "num", {"min": 1}),
    ("lpf_order", "Low-pass order (1 or 2)", 2, "Demodulation", "num", {"min": 1, "max": 2}),
    ("ac_hz", "Photodiode high-pass [Hz]", 100.0, "Demodulation", "num", {"min": 0}),
    ("err_gain", "Error gain (integer, 1 = lowest)", 1, "Demodulation", "num", {"min": 1}),
    ("prominence", "Peak threshold [x tallest]", 0.1, "Demodulation", "num", {"min": 0.01, "max": 1}),
    ("lock_same_dither", "Lock dither = acquisition dither", True, "Lock", "check", {}),
    ("lock_dither_hz", "Lock dither frequency [Hz]", 1000.0, "Lock", "num", {"min": 1}),
    ("lock_dither_mvpp", "Lock dither amplitude [mVpp]", 30.0, "Lock", "num", {"min": 0}),
    ("refine_phase", "Re-fit the phase before locking", True, "Lock", "check", {}),
    ("ugf_hz", "Loop bandwidth [Hz]", 10.0, "Lock", "num", {"min": 0.1}),
    ("p_gain", "Proportional loop gain", 0.0, "Lock", "num", {"min": 0}),
    ("gain_ramp", "Ramp the gain up when locking", True, "Lock", "check", {}),
    ("clamp_auto", "Clamp from the mode spacing", True, "Lock", "check", {}),
    ("clamp_mv", "Clamp half-width [mV]", 20.0, "Lock", "num", {"min": 0.1}),
    ("unlock_frac", "Unlocked below [x peak height]", 0.4, "Lock", "num", {"min": 0, "max": 1}),
    ("unlock_s", "...for longer than [s]", 0.3, "Lock", "num", {"min": 0}),
    ("auto_relock", "Auto-relock", True, "Lock", "check", {}),
    ("lock_window_ms", "Live trace length when locked [ms]", 33.0, "Lock", "num", {"min": 1}),
    ("kappa_mhz", "Cavity linewidth [MHz] (for Hz units)", 40.0, "Diagnostics", "num", {"min": 0}),
    ("driver_bw_hz", "PZT driver bandwidth with the PZT [Hz]", 1000.0, "Diagnostics", "num", {"min": 0}),
    ("noise_avg", "Noise: traces averaged per band", 3, "Diagnostics", "num", {"min": 1, "max": 50}),
    ("na_start_hz", "Loop: start frequency [Hz]", 4.0, "Diagnostics", "num", {"min": 1}),
    ("na_stop_hz", "Loop: stop frequency [Hz]", 2000.0, "Diagnostics", "num", {"min": 2}),
    ("na_points", "Loop: points", 30, "Diagnostics", "num", {"min": 3, "max": 400}),
    ("na_amp_frac", "Loop: excitation [x linewidth]", 0.03, "Diagnostics", "num", {"min": 0.001, "max": 0.5}),
    ("panels", "Show", ["transmission", "error", "drive", "setpoint history"], "Display", "multi", {"options": PANELS}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Display", "num", {"min": 1, "max": 30}),
    ("drive_every", "Refresh the drive plot every N traces", 3, "Display", "num", {"min": 1, "max": 100}),
    ("in1_scale", "IN1 scale [V per raw V] (1 = LV, 20 = HV)", 1.0, "Display", "num", {"min": 0.01}),
    ("out2_signal", "OUT2 monitor", "error", "Display", "select",
     {"options": ["error", "setpoint", "ramp", "photodiode", "off"]}),
    ("out2_gain", "OUT2 gain", 1.0, "Display", "num", {}),
]
KEYS = [f[0] for f in FIELDS]
DEFAULTS = {f[0]: f[2] for f in FIELDS}
DEFAULTS["target_mv"] = None           # lock target (drive voltage of the chosen peak); None = tallest
OUT2_SOURCE = {"error": "iq0", "setpoint": "pid0", "ramp": "asg0", "photodiode": "in1"}


# ---------------------------------------------------------------------------------------------
# Analysis (pure numpy)
# ---------------------------------------------------------------------------------------------
def binned(v, ys, nbins=600):
    """Average each y in ys into bins of v. Returns (v_centres, [y_binned ...])."""
    o = np.argsort(v)
    v = v[o]
    idx = np.clip(np.digitize(v, np.linspace(v[0], v[-1], nbins + 1)) - 1, 0, nbins - 1)
    n = np.bincount(idx, minlength=nbins)
    ok = n > 0
    vb = np.bincount(idx, v, nbins)[ok] / n[ok]
    return vb, [np.bincount(idx, y[o], nbins)[ok] / n[ok] for y in ys]


def smooth(y, n):
    """Moving average over n samples (edges padded)."""
    n = int(n)
    if n < 2:
        return y
    return np.convolve(np.pad(y, n, mode="edge"), np.ones(n) / n, mode="same")[n:-n]


def pd_peaks(vb, pb, prominence_frac, smooth_v=0.0):
    """Transmission peaks on one sweep direction: centre, height, FWHM (drive volts).
    With a dither much wider than the linewidth the dither-averaged peak is double-humped (the dither dwells
    at its turning points), so peaks are found on a copy smoothed over smooth_v (the dither pp) and the centre
    is the midpoint of the half-maximum crossings."""
    base = float(np.median(pb))
    binw = (vb[-1] - vb[0]) / max(len(vb) - 1, 1)
    ps = smooth(pb, round(smooth_v / binw)) if binw > 0 else pb
    span = float(ps.max() - base)
    if span <= 0:
        return []
    idx, _ = find_peaks(ps, prominence=prominence_frac * span)
    if len(idx) == 0:
        return []
    w = peak_widths(ps, idx, rel_height=0.5)
    i_axis = np.arange(len(vb))
    out = []
    for k, i in enumerate(idx):
        left = float(np.interp(w[2][k], i_axis, vb))
        right = float(np.interp(w[3][k], i_axis, vb))
        win = (vb >= left) & (vb <= right)
        h = float(pb[win].max()) if win.any() else float(pb[i])
        out.append(dict(v=(left + right) / 2, height=h - base, base=base, fwhm=right - left))
    return out


def zero_crossing(vb, eb, v_target, half):
    """Error-signal zero crossing nearest the target peak (within +/- half), and the slope there (dE/dV)."""
    w = (vb > v_target - half) & (vb < v_target + half)
    sv, se = vb[w], eb[w]
    if len(sv) < 6:
        return None
    sc = np.where(np.diff(np.sign(se)) != 0)[0]
    if len(sc) == 0:
        return None
    slopes = (se[sc + 1] - se[sc]) / np.maximum(sv[sc + 1] - sv[sc], 1e-12)
    score = np.abs(slopes) * np.exp(-((sv[sc] - v_target) / (0.5 * half)) ** 2)
    k = sc[int(np.argmax(score))]
    vz = sv[k] - se[k] * (sv[k + 1] - sv[k]) / (se[k + 1] - se[k])
    near = np.abs(sv - vz) < 0.12 * half
    if near.sum() < 3:
        near = np.argsort(np.abs(sv - vz))[:5]
    slope = float(np.polyfit(sv[near], se[near], 1)[0])
    return float(vz), slope


def analyze_sweep(tr, c):
    """Peaks, lock target, error zero crossing, slope and health checks from one sweep capture.
    c: prominence, target_mv, rate [V/s], lpf_hz, lpf_order, dither_hz, dither_mvpp, kappa, ramp_hz, ramp_mvpp."""
    t, v, pd, err, up = tr["t"], tr["v"], tr["pd"], tr["err"], tr["up"]
    dt = float(t[1] - t[0]) if len(t) > 1 else 1e-6
    pds = smooth(pd, round(1.0 / (c["dither_hz"] * dt))) if c["dither_mvpp"] > 0 else pd   # dither-averaged
    halves = {}
    for name, sel in (("up", up), ("down", ~up)):
        if sel.sum() < 100:
            continue
        vb, (pb, eb) = binned(v[sel], [pds[sel], err[sel]])
        halves[name] = dict(vb=vb, pb=pb, eb=eb, peaks=pd_peaks(vb, pb, c["prominence"], c["dither_mvpp"] * 1e-3))
    if not halves:
        return dict(peaks=[], target=None, notes=["no sweep data"])
    names = list(halves)
    base = halves[names[0]]["peaks"]
    other = halves[names[1]]["peaks"] if len(names) > 1 else []
    peaks = []
    for p in base:
        q = min(other, key=lambda o: abs(o["v"] - p["v"]), default=None)
        if q is not None and abs(q["v"] - p["v"]) < 2 * max(p["fwhm"], q["fwhm"]):
            peaks.append(dict(v=(p["v"] + q["v"]) / 2, height=(p["height"] + q["height"]) / 2,
                              base=(p["base"] + q["base"]) / 2, fwhm=(p["fwhm"] + q["fwhm"]) / 2,
                              v_by={names[0]: p["v"], names[1]: q["v"]}))
        else:
            peaks.append(dict(p, v_by={names[0]: p["v"]}))
    peaks.sort(key=lambda p: -p["height"])
    for p in peaks:                                  # where each peak appears in time (once per sweep direction)
        p["t_ms"] = []
        for name, sel in (("up", up), ("down", ~up)):
            if name in p["v_by"]:
                ii = np.flatnonzero(sel)
                k = ii[int(np.argmin(np.abs(v[ii] - p["v_by"][name])))]
                p["t_ms"].append(float(t[k] * 1e3))
    res = dict(peaks=peaks, target=None, notes=[], err_max=float(np.max(np.abs(err))))
    if not peaks:
        res["notes"].append("no transmission peak above the threshold in the ramp window")
        return res
    if c.get("target_mv") is None:
        ti = 0
    else:
        vt = c["target_mv"] * 1e-3
        ti = int(np.argmin([abs(p["v"] - vt) for p in peaks]))
        if abs(peaks[ti]["v"] - vt) > max(3 * peaks[ti]["fwhm"], 0.002):
            res["notes"].append(f"WARN the chosen peak ({c['target_mv']:+.1f} mV) is not in this sweep: "
                                f"click a peak or press 'Use tallest peak'")
            return res
    tgt = peaks[ti]
    res.update(target=ti, target_v=tgt["v"])
    others = [p["v"] for i, p in enumerate(peaks) if i != ti]
    res["mode_spacing"] = min((abs(o - tgt["v"]) for o in others), default=None)
    half = max(1.25 * tgt["fwhm"], 1e-4)
    if res["mode_spacing"]:
        half = min(half, 0.45 * res["mode_spacing"])
    lag = c["lpf_order"] / (2 * math.pi * c["lpf_hz"]) * c["rate"]            # filter delay in volts
    zcs = {}
    for name, h in halves.items():
        zc = zero_crossing(h["vb"], h["eb"], tgt["v_by"].get(name, tgt["v"]), half)
        if zc:
            zcs[name] = zc
    if len(zcs) == 2:
        v0 = (zcs["up"][0] + zcs["down"][0]) / 2
    elif zcs:
        (name, (vz, _)), = zcs.items()
        v0 = vz - lag if name == "up" else vz + lag
    else:
        v0 = None
    slope = float(np.mean([z[1] for z in zcs.values()])) if zcs else None
    res.update(v0=v0, slope=slope)
    if v0 is not None:
        res["v0_t_ms"] = []
        for name, sel in (("up", up), ("down", ~up)):
            if name in zcs:
                ii = np.flatnonzero(sel)
                k = ii[int(np.argmin(np.abs(v[ii] - zcs[name][0])))]
                res["v0_t_ms"].append(float(t[k] * 1e3))
    n = res["notes"]
    lw = c.get("kappa") or tgt["fwhm"]
    t_cross = lw / max(c["rate"], 1e-12)
    cycles = c["dither_hz"] * t_cross
    res["cycles_per_linewidth"] = cycles
    if v0 is None:
        n.append("WARN no error zero crossing at the target peak: check the phase (Auto-optimize phase)")
    elif abs(v0 - tgt["v"]) > 0.25 * tgt["fwhm"]:
        n.append(f"WARN error zero crossing {1e3*(v0 - tgt['v']):+.2f} mV from the transmission peak")
    else:
        n.append("OK error zero crossing sits on the target transmission peak")
    if c["dither_mvpp"] > 0 and cycles < 10:
        k = 10 / max(cycles, 1e-9)
        n.append(f"WARN only {cycles:.1f} dither cycles per linewidth crossing (want >= 10), so the error signal "
                 f"is smeared. Fix: ramp at {c['ramp_hz']/k:.3g} Hz, or ramp {c['ramp_mvpp']/k:.3g} mVpp, or "
                 f"dither at {c['dither_hz']*k:.0f} Hz (locking is not affected: LOCK! sweeps slowly first)")
    if 2 * math.pi * c["lpf_hz"] * t_cross < 5 and c["dither_mvpp"] > 0:
        n.append("WARN the error low-pass smears features at this ramp speed (raise it, or slow the ramp)")
    if c["lpf_hz"] > c["dither_hz"] / 3:
        n.append("WARN error low-pass above dither/3: expect dither ripple on the error signal")
    if res["err_max"] > 0.95:
        n.append("WARN error signal near full scale (clipping): lower the error gain")
    elif res["err_max"] < 0.02 and c["dither_mvpp"] > 0:
        n.append("WARN error signal tiny: raise the error gain or check the phase")
    if c.get("kappa") and c["dither_mvpp"] > 0:
        n.append(f"INFO dither = {c['dither_mvpp']*1e-3/c['kappa']:.2f} linewidths pp (linewidth "
                 f"{c['kappa']*1e3:.2f} mV with the dither off; steepest lock slope at 0.71 = "
                 f"{0.71*c['kappa']*1e3:.1f} mVpp)")
    return res


def envelope(x, y, n=700):
    """Min/max decimation to about 2n points: keeps the dither envelope and spikes visible when plotting."""
    x, y = np.asarray(x), np.asarray(y)
    k = len(y) // n
    if k < 2:
        return x, y
    m = k * n
    yy = y[:m].reshape(n, k)
    xx = x[:m].reshape(n, k)[:, [0, -1]].ravel()
    lo, hi = yy.min(axis=1), yy.max(axis=1)
    first_is_min = yy.argmin(axis=1) < yy.argmax(axis=1)
    out = np.where(first_is_min[:, None], np.stack([lo, hi], 1), np.stack([hi, lo], 1)).ravel()
    return xx, out


def sine_pp(t, y, f):
    """Peak-to-peak amplitude of the component of y at frequency f (lock-in style fit)."""
    if f * (t[-1] - t[0]) < 3:
        return None
    z = np.mean((y - np.mean(y)) * np.exp(-2j * np.pi * f * t))
    return float(4 * abs(z))


def loop_model(f, ugf, p_gain, lp_list, driver_bw, delay):
    """Open-loop gain G(f) of the lock as the app sets it up (integrator unity gain at ugf, normalized P gain),
    with the error low-pass stages, a one-pole PZT driver and a pure delay. G > 0 means negative feedback.
    f may be a number or an array."""
    f = np.asarray(f, dtype=float)
    g = ugf / (1j * f) + p_gain
    for fc in lp_list:
        if fc > 0:
            g = g / (1 + 1j * f / fc)
    if driver_bw > 0:
        g = g / (1 + 1j * f / driver_bw)
    return g * np.exp(-2j * math.pi * f * delay)


def margins(fs, G):
    """Unity-gain frequency and phase margin [deg] from a sampled open-loop gain."""
    mag = np.abs(G)
    idx = np.flatnonzero((mag[:-1] >= 1) & (mag[1:] < 1))
    if len(idx) == 0:
        return None, None
    i = idx[0]
    x0, x1 = math.log(fs[i]), math.log(fs[i + 1])
    y0, y1 = math.log(mag[i]), math.log(mag[i + 1])
    fu = math.exp(x0 - y0 * (x1 - x0) / (y1 - y0))
    ph = np.unwrap(np.angle(G))
    phu = float(np.interp(math.log(fu), np.log(fs), ph))
    return fu, 180.0 + math.degrees(phu)


_PM_CACHE = {}


def predicted_margin(s, lp_list):
    key = (s["ugf_hz"], s["p_gain"], tuple(lp_list), s["driver_bw_hz"])
    if key not in _PM_CACHE:
        fs = np.logspace(-1, 6, 3000)
        _PM_CACHE[key] = margins(fs, loop_model(fs, s["ugf_hz"], s["p_gain"], lp_list, s["driver_bw_hz"], RP_LATENCY))
    return _PM_CACHE[key]


# ---------------------------------------------------------------------------------------------
# Controller: owns the Red Pitaya. All hardware access happens in the main thread (run()).
# ---------------------------------------------------------------------------------------------
class Controller:
    def __init__(self, host, config, settings):
        self.s = dict(settings)
        self.cmds = queue.Queue()
        self._pub_lock = threading.Lock()
        self.logs = collections.deque(maxlen=300)
        self.history = collections.deque(maxlen=20000)   # (t, transmission, error mean, error rms, setpoint, mode)
        self.version = 0                                 # bumps when the controller itself changes settings
        self.seq = 0                                     # bumps on every new set of traces
        self.meas_seq = 0                                # bumps on every new noise / loop measurement
        self.mode = "connecting"
        self.ctx = "acq"                                 # which dither is on OUT1: "acq" or "lock"
        self.traces = None
        self.drive = None
        self.analysis = {}
        self.lock_info = {}
        self.noise = None
        self.loop = None
        self.kappa = None                                # linewidth in OUT1 volts (dither off)
        self.amp_cal = 1.0                               # measured dither pp / requested pp
        self.dither_meas = None
        self.fps = 0.0
        self._t_last_frame = None
        self._cal_checks = 2
        self._last_dither = None
        self._clip_msg = None
        self._scope_cfg = {}
        self.ramp = None
        self._ramp_cache = None
        self._bad_since = None
        self._good_ival = collections.deque(maxlen=50)
        self._tick = 0
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("iq0", "pid0", "pid1", "asg0", "scope"):   # reserve: PyRPL tools (network analyzer) skip them
            try:
                getattr(self.r, name).owner = "cavity_lock_app"
            except Exception:
                pass
        for name in ("asg1", "pid2", "iq1", "iq2"):          # unused modules must not write to the outputs
            try:
                getattr(self.r, name).output_direct = "off"
            except Exception:
                pass
        self.r.pid0.i = 0
        self.log("connected")
        self.configure()
        self._start_acquire()

    # ---- small utilities ----
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def submit(self, cmd, arg=None):
        self.cmds.put((cmd, arg))

    def snapshot(self):
        with self._pub_lock:
            return dict(mode=self.mode, traces=self.traces, drive=self.drive, analysis=self.analysis,
                        lock=dict(self.lock_info), history=list(self.history), logs=list(self.logs),
                        settings=dict(self.s), version=self.version, seq=self.seq, ramp=self.ramp,
                        kappa=self.kappa, dither=self._dither(), dither_meas=self.dither_meas, ctx=self.ctx,
                        noise=self.noise, loop=self.loop, meas_seq=self.meas_seq, fps=self.fps)

    def _publish(self, **kw):
        with self._pub_lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if "traces" in kw:
                self.seq += 1
                now = time.time()
                if self._t_last_frame:
                    dt = now - self._t_last_frame
                    self.fps = 0.8 * self.fps + 0.2 / max(dt, 1e-3) if self.fps else 1 / max(dt, 1e-3)
                self._t_last_frame = now

    def _dither(self):
        """(frequency Hz, amplitude mVpp) of the dither for the present context."""
        s = self.s
        if self.ctx == "lock" and not s["lock_same_dither"]:
            return float(s["lock_dither_hz"]), float(s["lock_dither_mvpp"])
        return float(s["acq_dither_hz"]), float(s["acq_dither_mvpp"])

    def _filter_settle(self):
        return 6 * self.s["lpf_order"] / (2 * math.pi * self.s["lpf_hz"])

    def _lp_list(self):
        return [self.s["lpf_hz"]] * int(self.s["lpf_order"])

    # ---- hardware configuration ----
    def configure(self):
        s, r = self.s, self.r
        f_d, mvpp = self._dither()
        if (f_d, mvpp) != self._last_dither:
            self._last_dither = (f_d, mvpp)
            self._cal_checks = 2                         # re-check the dither amplitude on OUT1
        iq = r.iq0
        iq.input = "in1"
        iq.frequency = f_d
        iq.amplitude = mvpp * 1e-3 / 2 / self.amp_cal
        iq.phase = s["phase_deg"]
        iq.bandwidth = [s["lpf_hz"]] * int(s["lpf_order"])
        iq.acbandwidth = s["ac_hz"]
        iq.gain = 0
        iq.quadrature_factor = s["err_gain"]
        iq.output_signal = "quadrature"
        iq.output_direct = "out1" if mvpp > 0 else "off"
        p1 = r.pid1                                      # OUT2 monitor
        p1.i, p1.ival, p1.setpoint = 0, 0, 0
        p1.p = s["out2_gain"]
        p1.min_voltage, p1.max_voltage = -1.0, 1.0
        if s["out2_signal"] == "off":
            p1.output_direct = "off"
        else:
            p1.input = OUT2_SOURCE[s["out2_signal"]]
            p1.output_direct = "out2"
        for p in (r.pid0, r.pid1):
            try:
                p.inputfilter = [0, 0, 0, 0]
            except Exception:
                pass
        self._snap_to_hardware()

    def _snap_to_hardware(self):
        """The FPGA has a factor-2 ladder of filter corners and integer demodulation gains. Read back what it
        really uses and show that in the UI, so the numbers on screen are the numbers in the hardware."""
        s, iq, snapped = self.s, self.r.iq0, {}
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
            s.update(snapped)
            self.version += 1

    # ---- ramp ----
    def _ramp_limits(self, center, pp):
        d = self._dither()[1] * 1e-3 / 2
        lim = OUT_LIMIT - d
        lo, hi = center - pp / 2, center + pp / 2
        lo2, hi2 = float(np.clip(lo, -lim, lim)), float(np.clip(hi, -lim, lim))
        msg = f"ramp clipped to {lo2*1e3:+.0f}..{hi2*1e3:+.0f} mV (OUT1 limit)" if (lo2, hi2) != (lo, hi) else None
        if msg and msg != self._clip_msg:
            self.log(msg)
        self._clip_msg = msg
        return lo2, hi2

    def _ramp_on(self, lo, hi, hz, shape):
        p0 = self.r.pid0
        p0.i, p0.p = 0, 0
        p0.output_direct = "off"                          # the ramp sets OUT1 on its own
        p0.ival = 0
        self.r.asg0.setup(waveform="ramp" if shape == "triangle" else "halframp", frequency=hz,
                          amplitude=max(hi - lo, 0) / 2, offset=(hi + lo) / 2,
                          trigger_source="immediately", output_direct="out1")
        self.ramp = dict(lo=lo, hi=hi, hz=hz, shape=shape)
        self._ramp_cache = None

    def _ramp_off(self):
        a = self.r.asg0
        a.output_direct, a.amplitude, a.offset = "off", 0, 0

    def _start_acquire(self, quiet=False):
        s = self.s
        self.r.pid0.i = 0
        self.ctx = "acq"
        self.configure()
        lo, hi = self._ramp_limits(s["ramp_center_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3)
        self._ramp_on(lo, hi, s["ramp_hz"], s["ramp_shape"])
        self.mode = "acquire"
        if not quiet:
            f_d, mvpp = self._dither()
            self.log(f"acquiring: {s['ramp_shape']} ramp {lo*1e3:+.0f}..{hi*1e3:+.0f} mV at {s['ramp_hz']:g} Hz, "
                     f"dither {mvpp:g} mVpp at {f_d:g} Hz")

    # ---- scope ----
    def _scope_set(self, sc, **kw):
        """Write scope settings only when they change: every register access is a network round trip
        (~0.25 ms on a LAN), and a capture used to spend more time on settings than on data."""
        for k, v in kw.items():
            if self._scope_cfg.get(k) != v:
                try:
                    setattr(sc, k, v)
                except Exception:
                    if k not in ("average", "rolling_mode"):
                        raise
                self._scope_cfg[k] = v

    def capture(self, in1, in2, duration, trigger="immediately"):
        """One Red Pitaya scope shot -> (t, ch1, ch2), t starting at 0 (at the trigger when triggered)."""
        sc = self.r.scope
        opts = sorted(getattr(sc, "durations", []) or [])
        if opts:
            duration = min([d for d in opts if d >= duration * 0.999] or [opts[-1]])
        self._scope_set(sc, input1=in1, input2=in2, duration=duration, average=True, rolling_mode=False,
                        trigger_source=trigger)
        if trigger != "immediately":
            self._scope_set(sc, trigger_delay=duration / 2)   # trace starts at the trigger
        # arm / poll / read directly (method names differ between PyRPL 0.9.3 and 0.9.9); else scope.single()
        start = getattr(sc, "_start_trace_acquisition", None) or getattr(sc, "_start_acquisition", None)
        read = getattr(sc, "_get_trace", None) or getattr(sc, "_get_curve", None)
        timeout = 3 * duration + 2.0
        if start and read and hasattr(sc, "curve_ready"):
            start()
            t_arm = time.time()
            time.sleep(max(0.0, duration - 0.002))           # nothing can be ready before one trace length
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
        t = np.arange(n) * (duration / n)                    # local time axis: no register reads
        return t, data[0], data[1]

    def capture_sweep(self, averages=1, want_drive=False):
        """Photodiode and error over exactly one ramp period, with the drive voltage of every sample.
        Also returns the OUT1 waveform (dict) when it was captured this time."""
        rp = self.ramp
        hz = rp["hz"]
        dur = 1.02 / hz
        key = (rp["lo"], rp["hi"], hz, rp["shape"])
        drive = None
        if want_drive or self._ramp_cache is None or self._ramp_cache[0] != key:
            td, out1, ramp = self.capture("out1", "asg0", dur, trigger="asg0")
            self._ramp_cache = (key, ramp)
            drive = dict(t=td, out1=out1, ref=ramp, ref_name="ramp (asg0)")
            if np.ptp(ramp) < 0.2 * (rp["hi"] - rp["lo"]):
                self.log("WARN the ramp does not show up on the scope: check asg0")
        ramp = self._ramp_cache[1]
        pd = err = None
        for _ in range(int(averages)):
            t, p, e = self.capture("in1", "iq0", dur, trigger="asg0")
            pd = p if pd is None else pd + p
            err = e if err is None else err + e
        pd, err = pd / averages, err / averages
        n = min(len(ramp), len(pd), int(np.searchsorted(t, 1.0 / hz)))     # exactly one ramp period
        ramp, pd, err, t = ramp[:n], pd[:n], err[:n], t[:n]
        k = max(3, n // 200)
        sm = np.convolve(np.pad(ramp, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
        up = np.gradient(sm) > 0
        if drive is not None:
            m = min(n, len(drive["t"]))
            drive = {k2: (v2[:m] if isinstance(v2, np.ndarray) else v2) for k2, v2 in drive.items()}
        return dict(t=t, v=ramp, pd=pd * self.s["in1_scale"], err=err, up=up), drive

    def _ctx(self, target_mv):
        s, rp = self.s, self.ramp
        f_d, mvpp = self._dither()
        pp = rp["hi"] - rp["lo"]
        rate = (2 if rp["shape"] == "triangle" else 1) * pp * rp["hz"]
        return dict(prominence=s["prominence"], target_mv=target_mv, rate=rate, lpf_hz=s["lpf_hz"],
                    lpf_order=s["lpf_order"], dither_hz=f_d, dither_mvpp=mvpp, kappa=self.kappa,
                    ramp_hz=rp["hz"], ramp_mvpp=pp * 1e3)

    def _check_drive(self, drive):
        """Measure the dither actually on OUT1 (OUT1 minus ramp or setpoint) and calibrate its amplitude."""
        f_d, want = self._dither()
        pp = sine_pp(drive["t"], drive["out1"] - drive["ref"], f_d)
        if pp is None:
            return
        self.dither_meas = pp * 1e3
        if self._cal_checks > 0 and want > 0 and pp > 0:
            self._cal_checks -= 1
            ratio = pp * 1e3 / want
            if abs(ratio - 1) > 0.02:
                self.amp_cal *= ratio
                self.log(f"dither on OUT1 measured {pp*1e3:.2f} mVpp for {want:g} requested: corrected")
                self.configure()
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
                        if self.mode in ("busy", "locking", "measuring"):
                            self.mode = "held"
            except queue.Empty:
                pass
            try:
                if self.mode == "acquire":
                    self._acquire_tick()
                elif self.mode in ("locked", "held"):
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
                return              # the UI sent values from before the controller changed settings: stale
            changed = {k for k, v in arg.items() if s.get(k) != v}
            if not changed:
                return
            f_before = self._dither()[0]
            s.update(arg)
            self.configure()
            ramp_keys = {"ramp_shape", "ramp_hz", "ramp_mvpp", "ramp_center_mv", "acq_dither_mvpp"}
            if self.mode == "acquire" and changed & ramp_keys:
                self._start_acquire(quiet=True)
            if self.mode == "locked" and changed & {"ugf_hz", "p_gain"} and self.lock_info.get("slope"):
                self._set_gains(s["ugf_hz"])
            if self.mode == "locked" and changed & {"clamp_mv", "clamp_auto"}:
                lo, hi = self._clamp(self.lock_info["v0"], self.lock_info.get("spacing"))
                self.r.pid0.min_voltage, self.r.pid0.max_voltage = lo, hi
                self.lock_info["clamp"] = (lo, hi)
            if self.ctx == "lock" and self.mode in ("locked", "held") and self._dither()[0] != f_before:
                self.log("note: a new dither frequency can shift the demodulation phase; press LOCK! to re-fit")
            if changed & {"ugf_hz", "p_gain", "lpf_hz", "lpf_order", "driver_bw_hz"}:
                fu, pm = predicted_margin(s, self._lp_list())
                if pm is not None and pm < 45:
                    self.log(f"WARN predicted phase margin {pm:.0f} deg at {fu:.0f} Hz: the loop may oscillate")
            if changed - {"panels", "ui_hz", "drive_every"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "acquire":
            self._start_acquire()
        elif cmd == "autophase":
            self._auto_phase()
        elif cmd == "target":
            self._pick_target(arg)
        elif cmd == "tallest":
            s["target_mv"] = None
            self.log("lock target: tallest peak")
        elif cmd == "lock":
            self._lock()
        elif cmd == "unlock":
            self.r.pid0.i = 0
            if self.mode in ("locked", "locking", "busy"):
                self.mode = "held"
            self.log(f"unlocked; setpoint held at {float(self.r.pid0.ival)*1e3:+.2f} mV")
        elif cmd == "noise":
            self._measure_noise()
        elif cmd == "loop":
            self._measure_loop()
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            self.configure()
            self.version += 1
            if self.mode == "acquire":
                self._start_acquire(quiet=True)
            self.log("settings loaded")

    # ---- acquisition ----
    def _acquire_tick(self):
        s = self.s
        self._tick += 1
        want = ("drive" in s["panels"] and self._tick % max(int(s["drive_every"]), 1) == 0) or self._cal_checks > 0
        tr, drive = self.capture_sweep(s["averages"], want_drive=want)
        an = analyze_sweep(tr, self._ctx(s["target_mv"]))
        if s["target_mv"] is not None and an.get("target_v") is not None:
            s["target_mv"] = an["target_v"] * 1e3        # follow the chosen peak as it drifts
        if drive is not None:
            self._check_drive(drive)
        self._publish(traces=dict(kind="sweep", **tr), analysis=an,
                      **({"drive": drive} if drive is not None else {}))

    def _pick_target(self, t_ms):
        tr, an = self.traces, self.analysis or {}
        if self.mode != "acquire" or tr is None or tr.get("kind") != "sweep":
            self.log("pick the lock target while acquiring (press Acquire)")
            return
        if not an.get("peaks"):
            self.log("no peaks in this sweep to pick from")
            return
        k = int(np.argmin(np.abs(tr["t"] * 1e3 - t_ms)))
        v = float(tr["v"][k])
        p = min(an["peaks"], key=lambda q: abs(q["v"] - v))
        self.s["target_mv"] = p["v"] * 1e3
        self.log(f"lock target: peak at {p['v']*1e3:+.1f} mV (height {p['height']:.3f} V)")
        self._publish(analysis=analyze_sweep({k2: tr[k2] for k2 in ("t", "v", "pd", "err", "up")},
                                             self._ctx(self.s["target_mv"])))

    def _fit_phase(self, grab, v_t, half):
        """Set the demodulation phase that maximizes the error signal within +/- half of v_t
        (4 captures, cos^2 fit), with the sign chosen so the slope at the zero crossing is negative."""
        s = self.s
        phi0 = s["phase_deg"]
        energies = []
        for k in range(4):
            self.r.iq0.phase = phi0 + 45 * k
            time.sleep(self._filter_settle())
            tr = grab()
            w = np.abs(tr["v"] - v_t) < half
            e = tr["err"][w]
            energies.append(float(np.sum((e - e.mean()) ** 2)) if len(e) else 0.0)
        c, sn = energies[0] - energies[2], energies[1] - energies[3]
        phi = phi0 + 0.5 * math.degrees(math.atan2(sn, c))
        self.r.iq0.phase = phi
        time.sleep(self._filter_settle())
        tr = grab()
        an = analyze_sweep(tr, self._ctx(v_t * 1e3))
        if an.get("slope") is not None and an["slope"] > 0:
            phi += 180
        s["phase_deg"] = round(float((phi + 180) % 360 - 180), 1)
        self.configure()
        return s["phase_deg"], max(energies) / max(min(energies), 1e-30)

    def _grab_and_show(self):
        tr, _ = self.capture_sweep(1)
        self._publish(traces=dict(kind="sweep", **tr), analysis=analyze_sweep(tr, self._ctx(self.s["target_mv"])))
        return tr

    def _auto_phase(self):
        s = self.s
        if self.mode != "acquire":
            self.log("Auto-optimize phase works while acquiring (press Acquire first)")
            return
        an = self.analysis or {}
        if an.get("target") is None:
            self.log("auto phase: no target peak in the sweep")
            return
        tgt = an["peaks"][an["target"]]
        self.mode = "busy"
        try:
            self.log(f"auto phase 1/2: linewidth of the peak at {tgt['v']*1e3:+.1f} mV with the dither off")
            self.r.iq0.output_direct = "off"
            tr, _ = self.capture_sweep(s["averages"])
            self.configure()                                  # dither back on
            off = analyze_sweep(tr, dict(self._ctx(tgt["v"] * 1e3), dither_mvpp=0))
            if off.get("target") is not None:
                self.kappa = off["peaks"][off["target"]]["fwhm"]
                self.log(f"  linewidth {self.kappa*1e3:.2f} mV of drive; steepest lock slope with "
                         f"{0.71*self.kappa*1e3:.1f} mVpp of dither (now {self._dither()[1]:g})")
            half = max(1.2 * tgt["fwhm"], 3 * (self.kappa or 0))
            if an.get("mode_spacing"):
                half = min(half, 0.45 * an["mode_spacing"])
            self.log("auto phase 2/2: fitting the phase")
            time.sleep(self._filter_settle())
            phi, contrast = self._fit_phase(self._grab_and_show, tgt["v"], half)
            tr = self._grab_and_show()
            new = analyze_sweep(tr, self._ctx(s["target_mv"]))
            peak = new.get("err_max", 0)
            self.log(f"auto phase done: phase {phi:+.1f} deg (contrast {contrast:.0f}:1), error peak {peak:.2f} V")
            if peak > 0.9:
                self.log("  the error signal clips: lower the error gain")
            elif 0 < peak < 0.25:
                self.log(f"  tip: error gain {max(1, round(s['err_gain'] * 0.5 / peak))} would bring the error "
                         f"peak to ~0.5 V")
            self.version += 1
        finally:
            if self.mode == "busy":
                self.mode = "acquire"

    # ---- locking ----
    def _clamp(self, v0, spacing):
        hw = 0.4 * spacing if (self.s["clamp_auto"] and spacing) else self.s["clamp_mv"] * 1e-3
        return max(v0 - hw, -OUT_LIMIT), min(v0 + hw, OUT_LIMIT)

    def _set_gains(self, ugf):
        S = self.lock_info["slope"]
        self.r.pid0.i = -ugf / S                          # i*S < 0: negative feedback, |i*S| = bandwidth
        self.r.pid0.p = -self.s["p_gain"] / S
        i_act = float(self.r.pid0.i)
        self.lock_info["ugf_actual"] = abs(i_act * S)
        return i_act

    def _lock(self):
        if self.mode == "acquire":
            an = self.analysis or {}
            if an.get("target") is None:
                self.log("LOCK refused: no target peak in the sweep")
                return False
            tgt = an["peaks"][an["target"]]
            center = an["v0"] if an.get("v0") is not None else tgt["v"]
            self.log(f"LOCK 1/3: going to the peak at {tgt['v']*1e3:+.1f} mV")
            ref = dict(fwhm=tgt["fwhm"], spacing=an.get("mode_spacing"), relocks=0)
        elif self.mode in ("held", "locked") and self.lock_info:
            center = float(self.r.pid0.ival)
            self.log(f"LOCK 1/3: re-locking around the setpoint {center*1e3:+.2f} mV")
            ref = dict(fwhm=self.lock_info["fwhm"], spacing=self.lock_info.get("spacing"), relocks=0)
        else:
            self.log("LOCK: start from Acquire")
            return False
        return self._zoom_and_lock(center, ref, refine=self.s["refine_phase"])

    def _zoom_and_lock(self, center, ref, refine=True):
        """Slow sweep across the peak with the lock dither -> (phase), zero crossing, slope; then engage."""
        s = self.s
        self.mode = "locking"
        self.r.pid0.i = 0
        self.ctx = "lock"
        self.configure()
        f_d, d_mvpp = self._dither()
        kap = self.kappa or ref["fwhm"]
        half = max(2.5 * ref["fwhm"], 1.5 * d_mvpp * 1e-3, 4 * kap)
        if ref.get("spacing"):
            half = min(half, 0.45 * ref["spacing"])
        f_z = float(np.clip(f_d * kap / (40 * half), 1.0, max(s["ramp_hz"], 1.0)))
        lo, hi = self._ramp_limits(center, 2 * half)
        self._ramp_on(lo, hi, f_z, "triangle")
        self.log(f"  slow sweep {lo*1e3:+.1f}..{hi*1e3:+.1f} mV at {f_z:.2g} Hz, lock dither {d_mvpp:g} mVpp "
                 f"at {f_d:g} Hz")
        saved_target = s["target_mv"]
        s["target_mv"] = center * 1e3
        try:
            time.sleep(max(1.5 / f_z, self._filter_settle()))
            if refine:
                self.log("LOCK 2/3: fitting the phase at the lock dither")
                old = s["phase_deg"]
                phi, contrast = self._fit_phase(self._grab_and_show, center, 0.9 * half)
                self.log(f"  phase {old:+.1f} -> {phi:+.1f} deg (contrast {contrast:.0f}:1)")
                self.version += 1
            tr = self._grab_and_show()
            an = analyze_sweep(tr, self._ctx(center * 1e3))
        finally:
            s["target_mv"] = saved_target
        if an.get("target") is None or an.get("v0") is None or not an.get("slope") or abs(an["v0"] - center) > half:
            self.log("LOCK refused: no clean error zero crossing in the slow sweep (check phase / dither)")
            self._park(center)
            return False
        tgt = an["peaks"][an["target"]]
        ref = dict(ref, height=tgt["height"], fwhm=tgt["fwhm"])
        self.log(f"LOCK 3/3: parking at {an['v0']*1e3:+.3f} mV (slope {an['slope']:+.1f} /V) and closing the loop")
        return self._engage(an["v0"], an["slope"], ref)

    def _park(self, v):
        self._ramp_off()
        p0 = self.r.pid0
        p0.i, p0.p = 0, 0
        p0.min_voltage, p0.max_voltage = -OUT_LIMIT, OUT_LIMIT
        p0.ival = v
        p0.output_direct = "out1"
        self.mode = "held"

    def _engage(self, v0, S, ref):
        s = self.s
        lo, hi = self._clamp(v0, ref.get("spacing"))
        self._ramp_off()
        p0 = self.r.pid0
        p0.i, p0.p, p0.setpoint = 0, 0, 0
        p0.min_voltage, p0.max_voltage = lo, hi
        p0.input = "iq0"
        p0.ival = v0
        p0.output_direct = "out1"
        self.lock_info = dict(v0=v0, slope=S, height=ref["height"], fwhm=ref["fwhm"], spacing=ref.get("spacing"),
                              clamp=(lo, hi), t0=time.time(), relocks=ref.get("relocks", 0))
        self.log(f"  clamp {lo*1e3:+.1f}..{hi*1e3:+.1f} mV")
        steps = [s["ugf_hz"] / 10, s["ugf_hz"] / 3, s["ugf_hz"]] if s["gain_ramp"] else [s["ugf_hz"]]
        for u in steps:
            i_act = self._set_gains(u)
            if i_act == 0:
                p0.i = 0
                self.log(f"  integrator gain {-u/S:.3g} Hz is below PyRPL's resolution: raise the error gain")
                self.mode = "held"
                return False
            time.sleep(0.4)
            ival = float(p0.ival)
            if ival <= lo + 0.03 * (hi - lo) or ival >= hi - 0.03 * (hi - lo):
                p0.i = 0
                self.log("  ran to the clamp while engaging: wrong peak or phase. Holding.")
                self.mode = "held"
                return False
        self.log(f"  loop bandwidth {self.lock_info['ugf_actual']:.2f} Hz; LOCKED")
        self.mode = "locked"
        self._bad_since = None
        self._good_ival.clear()
        return True

    def _live_tick(self):
        """Time-domain traces while locked or held; watchdog when locked."""
        s = self.s
        t, pd, err = self.capture("in1", "iq0", s["lock_window_ms"] * 1e-3)
        pd = pd * s["in1_scale"]
        self._tick += 1
        drive = None
        if ("drive" in s["panels"] and (self._tick % max(int(s["drive_every"]), 1) == 0 or self.drive is None
                                        or self.drive.get("ref_name") != "setpoint (pid0)")) or self._cal_checks > 0:
            td, out1, sp = self.capture("out1", "pid0", s["lock_window_ms"] * 1e-3)
            drive = dict(t=td, out1=out1, ref=sp, ref_name="setpoint (pid0)")
            self._check_drive(drive)
        ival = float(self.r.pid0.ival)
        now = time.time()
        T, em, erms = float(np.mean(pd)), float(np.mean(err)), float(np.std(err))
        self.history.append((now, T, em, erms, ival, self.mode))
        self._publish(traces=dict(kind="time", t=t, pd=pd, err=err), **({"drive": drive} if drive else {}))
        if self.mode != "locked":
            time.sleep(0.05)
            return
        li = self.lock_info
        lo, hi = li["clamp"]
        at_edge = ival < lo + 0.05 * (hi - lo) or ival > hi - 0.05 * (hi - lo)
        low = T < s["unlock_frac"] * li["height"]
        li.update(transmission=T, frac=T / li["height"] if li["height"] else 0, err_rms=erms, setpoint=ival)
        if not (at_edge or low):
            self._bad_since = None
            self._good_ival.append(ival)
            return
        self._bad_since = self._bad_since or now
        if now - self._bad_since < s["unlock_s"] and not at_edge:
            return
        why = "setpoint at the clamp" if at_edge else "transmission low"
        self.log(f"UNLOCK ({why}) at {ival*1e3:+.3f} mV")
        self.history.append((now, T, em, erms, ival, "unlock"))
        self.r.pid0.i = 0
        self.mode = "held"
        if s["auto_relock"]:
            self._relock()

    def _relock(self):
        li = self.lock_info
        tries = li.get("relocks", 0)
        if tries >= 5:
            self.log("relock: too many tries; holding. Press Acquire, then LOCK!")
            return
        center = float(np.median(self._good_ival)) if self._good_ival else li["v0"]
        span = max((li["clamp"][1] - li["clamp"][0]) / 2, 6 * li["fwhm"])
        f_d = self._dither()[0]
        kap = self.kappa or li["fwhm"]
        f = float(np.clip(f_d * kap / (40 * span), 1.0, max(self.s["ramp_hz"], 1.0)))
        lo, hi = self._ramp_limits(center, 2 * span)
        self.log(f"relock {tries+1}: searching {lo*1e3:+.1f}..{hi*1e3:+.1f} mV")
        self.mode = "locking"
        self._ramp_on(lo, hi, f, "triangle")
        time.sleep(max(1.5 / f, self._filter_settle()))
        saved = self.s["target_mv"]
        self.s["target_mv"] = None
        try:
            tr = self._grab_and_show()
            an = analyze_sweep(tr, self._ctx(None))
        finally:
            self.s["target_mv"] = saved
        li["relocks"] = tries + 1
        if an.get("peaks") and an["peaks"][0]["height"] > 0.6 * li["height"]:
            ref = dict(fwhm=li["fwhm"], spacing=li.get("spacing"), relocks=tries + 1)
            if self._zoom_and_lock(an["peaks"][0]["v"], ref, refine=False):
                self.log("relocked")
                return
        else:
            self.log("relock failed: TEM00 not found near the last lock point; holding")
            self._park(center)

    # ---- diagnostics ----
    def _show_panel(self, name):
        if name not in self.s["panels"]:
            self.s["panels"] = [p for p in PANELS if p in self.s["panels"] or p == name]
            self.version += 1                            # the UI's "Show" boxes follow

    def _kappa_drive(self):
        """Linewidth in OUT1 volts: the dither-off value from Auto-optimize phase if measured, else the swept
        FWHM of the locked peak (which the lock dither broadens a little)."""
        return (self.kappa, "dither-off linewidth") if self.kappa else (self.lock_info.get("fwhm"), "swept FWHM")

    def _measure_noise(self):
        """Spectra of the residual detuning (error signal / slope) and of the transmitted intensity, in two
        bands: ~1 s traces (1 Hz resolution, up to ~7 kHz) and ~8 ms traces (up to ~1 MHz)."""
        s = self.s
        if self.mode != "locked":
            self.log("Measure noise works while locked")
            return
        li = self.lock_info
        S = li["slope"]
        kap, kap_src = self._kappa_drive()
        self.mode = "measuring"
        try:
            self.log(f"measuring noise: {int(s['noise_avg'])} traces of ~1 s and of ~8 ms (keep the lab quiet)")
            bands = []
            for dur, nper in ((1.07, 8192), (0.0084, 4096)):
                pe = pp = None
                tm = []
                f = None
                for _ in range(int(s["noise_avg"])):
                    t, pd, err = self.capture("in1", "iq0", dur)
                    fs = 1.0 / (t[1] - t[0])
                    pd = pd * s["in1_scale"]
                    f, a = welch(err, fs=fs, nperseg=min(nper, len(err)), detrend="linear")
                    _, b = welch(pd, fs=fs, nperseg=min(nper, len(pd)), detrend="linear")
                    pe = a if pe is None else pe + a
                    pp = b if pp is None else pp + b
                    tm.append(float(np.mean(pd)))
                n = int(s["noise_avg"])
                bands.append((f, pe / n, pp / n, float(np.mean(tm))))
            f_lo, e_lo, p_lo, T_lo = bands[0]
            f_hi, e_hi, p_hi, T_hi = bands[1]
            cut = f_lo[-1] * 0.4                           # stitch: low band below, high band above
            sel_lo = (f_lo > 0) & (f_lo <= cut)
            sel_hi = f_hi > cut
            f = np.concatenate([f_lo[sel_lo], f_hi[sel_hi]])
            S_err = np.concatenate([e_lo[sel_lo], e_hi[sel_hi]])
            S_pd = np.concatenate([p_lo[sel_lo], p_hi[sel_hi]])
            T = 0.5 * (T_lo + T_hi)
            det_lw = np.sqrt(S_err) / abs(S) / kap if kap else None          # linewidths / sqrt(Hz)
            rin = np.sqrt(S_pd) / max(T, 1e-9)                               # 1 / sqrt(Hz)
            df = np.gradient(f)

            def band_rms(asd, lo, hi):
                m = (f >= lo) & (f < hi)
                return float(np.sqrt(np.sum(asd[m] ** 2 * df[m])))
            edges = [(0.5, 10), (10, 100), (100, 1e3), (1e3, 1e4), (1e4, 1e6)]
            rows = []
            for lo, hi in edges:
                rows.append(dict(band=f"{lo:g}-{hi:g} Hz", det=band_rms(det_lw, lo, hi) if det_lw is not None else None,
                                 rin=band_rms(rin, lo, hi)))
            f_d, d_mvpp = self._dither()
            m_pp = d_mvpp * 1e-3 / kap if kap else None
            self.noise = dict(f=f.tolist(), det_lw=None if det_lw is None else det_lw.tolist(), rin=rin.tolist(),
                              kappa_src=kap_src, kappa_v=kap, kappa_mhz=s["kappa_mhz"], T=T, rows=rows, f_d=f_d,
                              dither_pp_lw=m_pp, t=time.time())
            tot_det = band_rms(det_lw, 0.5, 1e6) if det_lw is not None else float("nan")
            tot_rin = band_rms(rin, 0.5, 1e6)
            self.log(f"noise: residual detuning {tot_det*100:.2f}% of the linewidth rms "
                     f"(~{tot_det*s['kappa_mhz']*1e3:.0f} kHz), intensity {tot_rin*100:.2f}% rms (0.5 Hz-1 MHz)")
            if m_pp:
                self.log(f"  the dither itself modulates the transmission at {2*f_d:g} Hz by ~{(1-1/(1+m_pp**2))*100:.0f}% "
                         f"peak-to-peak (dither = {m_pp:.2f} linewidths pp; roughly the square of that)")
            self._show_panel("noise spectrum")
            with self._pub_lock:
                self.meas_seq += 1
        finally:
            if self.mode == "measuring":
                self.mode = "locked"

    def _measure_loop(self):
        """Open-loop gain with PyRPL's network analyzer: inject a small sine on OUT1 (summed with the lock) and
        measure the PID output c and the error e. c/x = -G/(1+G) gives G; (e/x)/(1 + c/x) gives the plant."""
        s = self.s
        if self.mode != "locked":
            self.log("Measure loop works while locked")
            return
        kap, _ = self._kappa_drive()
        amp = float(np.clip(s["na_amp_frac"] * (kap or 0.005), 1e-4, 0.05))
        na = self.p.networkanalyzer
        self.mode = "measuring"
        res = {}
        try:
            f_stop = min(s["na_stop_hz"], 0.45 * self._dither()[0])
            self.log(f"measuring the loop: {int(s['na_points'])} points, {s['na_start_hz']:g}-{f_stop:g} Hz, "
                     f"excitation {amp*1e3:.2f} mV on OUT1")
            for inp in ("pid0", "iq0"):
                try:
                    na.trace_average = 1
                except Exception:
                    pass
                na.setup(start_freq=s["na_start_hz"], stop_freq=f_stop, points=int(s["na_points"]), rbw=100,
                         average_per_point=1, amplitude=amp, input=inp, output_direct="out1",
                         acbandwidth=0, logscale=True, auto_bandwidth=True, q_factor_min=5, auto_amplitude=False)
                data = np.asarray(na.single(), dtype=complex)
                res[inp] = data
                res["f"] = np.asarray(na.frequencies, dtype=float)
                if self.mode != "measuring":
                    break
            try:
                na.iq.amplitude = 0
                na.iq.output_direct = "off"
            except Exception:
                pass
            T = res["pid0"]                                   # c/x = -G/(1+G)
            G = -T / (1 + T)
            P = res["iq0"] / (1 + T) if "iq0" in res else None
            f = res["f"]
            fu, pm = margins(f, G)
            Gm = loop_model(f, self.lock_info.get("ugf_actual", s["ugf_hz"]), s["p_gain"], self._lp_list(),
                            s["driver_bw_hz"], RP_LATENCY)
            self.loop = dict(f=f.tolist(), G_mag=np.abs(G).tolist(), G_ph=np.degrees(np.unwrap(np.angle(G))).tolist(),
                             P_mag=None if P is None else np.abs(P).tolist(),
                             P_ph=None if P is None else np.degrees(np.unwrap(np.angle(P))).tolist(),
                             M_mag=np.abs(Gm).tolist(), M_ph=np.degrees(np.unwrap(np.angle(Gm))).tolist(),
                             ugf=fu, pm=pm, t=time.time())
            self.log("loop: " + (f"unity gain at {fu:.1f} Hz, phase margin {pm:.0f} deg" if fu else
                                 "no unity-gain crossing in the measured range (widen it)"))
            self._show_panel("loop (Bode)")
            with self._pub_lock:
                self.meas_seq += 1
        finally:
            if self.mode == "measuring":
                self.mode = "locked"


# ---------------------------------------------------------------------------------------------
# Browser UI (Dash). The server sends compact data; the browser builds the plots (Plotly.react, SVG: in tests WebGL refreshed about 2x slower).
# ---------------------------------------------------------------------------------------------
SCOPE_JS = """
function(fr) {
  if (!fr) { return [window.dash_clientside.no_update, window.dash_clientside.no_update]; }
  var C = {pd: '#222222', err: '#1f5fa8', out1: '#8a8a8a', ref: '#d35400', tgt: '#1e8e3e'};
  var rows = fr.rows, n = rows.length, data = [], shapes = [], ann = [], lay = {};
  var gap = 0.06, h = (1 - gap * (n - 1)) / Math.max(n, 1);
  var titles = {transmission: 'Transmission (photodiode, IN1) [V]', error: 'Error signal (iq0) [V]',
                drive: 'Drive on OUT1 [mV]'};
  rows.forEach(function(r, i) {
    var ya = i === 0 ? 'y' : 'y' + (i + 1), key = i === 0 ? 'yaxis' : 'yaxis' + (i + 1);
    var top = 1 - i * (h + gap);
    lay[key] = {domain: [top - h, top], anchor: 'x', zeroline: false};
    ann.push({text: titles[r], x: 0.5, xref: 'paper', y: top, yref: 'paper', yanchor: 'bottom', showarrow: false,
              font: {size: 13}});
    if (r === 'transmission' && fr.pd) {
      data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr.pd, name: 'photodiode', yaxis: ya,
                 line: {color: C.pd, width: 1}});
      (fr.peaks || []).forEach(function(p) {
        data.push({type: 'scatter', mode: 'markers+text', x: p.t, y: p.t.map(function() { return p.y; }),
                   text: p.t.map(function() { return p.label; }), textposition: 'top center', yaxis: ya,
                   marker: {symbol: p.tgt ? 'star' : 'triangle-down', size: p.tgt ? 13 : 9,
                            color: p.tgt ? C.tgt : 'black'}, showlegend: false, hoverinfo: 'skip'});
      });
      if (fr.lock_height !== null && fr.lock_height !== undefined) {
        shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: fr.lock_height, y1: fr.lock_height,
                     line: {color: C.tgt, dash: 'dot', width: 1}});
      }
    }
    if (r === 'error' && fr.err) {
      data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr.err, name: 'error', yaxis: ya,
                 line: {color: C.err, width: 1}});
      shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: 0, y1: 0, line: {color: 'gray', width: 1}});
    }
    if (r === 'drive' && fr.dt) {
      data.push({type: 'scatter', mode: 'lines', x: fr.dt, y: fr.dout, name: 'OUT1', yaxis: ya,
                 line: {color: C.out1, width: 1}});
      data.push({type: 'scatter', mode: 'lines', x: fr.dt, y: fr.dref, name: fr.dref_name, yaxis: ya,
                 line: {color: C.ref, width: 1.5}});
    }
  });
  (fr.v0_t || []).forEach(function(tv) {
    shapes.push({type: 'line', xref: 'x', x0: tv, x1: tv, yref: 'paper', y0: 0, y1: 1,
                 line: {color: C.tgt, dash: 'dash', width: 1}});
  });
  lay.xaxis = {title: {text: fr.xtitle}, anchor: n > 0 ? (n === 1 ? 'y' : 'y' + n) : 'y'};
  lay.shapes = shapes; lay.annotations = ann;
  lay.height = 200 * Math.max(n, 1) + 70; lay.margin = {l: 55, r: 15, t: 30, b: 45};
  lay.template = 'plotly_white'; lay.hovermode = 'x'; lay.uirevision = fr.uirev;
  lay.legend = {orientation: 'h', y: 1.04, x: 1, xanchor: 'right', yanchor: 'bottom'};
  lay.plot_bgcolor = 'white'; lay.paper_bgcolor = 'white';
  var scope = {data: data, layout: lay};
  var hist = window.dash_clientside.no_update;
  if (fr.hist) {
    var H = fr.hist, hd = [
      {type: 'scatter', mode: 'lines', x: H.t, y: H.sp, name: 'setpoint [mV]', line: {color: C.ref}},
      {type: 'scatter', mode: 'lines', x: H.t, y: H.T, name: 'transmission [V]', yaxis: 'y2',
       line: {color: C.pd, width: 1}}];
    var hs = (H.unlocks || []).map(function(u) {
      return {type: 'line', xref: 'x', x0: u, x1: u, yref: 'paper', y0: 0, y1: 1, line: {color: 'red', width: 1}}; });
    hist = {data: hd, layout: {title: {text: 'Setpoint (OUT1 DC from pid0) over time', font: {size: 14}}, height: 260,
            xaxis: {title: {text: 'time [s]'}}, yaxis: {title: {text: 'setpoint [mV]'}},
            yaxis2: {title: {text: 'transmission [V]'}, overlaying: 'y', side: 'right'}, shapes: hs,
            margin: {l: 55, r: 55, t: 35, b: 40}, template: 'plotly_white', uirevision: 'hist',
            plot_bgcolor: 'white', paper_bgcolor: 'white',
            legend: {orientation: 'h', y: 1.12, x: 1, xanchor: 'right'},
            annotations: H.t.length ? [] : [{text: 'fills in once locked (or held)', showarrow: false,
                                            xref: 'paper', yref: 'paper', x: 0.5, y: 0.5}]}};
  }
  return [scope, hist];
}
"""


def build_app(ctl):
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    app = Dash(__name__, title="Cavity lock", update_title=None)
    FIELD = {f[0]: f for f in FIELDS}
    css_box = {"border": "1px solid #ccc", "borderRadius": "8px", "padding": "8px 10px", "marginBottom": "10px"}
    btn = {"margin": "3px", "padding": "6px 10px", "fontSize": "14px", "cursor": "pointer"}

    def to_ui(key, val):
        kind = FIELD[key][4]
        return (["on"] if val else []) if kind == "check" else val

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "check":
            return bool(val)
        if kind == "multi":
            return [p for p in PANELS if p in (val or [])]
        if kind == "select":
            return val
        if val is None:
            return ctl.s.get(key, default)
        return int(val) if isinstance(default, int) and not isinstance(default, bool) else float(val)

    def control(key):
        _, label, _, _, kind, opt = FIELD[key]
        val = to_ui(key, ctl.s.get(key, FIELD[key][2]))
        if kind == "num":
            comp = dcc.Input(id=f"s-{key}", type="number", value=val, debounce=True, step="any",
                             style={"width": "90px"}, **opt)
        elif kind == "check":
            comp = dcc.Checklist(id=f"s-{key}", options=[{"label": "", "value": "on"}], value=val,
                                 style={"display": "inline-block"})
        elif kind == "multi":
            return html.Div([html.Span(label, style={"fontSize": "13px"}),
                             dcc.Checklist(id=f"s-{key}", options=[{"label": " " + o, "value": o} for o in opt["options"]],
                                           value=val, inline=True, inputStyle={"marginLeft": "8px"},
                                           style={"fontSize": "13px"})], style={"margin": "3px 0"})
        else:
            comp = dcc.Dropdown(id=f"s-{key}", options=[{"label": o, "value": o} for o in opt["options"]],
                                value=val, clearable=False, style={"width": "130px"})
        return html.Div([html.Span(label, style={"flex": "1", "fontSize": "13px"}), comp],
                        style={"display": "flex", "alignItems": "center", "gap": "6px", "margin": "3px 0"})

    buttons = html.Div([
        html.Button("Acquire", id="btn-acquire", style=btn),
        html.Button("Auto-optimize phase", id="btn-phase", style=btn),
        html.Button("LOCK!", id="btn-lock",
                    style=dict(btn, background="#1e8e3e", color="white", fontWeight="bold", fontSize="16px")),
        html.Button("Unlock (hold)", id="btn-unlock", style=btn),
        html.Button("Measure noise", id="btn-noise", style=btn),
        html.Button("Measure loop", id="btn-loop", style=btn),
        html.Button("Save settings", id="btn-save", style=btn),
        html.Button("Load settings", id="btn-load", style=btn),
    ], style=css_box)
    target_box = html.Div([
        html.B("Lock target"),
        html.Div(id="target-text", style={"fontSize": "13px", "margin": "4px 0"}),
        html.Div("Click a transmission peak in the plot to choose it.", style={"fontSize": "12px", "color": "#666"}),
        html.Button("Use tallest peak", id="btn-tallest", style=btn),
    ], style=css_box)

    def sidebar():                      # built on every page load, so it shows the controller's present values
        groups = [html.Div([html.B(g)] + [control(f[0]) for f in FIELDS if f[3] == g], style=css_box)
                  for g in dict.fromkeys(f[3] for f in FIELDS)]
        return [buttons, target_box] + groups

    def serve_layout():
        return html.Div([
            html.Div([
                html.H3("Cavity PZT dither lock", style={"margin": "4px 0"}),
                html.Div(id="status", style={"fontSize": "17px", "fontWeight": "bold"}),
            ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"}),
            html.Div([
                html.Div(sidebar(),
                         style={"width": "390px", "flexShrink": "0", "overflowY": "auto", "height": "calc(100vh - 70px)"}),
                html.Div([
                    dcc.Graph(id="scope", config={"displaylogo": False}),
                    dcc.Graph(id="history", config={"displaylogo": False}),
                    dcc.Graph(id="noise", config={"displaylogo": False}, style={"display": "none"}),
                    dcc.Graph(id="loopfig", config={"displaylogo": False}, style={"display": "none"}),
                    html.Div([
                        html.Div(id="analysis", style={"flex": "1", "fontSize": "13px", "whiteSpace": "pre-wrap"}),
                        html.Pre(id="log", style={"flex": "1", "fontSize": "12px", "height": "22vh", "overflowY": "auto",
                                                  "whiteSpace": "pre-wrap", "background": "#f6f6f6", "padding": "6px",
                                                  "margin": 0}),
                    ], style={"display": "flex", "gap": "10px"}),
                ], style={"flex": "1", "minWidth": "0", "overflowY": "auto", "height": "calc(100vh - 70px)"}),
            ], style={"display": "flex", "gap": "12px"}),
            dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
            dcc.Store(id="synced", data=-1),                  # -1: the first tick pushes all present values
            dcc.Store(id="frame"),
            dcc.Store(id="frame-seq", data=-1),
            dcc.Store(id="meas-seq", data=-1),
            dcc.Store(id="ack"),
        ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    app.layout = serve_layout

    @app.callback(Output("ack", "data", allow_duplicate=True),
                  [Input(f"s-{k}", "value") for k in KEYS], State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        trig = (ctx.triggered_id or "").replace("s-", "")
        if trig in FIELD and FIELD[trig][4] == "num" and vals[KEYS.index(trig)] is None:
            lim = FIELD[trig][5]
            ctl.log(f"'{FIELD[trig][1]}': not a number or outside "
                    f"[{lim.get('min', '-inf')}, {lim.get('max', 'inf')}]; keeping {ctl.s[trig]}")
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-acquire": "acquire", "btn-phase": "autophase", "btn-lock": "lock", "btn-unlock": "unlock",
               "btn-save": "save", "btn-load": "load", "btn-tallest": "tallest", "btn-noise": "noise",
               "btn-loop": "loop"}

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(b, "n_clicks") for b in btn_cmd],
                  prevent_initial_call=True)
    def on_button(*_):
        if ctx.triggered_id in btn_cmd:
            ctl.submit(btn_cmd[ctx.triggered_id])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("scope", "clickData"), prevent_initial_call=True)
    def on_click(cd):
        if cd and cd.get("points"):
            ctl.submit("target", float(cd["points"][0]["x"]))
        return time.time()

    def r5(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 5).tolist()

    def frame_data(snap):
        s, tr, dr, an = snap["settings"], snap["traces"], snap["drive"], snap["analysis"] or {}
        rows = [p for p in ("transmission", "error", "drive") if p in s["panels"]]
        sweep = tr is not None and tr["kind"] == "sweep"
        fr = dict(rows=rows, uirev=f"{'sweep' if sweep else 'live'}-{len(rows)}",
                  xtitle="time [ms]" + ("  (one ramp period; click a peak to pick the lock target)" if sweep else "  (live)"))
        if tr is not None:
            t_ms = tr["t"] * 1e3
            if "transmission" in rows:
                x, y = envelope(t_ms, tr["pd"])
                fr.update(t=r5(x), pd=r5(y))
            if "error" in rows:
                x, y = envelope(t_ms, tr["err"])
                fr.update(t=r5(x), err=r5(y))
        if dr is not None and "drive" in rows:
            x, y = envelope(dr["t"] * 1e3, dr["out1"] * 1e3)
            _, yr = envelope(dr["t"] * 1e3, dr["ref"] * 1e3)
            fr.update(dt=r5(x), dout=r5(y), dref=r5(yr), dref_name=dr["ref_name"])
        if sweep and an.get("peaks"):
            fr["peaks"] = [dict(t=p["t_ms"], y=p["base"] + p["height"], label=f"{p['v']*1e3:+.0f} mV",
                                tgt=i == an.get("target")) for i, p in enumerate(an["peaks"])]
            fr["v0_t"] = an.get("v0_t_ms", [])
        if not sweep and snap["lock"].get("height"):
            fr["lock_height"] = snap["lock"]["height"]
        if "setpoint history" in s["panels"]:
            h = snap["history"][-6000:]
            t, sp, T, un = [], [], [], []
            if h:
                t0 = h[0][0]
                step = max(1, len(h) // 1500)
                last = None
                for j in range(0, len(h), step):
                    x = h[j]
                    tt = x[0] - t0
                    if last is not None and tt - last > 1.5:          # gap (e.g. while acquiring): break the lines
                        t.append(None); sp.append(None); T.append(None)
                    t.append(round(tt, 3)); sp.append(round(x[4] * 1e3, 4)); T.append(round(x[1], 5))
                    last = tt
                un = [round(x[0] - t0, 3) for x in h if x[5] == "unlock"]
            fr["hist"] = dict(t=t, sp=sp, T=T, unlocks=un)
        return fr

    app.clientside_callback(SCOPE_JS, [Output("scope", "figure"), Output("history", "figure")], Input("frame", "data"))

    def noise_fig(nz):
        f = go.Figure()
        if nz:
            x = nz["f"]
            if nz["det_lw"] is not None:
                f.add_scatter(x=x, y=[v * nz["kappa_mhz"] * 1e6 for v in nz["det_lw"]], mode="lines",
                              name="residual detuning [Hz/rtHz]", line=dict(color="#1f5fa8", width=1))
            f.add_scatter(x=x, y=nz["rin"], mode="lines", name="relative intensity noise [1/rtHz]", yaxis="y2",
                          line=dict(color="#222222", width=1))
            for k in (1, 2, 3, 4):
                f.add_vline(x=k * nz["f_d"], line=dict(color="#d35400", width=0.8, dash="dot"))
        f.update_layout(title=dict(text="Noise while locked (dotted: dither harmonics)", font=dict(size=14)),
                        height=330, template="plotly_white", margin=dict(l=60, r=60, t=35, b=40),
                        xaxis=dict(type="log", title="frequency [Hz]"),
                        yaxis=dict(type="log", title="detuning [Hz/√Hz]", exponentformat="power"),
                        yaxis2=dict(type="log", title="RIN [1/√Hz]", overlaying="y", side="right",
                                    exponentformat="power"),
                        legend=dict(orientation="h", y=1.12, x=1, xanchor="right"))
        return f

    def loop_fig(lp):
        f = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
        if lp:
            x = lp["f"]
            f.add_scatter(x=x, y=lp["G_mag"], mode="lines+markers", name="|G| measured", row=1, col=1,
                          line=dict(color="#1f5fa8"))
            f.add_scatter(x=x, y=lp["M_mag"], mode="lines", name="|G| model", row=1, col=1,
                          line=dict(color="#1f5fa8", dash="dot"))
            if lp["P_mag"] is not None:
                pm = np.asarray(lp["P_mag"])
                f.add_scatter(x=x, y=(pm / pm[0]).tolist(), mode="lines", name="plant (normalized)", row=1, col=1,
                              line=dict(color="#d35400"))
            f.add_scatter(x=x, y=lp["G_ph"], mode="lines+markers", name="phase G", row=2, col=1,
                          line=dict(color="#1f5fa8"), showlegend=False)
            f.add_scatter(x=x, y=lp["M_ph"], mode="lines", name="phase model", row=2, col=1,
                          line=dict(color="#1f5fa8", dash="dot"), showlegend=False)
            if lp["P_ph"] is not None:
                f.add_scatter(x=x, y=lp["P_ph"], mode="lines", name="phase plant", row=2, col=1,
                              line=dict(color="#d35400"), showlegend=False)
            f.add_hline(y=1, line=dict(color="gray", width=1), row=1, col=1)
            f.add_hline(y=-180, line=dict(color="red", width=1, dash="dot"), row=2, col=1)
            if lp["ugf"]:
                f.add_vline(x=lp["ugf"], line=dict(color="#1e8e3e", dash="dash"))
        ttl = "Open-loop gain G" + (f": unity gain {lp['ugf']:.1f} Hz, phase margin {lp['pm']:.0f} deg"
                                     if lp and lp["ugf"] else "")
        f.update_layout(title=dict(text=ttl, font=dict(size=14)), height=420, template="plotly_white",
                        margin=dict(l=60, r=20, t=35, b=40), legend=dict(orientation="h", y=1.08, x=1, xanchor="right"))
        f.update_xaxes(type="log")
        f.update_xaxes(title_text="frequency [Hz]", row=2, col=1)
        f.update_yaxes(type="log", title_text="magnitude", row=1, col=1)
        f.update_yaxes(title_text="phase [deg]", row=2, col=1)
        return f

    @app.callback(
        [Output("frame", "data"), Output("frame-seq", "data"), Output("status", "children"),
         Output("target-text", "children"), Output("analysis", "children"), Output("log", "children"),
         Output("noise", "style"), Output("loopfig", "style"),
         Output("history", "style"), Output("meas-seq", "data"), Output("tick", "interval")]
        + [Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
        Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"), State("meas-seq", "data"))
    def on_tick(_, synced, frame_seq, meas_seq):
        snap = ctl.snapshot()
        s, mode, an, li = snap["settings"], snap["mode"], snap["analysis"] or {}, snap["lock"]
        new_frame = snap["seq"] != frame_seq or snap["version"] != synced
        frame = frame_data(snap) if new_frame else no_update
        f_d, d_mvpp = snap["dither"]
        dm = f" (measured {snap['dither_meas']:.1f})" if snap["dither_meas"] else ""
        color = {"locked": "#1e8e3e", "acquire": "#2471a3", "held": "#b9770e", "busy": "#7d3c98",
                 "locking": "#7d3c98", "measuring": "#7d3c98"}.get(mode, "#555")
        if mode in ("locked", "measuring") and li.get("setpoint") is not None:
            extra = (f"   setpoint {li['setpoint']*1e3:+.3f} mV   transmission {li['frac']*100:.0f}% of peak   "
                     f"bandwidth {li.get('ugf_actual', 0):.1f} Hz   dither {d_mvpp:g} mVpp{dm} @ {f_d:g} Hz")
        elif mode == "acquire" and snap["ramp"]:
            rp = snap["ramp"]
            extra = (f"   {rp['shape']} ramp {(rp['hi']-rp['lo'])*1e3:.0f} mVpp @ {rp['hz']:g} Hz   "
                     f"dither {d_mvpp:g} mVpp{dm} @ {f_d:g} Hz")
        elif mode == "held":
            extra = f"   setpoint held at {snap['history'][-1][4]*1e3:+.3f} mV" if snap["history"] else ""
        else:
            extra = ""
        extra += f"   {snap['fps']:.1f} traces/s" if snap["fps"] else ""
        status = html.Span([html.Span(mode.upper(), style={"color": color}), html.Span(extra, style={"fontSize": "13px"})])
        if an.get("target") is not None and mode in ("acquire", "busy"):
            p = an["peaks"][an["target"]]
            ttxt = (f"{'Chosen' if s.get('target_mv') is not None else 'Tallest'} peak at {p['v']*1e3:+.1f} mV, "
                    f"height {p['height']:.3f} V" + (f", lock point {an['v0']*1e3:+.2f} mV" if an.get("v0") is not None else ""))
        elif li.get("v0") is not None:
            ttxt = f"Locked peak: lock point {li['v0']*1e3:+.2f} mV, height {li['height']:.3f} V"
        else:
            ttxt = "no target yet"
        lines = []
        if mode in ("acquire", "busy", "locking") and an.get("peaks"):
            for i, p in enumerate(an["peaks"]):
                lines.append(f"{p['v']*1e3:+8.1f} mV   height {p['height']:.3f} V   FWHM {p['fwhm']*1e3:.2f} mV"
                             + ("   <- target" if i == an.get("target") else ""))
            if an.get("v0") is not None:
                lines.append(f"lock point {an['v0']*1e3:+.3f} mV, slope {an['slope']:+.1f} /V"
                             + (f", nearest other peak {an['mode_spacing']*1e3:.1f} mV away" if an.get("mode_spacing") else ""))
            lines += an.get("notes", [])
        elif li.get("v0") is not None:
            lines.append(f"lock point {li['v0']*1e3:+.3f} mV, slope {li['slope']:+.1f} /V, clamp "
                         f"{li['clamp'][0]*1e3:+.1f}..{li['clamp'][1]*1e3:+.1f} mV, relocks {li.get('relocks', 0)}")
            if li.get("err_rms") is not None:
                lines.append(f"error rms {li['err_rms']*1e3:.2f} mV  ->  about {abs(li['err_rms']/li['slope'])*1e6:.1f} uV "
                             f"of drive ({abs(li['err_rms']/li['slope'])/li['fwhm']*100:.2f}% of the swept FWHM)")
        lp_list = [s["lpf_hz"]] * int(s["lpf_order"])
        fu, pm = predicted_margin(s, lp_list)
        if fu:
            lines.append(f"model: unity gain {fu:.0f} Hz, phase margin {pm:.0f} deg (integrator + error low-pass "
                         f"{' + '.join(f'{x:.0f} Hz' for x in lp_list)} + driver {s['driver_bw_hz']:g} Hz + 0.2 us)")
            if pm < 45:
                lines.append("WARN phase margin under 45 deg: lower the loop bandwidth or raise the low-pass/dither frequency")
        nz = snap["noise"]
        if nz:
            lines.append("noise (linewidths rms | intensity rms) by band: " + ", ".join(
                f"{r['band']}: {r['det']*100 if r['det'] is not None else float('nan'):.2f}% | {r['rin']*100:.2f}%"
                for r in nz["rows"]))
        meas_new = snap["meas_seq"] != meas_seq
        show_noise = "noise spectrum" in s["panels"]
        show_loop = "loop (Bode)" in s["panels"]
        hist_style = {} if "setpoint history" in s["panels"] else {"display": "none"}
        if snap["version"] != synced:
            vals = [to_ui(k, s[k]) for k in KEYS] + [snap["version"]]
            interval = int(1000 / max(float(s["ui_hz"]), 1))
        else:
            vals = [no_update] * len(KEYS) + [no_update]
            interval = no_update
        return [frame, snap["seq"], status, ttxt, "\n".join(lines), "\n".join(reversed(snap["logs"][-80:])),
                {} if show_noise else {"display": "none"}, {} if show_loop else {"display": "none"},
                hist_style, snap["meas_seq"] if meas_new else no_update, interval] + vals

    # The measurement plots get their own callbacks: sending both figures inside the 10 Hz tick response made
    # the browser drop the whole update (seen with Dash 4.5), freezing the page.
    @app.callback(Output("noise", "figure"), Input("meas-seq", "data"))
    def on_noise(_):
        return noise_fig(ctl.noise)

    @app.callback(Output("loopfig", "figure"), Input("meas-seq", "data"))
    def on_loop(_):
        return loop_fig(ctl.loop)

    return app


def main():
    ap = argparse.ArgumentParser(description="Cavity PZT dither lock (Red Pitaya + PyRPL) in the browser")
    ap.add_argument("--host", default="rp-f0fa92.local", help="Red Pitaya hostname or IP")
    ap.add_argument("--config", default="cavity_lock_app", help="PyRPL config name")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    ctl = Controller(args.host, args.config, settings)
    app = build_app(ctl)
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=args.port, debug=False), daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"app running at {url}", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    stop = threading.Event()
    try:
        ctl.run(stop)
    except KeyboardInterrupt:
        print("\nquitting; the Red Pitaya keeps its present state (a running lock stays on).")


if __name__ == "__main__":
    main()
