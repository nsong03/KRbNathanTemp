#!/usr/bin/env python3
"""
cavity_lock_app.py -- browser app for dither-locking a cavity PZT with a Red Pitaya (PyRPL).

The UI is a Moku-style block diagram of the real FPGA signal chain (schematic.py = the model, ui.py = Dash):
  IN1 -> band-pass (iq0 AC high-pass) -> mixer (LO = dither through the phase delay) -> low-pass -> integer gain
      -> error (iq0) -> setpoint -> PID (pid0, P + I) -> limiter -> sum (+ ramp asg0, + dither) -> OUT1
Every box shows what the registers really hold; every knob writes the register drawn on its box.
Switches on the diagram:
  Scan    asg0 ramp into OUT1 (acquisition). Off: OUT1 held at the ramp centre.
  Dither  iq0's oscillator into OUT1.
  Lock    stops the scan, sweeps slowly across the chosen peak with the lock dither, re-fits the phase, parks
          OUT1 at the error zero crossing and closes pid0 with a gain ramp. Off: unlock and hold.
  Output  OUT1 enable (off: ramp, dither and PID disconnected, OUT1 = 0 V).  Auto-relock: the watchdog.
Probe points on the wires feed the embedded scope (2 channels). Click a transmission peak or an error zero
crossing (Lock Assist crosshair) to choose the lock target.
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

import sys

import numpy as np
from scipy.signal import find_peaks, peak_widths, welch

import schematic as SCH

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "cavity_lock_settings.json")
OUT_LIMIT = SCH.OUT_LIMIT             # never command OUT1 beyond +/- this [V]
RP_LATENCY = 211e-9                   # analog-in to analog-out delay of the lock-in path (FPGA simulation)
PANELS = ["transmission", "error", "drive", "setpoint history", "noise spectrum", "loop (Bode)", "Bode (swept)"]

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
    ("setpoint_mv", "Setpoint (error offset) [mV]", 0.0, "Demodulation", "num", {}),
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
    ("bode_mode", "Bode: what to measure", "closed loop", "Diagnostics", "select",
     {"options": ["closed loop", "circuit only"]}),
    ("bode_start_hz", "Bode: start frequency [Hz]", 3.0, "Diagnostics", "num", {"min": 0.5}),
    ("bode_stop_hz", "Bode: stop frequency [Hz] (closed loop: max 0.45 x dither)", 500.0, "Diagnostics", "num",
     {"min": 1}),
    ("bode_points", "Bode: points", 30, "Diagnostics", "num", {"min": 3, "max": 200}),
    ("bode_cycles", "Bode: sine cycles per point", 6, "Diagnostics", "num", {"min": 2, "max": 50}),
    ("bode_amp_mv", "Bode circuit only: max test amplitude [mV]", 400.0, "Diagnostics", "num",
     {"min": 1, "max": 900}),
    ("panels", "Show", ["transmission", "error", "drive", "setpoint history"], "Display", "multi", {"options": PANELS}),
    ("ui_hz", "Browser refresh [Hz]", 10.0, "Display", "num", {"min": 1, "max": 30}),
    ("drive_every", "Refresh the drive plot every N traces", 3, "Display", "num", {"min": 1, "max": 100}),
    ("in1_scale", "IN1 scale [V per raw V] (1 = LV, 20 = HV)", 1.0, "Display", "num", {"min": 0.01}),
    ("out2_signal", "OUT2 monitor", "error", "Display", "select",
     {"options": ["error", "setpoint", "ramp", "photodiode", "off"]}),
    ("out2_gain", "OUT2 gain", 1.0, "Display", "num", {}),
    ("probe_a", "Probe A", "out1", "Display", "select", {"options": SCH.PROBE_SIGNALS + ["off"]}),
    ("probe_b", "Probe B", "asg0", "Display", "select", {"options": SCH.PROBE_SIGNALS + ["off"]}),
]
KEYS = [f[0] for f in FIELDS]
DEFAULTS = {f[0]: f[2] for f in FIELDS}
DEFAULTS["target_mv"] = None           # lock target (drive voltage of the chosen peak); None = tallest
OUT2_SOURCE = SCH.OUT2_SOURCE
SNAP_KEYS = ("lpf_hz", "lpf_order", "ac_hz", "err_gain")   # the FPGA rounds these: show requested -> used


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


def strong_crossings(vb, eb, frac=0.2):
    """Error zero crossings (vb ascending) where the error swings past +/- frac x its maximum on both sides
    (a hysteresis comparator, so noise around zero far from a resonance does not count).
    Returns [(v, slope sign)]."""
    if len(eb) < 4:
        return []
    th = frac * float(np.max(np.abs(eb)))
    if th <= 0:
        return []
    out, state, last = [], 0, 0
    for i in np.flatnonzero(np.abs(eb) > th):
        st = 1 if eb[i] > 0 else -1
        if state and st != state:
            sc = np.flatnonzero(np.diff(np.sign(eb[last:i + 1])) != 0)
            if len(sc):
                k = last + int(sc[len(sc) // 2])
                den = eb[k + 1] - eb[k]
                vz = vb[k] - eb[k] * (vb[k + 1] - vb[k]) / den if den else vb[k]
                out.append((float(vz), -1 if state > 0 else 1))
        state, last = st, i
    return out


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
    zc = []                                          # Lock Assist crosshairs: every strong error zero crossing
    for name, sel in (("up", up), ("down", ~up)):
        if name not in halves:
            continue
        ii = np.flatnonzero(sel)
        for vz, sgn in strong_crossings(halves[name]["vb"], halves[name]["eb"]):
            k = ii[int(np.argmin(np.abs(v[ii] - vz)))]
            near = [p for p in peaks if abs(p["v_by"].get(name, p["v"]) - vz) < max(p["fwhm"], 1e-4)]
            zc.append(dict(v=vz, t_ms=float(t[k] * 1e3), slope=sgn, on_peak=bool(near),
                           peak_v=near[0]["v"] if near else None))
    res["zc"] = zc
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
        self.bode = None                                 # swept-sine Bode measurement (filled point by point)
        self.bode_seq = 0
        self.meas_progress = ""
        self._stop_meas = threading.Event()              # set from the UI thread: stop the running sweep
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
        self.dither_on = True                            # the Dither switch (iq0.output_direct)
        self.hw = {}                                     # register readback shown on the diagram
        self.req = {}                                    # requested values of keys the FPGA rounds
        self.ival = None                                 # last pid0.ival read
        self._probe_last = "b"                           # probe channel assigned last
        self.log(f"connecting to {host} ...")
        from pyrpl import Pyrpl
        self.p = Pyrpl(config=config, hostname=host, gui=False)
        self.r = self.p.rp
        for name in ("iq0", "pid0", "pid1", "asg0", "scope", "asg1", "iq1", "pid2"):   # reserve: PyRPL tools
                                                                                   # (network analyzer) skip them
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
        self._start_acquire()
        self._readback()

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
                        noise=self.noise, loop=self.loop, meas_seq=self.meas_seq, fps=self.fps,
                        bode=self.bode, bode_seq=self.bode_seq, meas_progress=self.meas_progress,
                        hw=dict(self.hw), req=dict(self.req), ival=self.ival, amp_cal=self.amp_cal,
                        dither_on=self.dither_on)

    def switches(self):
        """Switch positions, derived from the register readback and the mode (never stored)."""
        return SCH.switch_states(self.mode, self.hw, self.s)

    def _readback(self):
        hw = SCH.read_hw(self.r)
        with self._pub_lock:
            self.hw = hw

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
        """Write every static block of the schematic (iq0: dither, phase, filters, gain; pid0 setpoint; pid1)."""
        SCH.write_static(self)
        for p in (self.r.pid0, self.r.pid1):
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
            for k in snapped:
                self.req[k] = s[k]                       # what was asked for, shown as "asked -> used"
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
        self._readback()

    def _ramp_off(self):
        a = self.r.asg0
        a.output_direct, a.amplitude, a.offset = "off", 0, 0

    def _start_acquire(self, quiet=False):
        s = self.s
        self.r.pid0.i = 0
        self.ctx = "acq"
        self.mode = "acquire"                            # before configure(): the writers look at the mode
        self.configure()
        lo, hi = self._ramp_limits(s["ramp_center_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3)
        self._ramp_on(lo, hi, s["ramp_hz"], s["ramp_shape"])
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
            drive = dict(t=td, out1=out1, ref=ramp, ref_name="ramp (asg0)", ref_sig="asg0")
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

    def _cal_live(self):
        """A dither calibration capture is due (and meaningful: the dither is on OUT1)."""
        return self._cal_checks > 0 and self.dither_on and self.mode != "off"

    def _check_drive(self, drive):
        """Measure the dither actually on OUT1 (OUT1 minus ramp or setpoint) and calibrate its amplitude."""
        if not self.dither_on or self.mode == "off":
            return                                       # no dither on OUT1: nothing to measure
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
                    try:
                        self._readback()                 # the diagram shows what the registers now hold
                    except Exception as e:
                        self.log(f"readback error: {e!r}")
            except queue.Empty:
                pass
            try:
                if self.mode == "acquire":
                    self._acquire_tick()
                elif self.mode in ("locked", "held", "off"):
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
            for k in changed & set(SNAP_KEYS):
                self.req[k] = arg[k]
            f_before = self._dither()[0]
            s.update(arg)
            written = SCH.apply_changed(self, changed)      # the writers of the blocks that own these keys
            if set(written) & {"lowpass", "bandpass", "amp"}:
                self._snap_to_hardware()
            if changed & {"probe_a", "probe_b"}:
                self._show_panel("drive")
            if self.ctx == "lock" and self.mode in ("locked", "held") and self._dither()[0] != f_before:
                self.log("note: a new dither frequency can shift the demodulation phase; press LOCK! to re-fit")
            if changed & {"ugf_hz", "p_gain", "lpf_hz", "lpf_order", "driver_bw_hz"}:
                fu, pm = predicted_margin(s, self._lp_list())
                if pm is not None and pm < 45:
                    self.log(f"WARN predicted phase margin {pm:.0f} deg at {fu:.0f} Hz: the loop may oscillate")
            if changed - {"panels", "ui_hz", "drive_every"}:
                self.log("applied: " + ", ".join(f"{k}={s[k]}" for k in sorted(changed)))
        elif cmd == "acquire":
            self._switch("scan", True)
        elif cmd == "switch":
            self._switch(*arg)
        elif cmd == "probe":
            self._toggle_probe(arg)
        elif cmd == "target_v":
            self._target_at(float(arg))
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
            self._switch("lock", False)
        elif cmd == "noise":
            self._measure_noise()
        elif cmd == "loop":
            self._measure_loop()
        elif cmd == "bode":
            self._measure_bode()
        elif cmd == "save":
            with open(SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=2)
            self.log(f"settings saved to {SETTINGS_FILE}")
        elif cmd == "load":
            with open(SETTINGS_FILE) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            self.req.clear()
            self.configure()
            self.version += 1
            if self.mode == "acquire":
                self._start_acquire(quiet=True)
            self.log("settings loaded")

    # ---- acquisition ----
    def _probe_due(self):
        return self._tick % max(int(self.s["drive_every"]), 1) == 0

    def _probe_frame(self, main, dur, trigger, cal):
        """Traces for the probe row (scope channels A and B). The main capture (in1 + iq0) is reused when both
        probes are among those; a calibration capture (out1 + ramp/setpoint) when it matches; otherwise a
        second capture of (A, B) every drive_every traces. Returns None when nothing new was captured."""
        s = self.s
        if "drive" not in s["panels"]:
            return None
        a, b = s["probe_a"], s["probe_b"]
        pair = (a, b)
        real = {x for x in pair if x != "off"}
        if real <= {"in1", "iq0"}:
            return dict(t=main["t"], a=main.get(a), b=main.get(b), a_name=a, b_name=b, pair=pair)
        if cal is not None and real <= {"out1", cal["ref_sig"]}:
            m = {"out1": cal["out1"], cal["ref_sig"]: cal["ref"]}
            return dict(t=cal["t"], a=m.get(a), b=m.get(b), a_name=a, b_name=b, pair=pair)
        if not self._probe_due() and self.drive is not None and self.drive.get("pair") == pair:
            return None
        t, ya, yb = self.capture(a if a != "off" else "in1", b if b != "off" else "in1", dur, trigger)

        def sc(n, y):
            return None if n == "off" else (y * s["in1_scale"] if n == "in1" else y)
        return dict(t=t, a=sc(a, ya), b=sc(b, yb), a_name=a, b_name=b, pair=pair)

    def _acquire_tick(self):
        s = self.s
        self._tick += 1
        real = {x for x in (s["probe_a"], s["probe_b"]) if x != "off"}
        want = self._cal_live() or ("drive" in s["panels"] and self._probe_due() and bool(real)
                                    and real <= {"out1", "asg0"})
        tr, drive = self.capture_sweep(s["averages"], want_drive=want)
        an = analyze_sweep(tr, self._ctx(s["target_mv"]))
        if s["target_mv"] is not None and an.get("target_v") is not None:
            s["target_mv"] = an["target_v"] * 1e3        # follow the chosen peak as it drifts
        if drive is not None:
            self._check_drive(drive)
        probe = self._probe_frame(dict(t=tr["t"], in1=tr["pd"], iq0=tr["err"]), 1.02 / self.ramp["hz"], "asg0",
                                  drive)
        self._publish(traces=dict(kind="sweep", **tr), analysis=an,
                      **({"drive": probe} if probe is not None else {}))

    def _target_at(self, v):
        """Lock Assist: the peak nearest drive voltage v (a clicked zero crossing) becomes the lock target."""
        tr, an = self.traces, self.analysis or {}
        if self.mode != "acquire" or tr is None or tr.get("kind") != "sweep" or not an.get("peaks"):
            self.log("pick the lock target while scanning (Scan on)")
            return
        p = min(an["peaks"], key=lambda q: abs(q["v"] - v))
        self.s["target_mv"] = p["v"] * 1e3
        self.log(f"lock target (crosshair at {v*1e3:+.1f} mV): peak at {p['v']*1e3:+.1f} mV")
        self._publish(analysis=analyze_sweep({k2: tr[k2] for k2 in ("t", "v", "pd", "err", "up")},
                                             self._ctx(self.s["target_mv"])))

    # ---- switches and probes ----
    def _unlock(self, why):
        self.r.pid0.i = 0
        if self.mode in ("locked", "locking", "busy", "measuring"):
            self.mode = "held"
        self.log(f"{why}: unlocked; setpoint held at {float(self.r.pid0.ival)*1e3:+.2f} mV")

    def _output_off(self):
        r = self.r
        r.pid0.i = 0
        self._ramp_off()
        r.iq0.output_direct = "off"
        r.pid0.output_direct = "off"
        self.mode = "off"
        self.ctx = "acq"
        self.log("Output OFF: OUT1 = 0 V (ramp, dither and PID disconnected from OUT1)")

    def _switch(self, name, on):
        """Diagram switches with interlocks. Their displayed state is derived from the registers (switches())."""
        s, on = self.s, bool(on)
        if self.mode in SCH.BUSY:
            self.log(f"{name} switch ignored while {self.mode}")
            return
        if name == "relock":
            s["auto_relock"] = on
            self.version += 1
            self.log(f"auto-relock {'on' if on else 'off'}")
            return
        if name == "output":
            if on and self.mode == "off":
                self.log("Output on: scanning")
                self._start_acquire()
            elif not on and self.mode != "off":
                self._output_off()
            return
        if on and self.mode == "off":
            self.log(f"{name}: Output is off; switch Output on first")
            return
        if name == "scan":
            if on:
                if self.mode == "locked":
                    self._unlock("Scan on")
                if self.mode != "acquire":
                    self._start_acquire()
            elif self.mode == "acquire":
                c = (self.ramp["lo"] + self.ramp["hi"]) / 2
                self._park(c)
                self.log(f"Scan off: OUT1 held at the ramp center {c*1e3:+.1f} mV")
        elif name == "dither":
            if not on and self.mode == "locked":
                self._unlock("Dither off (the error signal needs it)")
            self.dither_on = on
            SCH.WRITERS["dither"](self)
            self.log(f"dither {'on' if on else 'off'} (iq0.output_direct = {self.r.iq0.output_direct})")
        elif name == "lock":
            if on:
                if self.mode == "locked":
                    return
                if not self.dither_on:
                    self.dither_on = True
                    self.log("Lock: dither switched on first (the error signal needs it)")
                self._lock()
            elif self.mode == "locked":
                self._unlock("Lock off")

    def _toggle_probe(self, sig):
        """Click on a probe point: place it on A (if free), else B, else replace the older one; click a placed
        probe to remove it. The scope has 2 channels."""
        if sig not in SCH.PROBE_SIGNALS:
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
        self._show_panel("drive")
        self.log(f"probes: A = {a}, B = {b}")

    def _pick_target(self, t_ms):
        tr, an = self.traces, self.analysis or {}
        if self.mode != "acquire" or tr is None or tr.get("kind") != "sweep":
            self.log("pick the lock target while scanning (Scan on)")
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
            self.log("Auto-optimize phase works while scanning (Scan on)")
            return
        if not self.dither_on:
            self.log("Auto-optimize phase needs the dither: switch Dither on")
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
        elif self.mode in ("held", "locked") and self.lock_info.get("fwhm"):
            center = float(self.r.pid0.ival)
            self.log(f"LOCK 1/3: re-locking around the setpoint {center*1e3:+.2f} mV")
            ref = dict(fwhm=self.lock_info["fwhm"], spacing=self.lock_info.get("spacing"), relocks=0)
        else:
            self.log("LOCK: start from a scan (Scan on, pick a peak)")
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
        self._readback()

    def _engage(self, v0, S, ref):
        s = self.s
        lo, hi = self._clamp(v0, ref.get("spacing"))
        self._ramp_off()
        p0 = self.r.pid0
        p0.i, p0.p = 0, 0
        SCH.WRITERS["setpoint"](self)
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
        self._readback()
        self._bad_since = None
        self._good_ival.clear()
        return True

    def _live_tick(self):
        """Time-domain traces while locked or held; watchdog when locked."""
        s = self.s
        dur = s["lock_window_ms"] * 1e-3
        t, pd, err = self.capture("in1", "iq0", dur)
        pd = pd * s["in1_scale"]
        self._tick += 1
        cal = None
        if self._cal_live():
            td, out1, sp = self.capture("out1", "pid0", dur)
            cal = dict(t=td, out1=out1, ref=sp, ref_name="setpoint (pid0)", ref_sig="pid0")
            self._check_drive(cal)
        drive = self._probe_frame(dict(t=t, in1=pd, iq0=err), dur, "immediately", cal)
        ival = float(self.r.pid0.ival)
        self.ival = ival
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
        self._readback()

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

    # ---- swept-sine Bode measurement (internal generator asg1 + scope) ----
    def stop_measurement(self):
        """Called from the UI thread: the running sweep stops after the present point."""
        self._stop_meas.set()

    def _bode_publish(self, B):
        B = dict(B, curves=[dict(cv, f=list(cv["f"]), c=list(cv["c"]),
                                 **({"low": list(cv["low"])} if "low" in cv else {})) for cv in B["curves"]])
        with self._pub_lock:
            self.bode = B
            self.bode_seq += 1

    def _measure_bode(self):
        """Swept-sine Bode plot, filled in point by point.
        closed loop (locked): asg1 adds a small sine at the OUT1 sum; the scope records it with the PID output
            (pid0) and with the error (iq0). c/x gives the open-loop gain G = -T/(1+T), the sensitivity
            S = 1/(1+G) = 1 + c/x and the closed-loop response G/(1+G); the plant is (e/x)/S.
        circuit only (any mode, OUT1 untouched): spare copies of the blocks (iq1 = lock-in chain with the same
            low-pass and gain, its oscillator at 0 Hz so the chain is seen at baseband; pid2 = the PID with the
            loop gains per unit error slope) are driven by asg1, which is not routed to any output."""
        s = self.s
        closed = s["bode_mode"] == "closed loop"
        if self.mode in SCH.BUSY:
            self.log(f"Bode: busy ({self.mode})")
            return
        if closed and self.mode != "locked":
            self.log("Bode (closed loop) works while locked; use 'circuit only' to measure the electronics alone")
            return
        f_d = self._dither()[0]
        # closed loop: the lock-in cannot pass more than ~dither/2. Circuit only: the copies run at baseband, so any
        # frequency can be measured (up to the 62.5 MHz Nyquist limit); above dither/2 the plot is shaded.
        f_stop = min(s["bode_stop_hz"], 0.45 * f_d if closed else 30e6)
        f_start = min(s["bode_start_hz"], f_stop / 2)
        if f_stop < s["bode_stop_hz"]:
            self.log(f"Bode: stop frequency limited to {f_stop:.0f} Hz (closed loop: 0.45 x dither, the lock-in "
                     f"cannot pass more)")
        fs = np.geomspace(f_start, f_stop, int(s["bode_points"]))
        prev = self.mode
        self.mode = "measuring"
        self._stop_meas.clear()
        self._show_panel("Bode (swept)")
        try:
            if closed:
                self._bode_closed(fs)
            else:
                self._bode_circuit(fs, prev)
        finally:
            a1 = self.r.asg1
            a1.output_direct, a1.amplitude = "off", 0
            for m, attrs in (("iq1", {"output_direct": "off", "amplitude": 0}),
                             ("pid2", {"output_direct": "off", "i": 0, "p": 0, "ival": 0})):
                for k, v in attrs.items():
                    try:
                        setattr(getattr(self.r, m), k, v)
                    except Exception:
                        pass
            self.meas_progress = ""
            if self.mode == "measuring":
                self.mode = prev
            if self.bode:
                self._bode_publish(dict(self.bode, done=True))

    def _bode_capture(self, sig, f, cycles):
        """Two-channel capture (asg1, sig) long enough for `cycles` periods; complex amplitudes at f."""
        dur = float(np.clip(cycles / f, 1.3e-4, 2.1))    # 2.1 s: the scope then picks 2.15 s, not 4.3 s
        t, x, y = self.capture("asg1", sig, dur)
        return fit_sine(t, x, f), fit_sine(t, y, f)

    def _bode_point_live(self, f, amp, out):
        a1 = self.r.asg1
        a1.setup(waveform="sin", frequency=float(f), amplitude=float(amp), offset=0.0,
                 trigger_source="immediately", output_direct=out)
        with self._pub_lock:
            self.hw.update({"asg1.frequency": float(f), "asg1.amplitude": float(amp), "asg1.output_direct": out,
                            "asg1.waveform": "sin"})

    def _bode_closed(self, fs):
        s, li, r = self.s, self.lock_info, self.r
        kap, _ = self._kappa_drive()
        amp = float(np.clip(s["na_amp_frac"] * (kap or 0.005), 1e-4, 0.05))
        ugf = li.get("ugf_actual", s["ugf_hz"])
        Gm = loop_model(fs, ugf, s["p_gain"], self._lp_list(), s["driver_bw_hz"], RP_LATENCY)
        B = bode_init("closed loop", fs, [
            ("G open loop", "#1f5fa8", Gm), ("S = 1/(1+G): disturbance suppression", "#c0392b", 1 / (1 + Gm)),
            ("T = G/(1+G): closed loop", "#1e8e3e", Gm / (1 + Gm))],
            title=f"Closed-loop Bode (sine {amp*1e3:.2f} mV at OUT1, lock {ugf:.1f} Hz)",
            note="measured: solid with markers; model (integrator + low-pass + driver pole + delay): dotted")
        B["curves"].append(dict(name="plant P (normalized)", color="#d35400", f=[], c=[], model=False))
        self._bode_publish(B)
        self.log(f"Bode closed loop: {len(fs)} points {fs[0]:.3g}-{fs[-1]:.3g} Hz, sine {amp*1e3:.2f} mV on OUT1")
        lo, hi = li["clamp"]
        settle_loop = 4 / (2 * math.pi * max(ugf, 0.1))
        p0 = None
        for k, f in enumerate(fs):
            if self._stop_meas.is_set():
                self.log(f"Bode stopped after {k} points")
                break
            self.meas_progress = f"Bode {k+1}/{len(fs)} at {f:.3g} Hz"
            self._bode_point_live(f, amp, "out1")
            time.sleep(min(max(3 / f, settle_loop, self._filter_settle()), 1.5))
            X, C = self._bode_capture("pid0", f, s["bode_cycles"])
            X2, E = self._bode_capture("iq0", f, s["bode_cycles"])
            ival = float(r.pid0.ival)
            if ival <= lo + 0.03 * (hi - lo) or ival >= hi - 0.03 * (hi - lo):
                r.asg1.output_direct = "off"
                r.pid0.i = 0
                self.mode = "held"
                self.log(f"Bode: the lock ran to the clamp at {f:.3g} Hz; stopped and holding (lower the excitation)")
                break
            Tc, Ex = C / X, E / X2                       # c/x and e/x
            G, S = -Tc / (1 + Tc), 1 + Tc
            P = Ex / S
            p0 = p0 or dc_norm(P)                        # normalize to the first point: magnitude and sign only
            for cv, val in zip(B["curves"], (G, S, -Tc, P / p0)):
                cv["f"].append(float(f))
                cv["c"].append(val)
            fu, pm = margins(np.array(B["curves"][0]["f"]), np.array(B["curves"][0]["c"])) \
                if len(B["curves"][0]["f"]) > 1 else (None, None)
            B.update(ugf=fu, pm=pm, n=k + 1)
            self._bode_publish(dict(B))
        fu, pm = B.get("ugf"), B.get("pm")
        self.log("Bode closed loop: " + (f"unity gain {fu:.1f} Hz, phase margin {pm:.0f} deg" if fu else
                                         "no unity-gain crossing in the measured range"))

    def _bode_circuit(self, fs, prev):
        """Each block measured on its own spare copy, driven straight from asg1 (the response of the chain is the
        product: the blocks are linear). Per point, the test amplitude and the copy's gain (integer gain x K for the
        lock-in, i and p x K for the PID) are chosen so the copy's output is ~0.3 V: the response then stays far
        above the scope's 14-bit step even where the block attenuates by 100 dB. The gain actually used is read back
        and divided out. Points whose response is still under 5 steps are flagged (hollow markers)."""
        s, r = self.s, self.r
        S = self.lock_info.get("slope")
        Sn = abs(S) if S else 1.0
        iq, p2 = r.iq1, r.pid2
        qf0 = int(round(s["err_gain"]))
        # copy of the lock-in chain at baseband (oscillator at 0 Hz: the mixer multiplies by cos(0) = 1)
        iq.output_direct = "off"
        iq.amplitude = 0
        iq.frequency = 0
        iq.phase = 0
        iq.input = "asg1"
        iq.acbandwidth = 0
        iq.gain = 0
        iq.bandwidth = [s["lpf_hz"]] * int(s["lpf_order"])
        iq.quadrature_factor = qf0
        iq.output_signal = "quadrature"
        # copy of the PID with the loop gains per unit slope (i = UGF, p = P gain; the lock divides them by |S|)
        p2.output_direct = "off"
        p2.input = "asg1"
        p2.setpoint = 0
        p2.min_voltage, p2.max_voltage = -1.0, 1.0
        try:
            p2.inputfilter = [0, 0, 0, 0]
        except Exception:
            pass
        p2.p, p2.i = s["p_gain"], s["ugf_hz"]
        p_reg, i_reg = float(p2.p), float(p2.i)
        bw = [float(b) for b in np.atleast_1d(iq.bandwidth) if b > 0]
        tau_L = (3 + 2 * len(bw)) * 8e-9                 # FPGA pipeline (Verilog count, docs): IQ 3 + 2 per stage
        tau_C = 4 * 8e-9                                 # PID: 4 clocks
        L_m = np.exp(-2j * np.pi * fs * tau_L)
        for b in bw:
            L_m = L_m / (1 + 1j * fs / b)
        C_m = (i_reg / (1j * fs) + p_reg) * np.exp(-2j * np.pi * fs * tau_C)
        f_d = self._dither()[0]
        hp = float(r.iq0.acbandwidth)
        hp_fd = abs(1 / (1 + hp / (1j * f_d))) if hp > 0 else 1.0
        B = bode_init("circuit only", fs, [
            ("lock-in chain (normalized)", "#1f5fa8", L_m),
            (f"PID x slope (i = {i_reg:.3g} Hz, p = {p_reg:.3g})", "#7d3c98", C_m),
            ("electronics open loop", "#1e8e3e", L_m * C_m)],
            title="Circuit-only Bode (spare copies iq1, pid2 driven by asg1; OUT1 untouched)",
            note=(f"lock-in at baseband: low-pass {' + '.join(f'{b:.1f}' for b in bw)} Hz, gain x{qf0}; input "
                  f"high-pass at the dither: x{hp_fd:.3f} (not in the copy). PID with i = UGF, p = P gain: the lock "
                  f"uses i/|S|" + (f" (|S| = {Sn:.1f}/V)" if S else " (slope not measured yet)") +
                  ". Driver and cavity not included. Each block auto-ranged; hollow = under 5 scope steps. "
                  "Shaded: above dither/2, where the real lock-in folds the sidebands."))
        B["f_dither"] = f_d
        for cv in B["curves"]:
            cv["low"] = []
        self._bode_publish(B)
        self.log(f"Bode circuit only: {len(fs)} points {fs[0]:.3g}-{fs[-1]:.4g} Hz on iq1 and pid2 (OUT1 untouched)")
        A_max = s["bode_amp_mv"] * 1e-3
        TARGET, KMAX, FLOOR = 0.3, 1024, 5 * SCOPE_LSB
        L0 = None
        for k, f in enumerate(fs):
            if self._stop_meas.is_set():
                self.log(f"Bode stopped after {k} points")
                break
            self.meas_progress = f"Bode {k+1}/{len(fs)} at {f:.4g} Hz"
            settle = min(max(3 / f, self._filter_settle()), 1.5)
            # lock-in copy: amplitude first (output <= 0.3 V where the chain passes), then gain x K where it attenuates
            gL = abs(L_m[k]) * qf0
            A = float(np.clip(TARGET / max(gL, 1e-12), 1e-3, A_max))
            K = int(np.clip(round(TARGET / max(A * gL, 1e-12)), 1, KMAX))
            iq.quadrature_factor = qf0 * K
            K_L = float(iq.quadrature_factor) / qf0       # what the FPGA really uses
            self._bode_point_live(f, A, "off")
            time.sleep(settle)
            X, Y = self._bode_capture("iq1", f, s["bode_cycles"])
            L, low_L = Y / X / K_L, abs(Y) < FLOOR
            # PID copy, the same way (i and p scaled together)
            gC = abs(C_m[k])
            A = float(np.clip(TARGET / max(gC, 1e-12), 1e-3, A_max))
            K = float(np.clip(TARGET / max(A * gC, 1e-12), 1, KMAX))
            p2.p, p2.i = p_reg * K, i_reg * K
            K_C = float(p2.i) / i_reg if i_reg else (float(p2.p) / p_reg if p_reg else 1.0)
            self._bode_point_live(f, A, "off")
            time.sleep(min(3 / f, 0.5))
            p2.ival = 0
            X2, Z = self._bode_capture("pid2", f, s["bode_cycles"])
            C, low_C = Z / X2 / K_C, abs(Z) < FLOOR
            L0 = L0 or dc_norm(L)                        # magnitude and sign only: keep the real phase lag
            for cv, val, low in zip(B["curves"], (L / L0, C, C * L / L0), (low_L, low_C, low_L or low_C)):
                cv["f"].append(float(f))
                cv["c"].append(val)
                cv["low"].append(bool(low))
            ok = ~np.array(B["curves"][2]["low"])
            f_ok = np.array(B["curves"][2]["f"])[ok]
            fu, pm = margins(f_ok, np.array(B["curves"][2]["c"])[ok]) if len(f_ok) > 1 else (None, None)
            B.update(ugf=fu, pm=pm, n=k + 1, dc_gain=abs(L0))
            self._bode_publish(dict(B))
        iq.quadrature_factor = qf0
        n_low = sum(B["curves"][2]["low"])
        self.log(f"Bode circuit only: lock-in DC gain {abs(L0 or 0):.3f} V/V" +
                 (f"; electronics alone: unity gain {B['ugf']:.1f} Hz, phase margin {B['pm']:.0f} deg"
                  if B.get("ugf") else "") +
                 (f"; {n_low} points under the scope resolution even auto-ranged (hollow)" if n_low else ""))

SCOPE_LSB = 2.0 / 2 ** 14                  # one step of the 14-bit scope signal on +/-1 V


def dc_norm(z):
    """A real normalization constant from a low-frequency point: |z| with the sign of Re z. Dividing by the complex
    z would also remove the real phase lag at that frequency."""
    return abs(z) * (1.0 if z.real >= 0 else -1.0)


def fit_sine(t, y, f):
    """Complex amplitude Z of the component of y at f (y ~ Re(Z exp(2 pi i f t)) + offset + drift),
    by least squares; the offset and linear drift terms absorb integrator offsets and slow drifts.
    The fit uses <= 4096 samples, but never fewer than 16 per period of f."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    per_cycle = 1.0 / (f * (t[1] - t[0])) if len(t) > 1 else 1.0
    k = int(max(1, min(len(t) // 4096, per_cycle // 16)))
    t, y = t[::k], y[::k]
    w = 2 * np.pi * f * t
    M = np.column_stack([np.cos(w), np.sin(w), np.ones_like(t), t - t.mean()])
    c = np.linalg.lstsq(M, y, rcond=None)[0]
    return complex(c[0], -c[1])


def bode_init(mode, fs, models, title, note):
    """Data for the Bode plot: one curve per (name, colour, model array); measured points are appended."""
    return dict(mode=mode, title=title, note=note, f_plan=[float(x) for x in fs], n=0, n_total=len(fs), done=False,
                ugf=None, pm=None, curves=[dict(name=n, color=c, f=[], c=[], model=[complex(v) for v in m])
                                           for n, c, m in models])


def bode_json(B):
    """Compact plot data (dB, unwrapped degrees) for the browser."""
    if not B:
        return None
    out = dict(mode=B["mode"], title=B["title"], note=B["note"], n=B["n"], n_total=B["n_total"], done=B["done"],
               f_dither=B.get("f_dither"),
               ugf=B.get("ugf"), pm=B.get("pm"), fp=[round(x, 5) for x in B["f_plan"]], curves=[])
    for cv in B["curves"]:
        d = dict(name=cv["name"], color=cv["color"], f=[round(x, 5) for x in cv["f"]], low=cv.get("low"))
        if cv["c"]:
            c = np.asarray(cv["c"], dtype=complex)
            d["db"] = np.round(20 * np.log10(np.maximum(np.abs(c), 1e-12)), 3).tolist()
            d["ph"] = np.round(np.degrees(np.unwrap(np.angle(c))), 2).tolist()
        if cv.get("model"):
            m = np.asarray(cv["model"], dtype=complex)
            d["mdb"] = np.round(20 * np.log10(np.maximum(np.abs(m), 1e-12)), 3).tolist()
            d["mph"] = np.round(np.degrees(np.unwrap(np.angle(m))), 2).tolist()
        out["curves"].append(d)
    return out


# ---------------------------------------------------------------------------------------------
# Browser UI: the schematic and the embedded scope live in ui.py
# ---------------------------------------------------------------------------------------------
def build_app(ctl):
    import ui
    return ui.build_app(ctl, sys.modules[__name__])


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
