#!/usr/bin/env python3
"""
cavity_lock.py -- calibrate and lock a cavity PZT to an SR810 dither-lock error signal
with a Red Pitaya running PyRPL.

Signal chain
  DG922 CH1  1 kHz sine, 40 mVpp  -> Red Pitaya IN2  (LV)  -> pid1 (x0.1) -> OUT1   (dither)
  DG922 CH2  1 kHz square 0-5 V   -> SR810 REF IN
  Photodiode (cavity transmission)-> SR810 A
  SR810 CH1 OUTPUT (X)            -> Red Pitaya IN1  (HV jumper)
  asg0 (sweep, Part 1)            -> OUT1 + OUT2
  pid0 (lock, Part 2): IN1        -> OUT1 + OUT2
  Red Pitaya OUT1                 -> PZT driver MOD IN
  Red Pitaya OUT2                 -> scope (monitor: sweep / correction, no dither)

Commands
  python cavity_lock.py check       dither pass-through check (IN2 -> OUT1)
  python cavity_lock.py sweep       run the calibration sweep until Ctrl-C (set the SR810 phase)
  python cavity_lock.py calibrate   measure V0, slope S, linewidth -> calibration.json + calibration.png
  python cavity_lock.py widesweep   find the nearest other mode -> clamp width (updates calibration.json)
  python cavity_lock.py lock        park, engage, raise gain, watchdog + auto-relock (Ctrl-C = hold + stop)
  python cavity_lock.py stop        integrator off, sweep off, output held   (--zero ramps OUT1 to 0)
  python cavity_lock.py demo --simulate   the whole sequence on the model, with two kicks
Add --simulate to any command to run against a built-in model instead of hardware.
"""
import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np

# ============================ settings ===================================
HOSTNAME = "192.168.1.100"      # Red Pitaya IP address
PYRPL_CONFIG = "cavity_lock"    # PyRPL config file name

DG922_DITHER_VPP = 0.040        # DG922 CH1 amplitude into IN2 [Vpp]
DITHER_PASS_GAIN = 0.1          # pid1.p  -> dither on OUT1 = 4 mVpp
IN1_SCALE = 20.0                # IN1 HV jumper: volts at the BNC per raw volt read

SWEEP_FREQ = 5.0                # calibration sweep rate [Hz]
SWEEP_AMPL = 0.010              # calibration sweep amplitude, peak [V]  (20 mVpp)
WIDE_SWEEP_FREQ = 1.0           # wide sweep rate [Hz]
WIDE_SWEEP_AMPL = 0.20          # wide sweep amplitude, peak [V]
SWEEP_WAVEFORM = "ramp"         # PyRPL asg waveform; check on the scope that it is a triangle

LOCKIN_TAU = 1e-3               # SR810 time constant [s]
LOCKIN_POLES = 4                # 24 dB/oct

UGF_STEPS = [1.0, 2.0, 5.0, 10.0]   # loop unity-gain frequency ramp [Hz]
UGF_STEP_WAIT = 2.0             # s at each step
CLAMP_FRACTION = 0.4            # clamp half-width = this x distance to the nearest other mode
CLAMP_DEFAULT = 0.05            # clamp half-width if no wide sweep yet [V]
OUT_LIMIT = 0.95                # never command OUT1 beyond +/- this [V]

SR810_VISA = None               # e.g. "GPIB0::8::INSTR" or "ASRL/dev/ttyUSB0::INSTR": enables the
                                # transmission monitor (photodiode teed into SR810 rear AUX IN 1)
SR810_SENS = 0.5                # SR810 sensitivity [V], used to predict the locked transmission
T_LOCK_FRACTION = 0.5           # locked only if transmission > this x the expected locked level

WATCH_PERIOD = 0.1              # watchdog tick [s]
UNLOCK_X_FRACTION = 0.5         # |X| > this x lobe height ...
UNLOCK_X_TIME = 0.5             # ... for this long [s]  -> unlocked
UNLOCK_EDGE_FRACTION = 0.05     # ival within this fraction of the clamp range of an edge -> unlocked
RELOCK_TRIES = 5

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "calibration.json")
LOG_FILE = os.path.join(HERE, "lock_log.csv")


# ============================ physics helpers =============================
def h1(x, m):
    """First Fourier coefficient of a unit Lorentzian under dither depth m (half-linewidth units)."""
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


def lobe_position(m):
    """Detuning of the error-signal lobe (max |H1|), in half-linewidths."""
    xs = np.linspace(1e-3, 3.0 + 2.0 * m, 6000)
    return xs[np.argmax(np.abs(h1(xs, m)))]


def kappa_from_lobes(lobe_sep, dither_pp):
    """Solve lobe separation (drive V) = lobe_position(dither_pp/kappa) * kappa for kappa (drive V)."""
    g = lambda k: lobe_position(dither_pp / k) * k - lobe_sep
    lo, hi = dither_pp / 20.0, dither_pp * 200.0
    if g(lo) > 0 or g(hi) < 0:
        return None
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        if g(mid) > 0:
            hi = mid
        else:
            lo = mid
    return math.sqrt(lo * hi)


def expected_locked_transmission(cal):
    """Photodiode level when locked, predicted from the calibrated X lobe height:
    lobe = sqrt(2) max|H1| V_pk x 10/SENS ; locked average = V_pk / sqrt(1 + m^2)."""
    m = cal["dither_pp"] / cal["kappa"] if cal.get("kappa") else 1 / math.sqrt(2)
    xs = np.linspace(1e-3, 3 + 2 * m, 4000)
    c = math.sqrt(2) * np.max(np.abs(h1(xs, m)))
    v_pk = cal["lobe_height"] * IN1_SCALE * SR810_SENS / (10 * c)
    return v_pk / math.sqrt(1 + m * m)


# ============================ Red Pitaya wrapper ==========================
class RP:
    """Thin layer over PyRPL so the same procedure runs on hardware or on the model."""

    def __init__(self, simulate=False):
        self.simulate = simulate
        if simulate:
            self.r = FakeRedPitaya()
        else:
            from pyrpl import Pyrpl
            self.r = Pyrpl(config=PYRPL_CONFIG, hostname=HOSTNAME, gui=False).rp
        self.quiet_others()
        self.sr = None
        if SR810_VISA and not simulate:
            import pyvisa
            self.sr = pyvisa.ResourceManager().open_resource(SR810_VISA)
            self.sr.write("OUTX 1" if "GPIB" in SR810_VISA.upper() else "OUTX 0")   # answer on this port

    def quiet_others(self):
        """Modules this script does not use must not write to OUT1/OUT2 (old PyRPL configs can leave them on)."""
        for name in ("asg1", "pid2", "iq0", "iq1", "iq2"):
            try:
                getattr(self.r, name).output_direct = "off"
            except Exception:
                pass

    # --- timing ---
    def wait(self, dt):
        if self.simulate:
            self.r.advance(dt)
        else:
            time.sleep(dt)

    # --- module setup ---
    def setup_dither(self):
        p1 = self.r.pid1
        p1.i = 0
        p1.ival = 0
        p1.p = DITHER_PASS_GAIN
        p1.setpoint = 0
        self._filters_off(p1)
        p1.max_voltage, p1.min_voltage = 1.0, -1.0
        p1.input = "in2"
        p1.output_direct = "out1"

    def setup_lock_pid(self, ival=0.0, vmin=-OUT_LIMIT, vmax=OUT_LIMIT):
        p0 = self.r.pid0
        p0.i = 0
        p0.p = 0
        p0.setpoint = 0
        self._filters_off(p0)
        p0.min_voltage, p0.max_voltage = vmin, vmax
        p0.ival = ival
        p0.input = "in1"
        p0.output_direct = "both"

    @staticmethod
    def _filters_off(pid):
        try:
            pid.inputfilter = [0, 0, 0, 0]
        except Exception:
            pass

    def sweep_on(self, ampl, freq):
        self.r.asg0.setup(waveform=SWEEP_WAVEFORM, frequency=freq, amplitude=ampl, offset=0,
                          trigger_source="immediately", output_direct="both")

    def sweep_off(self):
        a = self.r.asg0
        a.output_direct = "off"
        a.amplitude = 0
        a.offset = 0

    # --- acquisition ---
    def capture(self, input1, input2, duration):
        s = self.r.scope
        s.input1, s.input2 = input1, input2
        opts = getattr(s, "durations", None)
        if opts:
            longer = [d for d in opts if d >= duration]
            duration = min(longer) if longer else max(opts)
        s.duration = duration
        s.trigger_source = "immediately"
        try:
            s.average = True
        except Exception:
            pass
        if hasattr(s, "single"):
            data = np.array(s.single())
        else:
            data = np.array(s.single_async().result())
        n = data.shape[-1]
        t = np.array(getattr(s, "times", np.linspace(0, s.duration, n)))[:n]
        return t, data[0], data[1]

    def read_x(self, n=20):
        """Mean of the raw IN1 reading."""
        vals = [float(self.r.sampler.in1) for _ in range(n)]
        return float(np.mean(vals))

    @property
    def has_transmission(self):
        return self.simulate or self.sr is not None

    def transmission(self):
        """Photodiode DC level [V] via SR810 AUX IN 1 (None if no monitor)."""
        if self.simulate:
            return self.r.transmission()
        if self.sr is None:
            return None
        return float(self.sr.query("OAUX? 1"))

    @property
    def pid0(self):
        return self.r.pid0


# ============================ analysis ====================================
def _binned(v, x, nbins=400):
    order = np.argsort(v)
    v, x = v[order], x[order]
    edges = np.linspace(v[0], v[-1], nbins + 1)
    idx = np.clip(np.digitize(v, edges) - 1, 0, nbins - 1)
    cnt = np.bincount(idx, minlength=nbins)
    ok = cnt > 0
    vb = (np.bincount(idx, v, nbins)[ok] / cnt[ok])
    xb = (np.bincount(idx, x, nbins)[ok] / cnt[ok])
    return vb, xb


def _one_feature(vb, xb):
    """Biggest dispersive feature: lobes, zero crossing, slope."""
    i_hi, i_lo = int(np.argmax(xb)), int(np.argmin(xb))
    a, b = sorted((i_hi, i_lo))
    seg_v, seg_x = vb[a:b + 1], xb[a:b + 1]
    sc = np.where(np.diff(np.sign(seg_x)) != 0)[0]
    if len(sc) == 0:
        return None
    mid = (a + b) / 2.0
    k = sc[np.argmin(np.abs(sc + a - mid))]
    v1, v2, x1, x2 = seg_v[k], seg_v[k + 1], seg_x[k], seg_x[k + 1]
    vz = v1 - x1 * (v2 - v1) / (x2 - x1)
    lobe_sep = abs(vb[i_hi] - vb[i_lo])
    near = np.abs(seg_v - vz) < 0.25 * lobe_sep
    if near.sum() < 3:
        near = np.abs(seg_v - vz) <= np.sort(np.abs(seg_v - vz))[min(4, len(seg_v) - 1)]
    slope = np.polyfit(seg_v[near], seg_x[near], 1)[0]
    return dict(vz=float(vz), slope=float(slope), lobe_sep=float(lobe_sep),
                lobe_height=float((xb[i_hi] - xb[i_lo]) / 2.0),
                v_hi=float(vb[i_hi]), v_lo=float(vb[i_lo]))


def analyze_sweep(t, x, ramp, center, sweep_rate):
    """Split into up/down halves, find the TEM00 feature in each, combine."""
    dr = np.gradient(ramp, t)
    res = {}
    for name, sel in (("up", dr > 0), ("down", dr < 0)):
        if sel.sum() < 200:
            continue
        vb, xb = _binned(center + ramp[sel], x[sel])
        f = _one_feature(vb, xb)
        if f:
            res[name] = f
    if not res:
        raise RuntimeError("no error-signal feature found in the sweep window")
    lag = sweep_rate * LOCKIN_POLES * LOCKIN_TAU          # drive volts the filter delay shifts a crossing
    if "up" in res and "down" in res:
        v0 = 0.5 * (res["up"]["vz"] + res["down"]["vz"])
        split = res["up"]["vz"] - res["down"]["vz"]
    elif "up" in res:
        v0, split = res["up"]["vz"] - lag, None
    else:
        v0, split = res["down"]["vz"] + lag, None
    fs = list(res.values())
    return dict(v0=float(v0), slope=float(np.mean([f["slope"] for f in fs])),
                lobe_sep=float(np.mean([f["lobe_sep"] for f in fs])),
                lobe_height=float(np.mean([f["lobe_height"] for f in fs])),
                updown_split=None if split is None else float(split),
                expected_lag=float(lag), halves=res)


def find_modes(t, x, ramp, center, slope_sign, sweep_rate, rel=0.10):
    """All stable zero crossings with a lobe > rel x the biggest lobe (one per cavity mode)."""
    dr = np.gradient(ramp, t)
    up = (dr > 0).sum() >= (dr < 0).sum()
    sel = dr > 0 if up else dr < 0
    lag = sweep_rate * LOCKIN_POLES * LOCKIN_TAU * (1 if up else -1)
    vb, xb = _binned(center + ramp[sel] - lag, x[sel], nbins=2000)
    big = np.max(np.abs(xb))
    sc = np.where(np.diff(np.sign(xb)) != 0)[0]
    modes = []
    for k in sc:
        if np.sign(xb[k + 1] - xb[k]) != slope_sign:
            continue
        w = slice(max(0, k - 60), min(len(xb), k + 60))
        amp = 0.5 * (np.max(xb[w]) - np.min(xb[w]))
        if amp > rel * big:
            vz = vb[k] - xb[k] * (vb[k + 1] - vb[k]) / (xb[k + 1] - xb[k])
            modes.append((float(vz), float(amp)))
    return modes


# ============================ commands ====================================
def load_cal():
    if not os.path.exists(CAL_FILE):
        sys.exit("No calibration.json yet: run  python cavity_lock.py calibrate  first.")
    with open(CAL_FILE) as f:
        return json.load(f)


def save_cal(cal):
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f, indent=2)


def cmd_check(rp):
    """Dither pass-through: IN2 should be ~40 mVpp, OUT1 ~4 mVpp, both 1 kHz."""
    rp.setup_dither()
    rp.sweep_off()
    rp.setup_lock_pid(ival=rp.pid0.ival)
    rp.wait(0.2)
    t, a, b = rp.capture("in2", "out1", 0.008)
    pp_in, pp_out = np.ptp(a), np.ptp(b)
    f = np.fft.rfftfreq(len(b), t[1] - t[0])
    f_pk = f[1:][np.argmax(np.abs(np.fft.rfft(b - b.mean()))[1:])]
    print(f"IN2  {pp_in*1e3:6.2f} mVpp   (expect {DG922_DITHER_VPP*1e3:.0f})")
    print(f"OUT1 {pp_out*1e3:6.2f} mVpp   (expect {DG922_DITHER_VPP*DITHER_PASS_GAIN*1e3:.1f}), "
          f"strongest line {f_pk:.0f} Hz")
    ok = abs(pp_out / max(pp_in, 1e-9) - DITHER_PASS_GAIN) < 0.3 * DITHER_PASS_GAIN
    print("PASS" if ok else "CHECK: OUT1/IN2 ratio is off -- look at pid1 settings and the IN2 jumper")


def cmd_sweep(rp):
    """Calibration sweep on until Ctrl-C: null the rear-panel Y feature with [Phase]."""
    rp.setup_dither()
    rp.setup_lock_pid(ival=rp.pid0.ival)
    rp.sweep_on(SWEEP_AMPL, SWEEP_FREQ)
    print(f"Sweeping {2*SWEEP_AMPL*1e3:.0f} mVpp at {SWEEP_FREQ:g} Hz around {rp.pid0.ival*1e3:+.1f} mV. "
          "Null the Y feature with [Phase]. Ctrl-C to stop.")
    try:
        while True:
            rp.wait(0.5)
            if rp.simulate:
                break
    except KeyboardInterrupt:
        pass
    rp.sweep_off()
    print("Sweep off.")


def run_calibration(rp, center, ampl=SWEEP_AMPL, freq=SWEEP_FREQ, plot=True):
    rp.setup_dither()
    rp.setup_lock_pid(ival=center)
    rp.sweep_on(ampl, freq)
    rp.wait(3.0 / freq)                                   # let the lock-in settle
    dur = 2.2 / freq
    t, x, ramp = rp.capture("in1", "asg0", dur)
    rp.sweep_off()
    rate = 4 * ampl * freq                                 # V/s on a triangle
    a = analyze_sweep(t, x, ramp, center, rate)
    if plot:
        plot_calibration(t, x, ramp, center, a)
    return a


def cmd_calibrate(rp):
    center = float(rp.pid0.ival)
    a = run_calibration(rp, center)
    dither_pp = DG922_DITHER_VPP * DITHER_PASS_GAIN
    kappa = kappa_from_lobes(a["lobe_sep"], dither_pp)
    cal = load_cal() if os.path.exists(CAL_FILE) else {}
    cal.update(v0=a["v0"], slope=a["slope"], lobe_sep=a["lobe_sep"], lobe_height=a["lobe_height"],
               kappa=kappa, dither_pp=dither_pp, time=time.strftime("%Y-%m-%d %H:%M:%S"))
    hw = cal.get("clamp_halfwidth", CLAMP_DEFAULT)
    cal["clamp"] = [max(cal["v0"] - hw, -OUT_LIMIT), min(cal["v0"] + hw, OUT_LIMIT)]
    save_cal(cal)
    print(f"V0 (resonance)     {a['v0']*1e3:+8.3f} mV on OUT1")
    if a["updown_split"] is not None:
        print(f"up/down split      {a['updown_split']*1e3:8.3f} mV  (filter lag alone ~{2*a['expected_lag']*1e3:.3f} mV)")
    print(f"slope S            {a['slope']:+8.3f} V/V  (raw IN1 per OUT1)")
    print(f"lobe height        {a['lobe_height']*IN1_SCALE:8.3f} V at the SR810 output")
    if kappa:
        ratio = dither_pp / kappa
        print(f"linewidth kappa    {kappa*1e3:8.3f} mV of drive;  dither = {ratio:.2f} kappa pp (optimum 0.71)")
        if not 0.45 < ratio < 1.1:
            print(f"  -> set DG922 CH1 to about {DG922_DITHER_VPP*0.71/ratio*1e3:.0f} mVpp and recalibrate")
    else:
        print("linewidth          could not solve (dither much larger or smaller than the line?)")
    print(f"clamp              {cal['clamp'][0]*1e3:+.1f} to {cal['clamp'][1]*1e3:+.1f} mV"
          + ("" if "clamp_halfwidth" in cal else "  (default -- run widesweep to set it from the mode spacing)"))
    print(f"wrote {CAL_FILE} and calibration.png")


def cmd_widesweep(rp):
    cal = load_cal()
    center = cal["v0"]
    rp.setup_dither()
    rp.setup_lock_pid(ival=center)
    rp.sweep_on(WIDE_SWEEP_AMPL, WIDE_SWEEP_FREQ)
    rp.wait(1.5 / WIDE_SWEEP_FREQ)
    t, x, ramp = rp.capture("in1", "asg0", 1.1 / WIDE_SWEEP_FREQ)
    rp.sweep_off()
    rp.setup_lock_pid(ival=center)
    modes = find_modes(t, x, ramp, center, np.sign(cal["slope"]), 4 * WIDE_SWEEP_AMPL * WIDE_SWEEP_FREQ)
    if not modes:
        sys.exit("No modes found in the wide sweep; widen WIDE_SWEEP_AMPL or check the error signal.")
    main = min(modes, key=lambda m: abs(m[0] - cal["v0"]))
    others = [m for m in modes if m is not main]
    print("modes found (drive mV, lobe size):")
    for v, amp in modes:
        print(f"   {v*1e3:+8.2f}  {amp*IN1_SCALE:6.3f} V" + ("   <- TEM00 (calibrated)" if (v, amp) == main else ""))
    if others:
        spacing = min(abs(v - main[0]) for v, _ in others)
        hw = CLAMP_FRACTION * spacing
        cal["mode_spacing"] = spacing
        print(f"nearest other mode {spacing*1e3:.1f} mV away -> clamp half-width {hw*1e3:.1f} mV")
    else:
        hw = CLAMP_FRACTION * WIDE_SWEEP_AMPL
        print(f"no other mode within +/-{WIDE_SWEEP_AMPL*1e3:.0f} mV -> clamp half-width {hw*1e3:.1f} mV")
    cal["clamp_halfwidth"] = hw
    cal["clamp"] = [max(cal["v0"] - hw, -OUT_LIMIT), min(cal["v0"] + hw, OUT_LIMIT)]
    save_cal(cal)
    plot_wide(t, x, ramp, center, modes, cal)
    print(f"clamp {cal['clamp'][0]*1e3:+.1f} to {cal['clamp'][1]*1e3:+.1f} mV; wrote {CAL_FILE} and widesweep.png")


def engage(rp, cal, log):
    """Park at V0, then ramp the integrator gain. Returns True if it holds."""
    vmin, vmax = cal["clamp"]
    rp.sweep_off()
    rp.setup_lock_pid(ival=cal["v0"], vmin=vmin, vmax=vmax)
    rp.wait(0.3)
    s = cal["slope"]
    for ugf in UGF_STEPS:
        rp.pid0.i = -ugf / s                               # i * S < 0 -> negative feedback, |i S| = UGF
        i_act = float(rp.pid0.i)                           # PyRPL quantizes i (steps of ~0.0046 Hz)
        if i_act == 0:
            print(f"  i = {-ugf/s:.4g} Hz is below PyRPL's resolution: lower the error-signal gain "
                  "(SR810 sensitivity) or attenuate OUT1, then recalibrate")
            return False
        ugf_act = abs(i_act * s)
        steps = int(UGF_STEP_WAIT / WATCH_PERIOD)
        for _ in range(steps):
            rp.wait(WATCH_PERIOD)
            state = status(rp, cal)
            log(state, "engage", ugf)
            if state["at_edge"]:
                print(f"  ran to the clamp at UGF {ugf:g} Hz -> wrong sign or not near resonance")
                rp.pid0.i = 0
                return False
        tr = "" if state["T"] is None else f"   T {state['T']*1e3:6.1f} mV"
        print(f"  UGF {ugf_act:5.2f} Hz (i = {i_act:+.4f})   X {state['x']*IN1_SCALE:+7.3f} V   "
              f"ival {state['ival']*1e3:+8.3f} mV{tr}")
    if state["t_low"]:
        print("  transmission low after engaging -> parked off resonance")
        rp.pid0.i = 0
        return False
    return True


def status(rp, cal):
    ival = float(rp.pid0.ival)
    x = rp.read_x()
    tr = rp.transmission()
    vmin, vmax = cal["clamp"]
    margin = UNLOCK_EDGE_FRACTION * (vmax - vmin)
    t_low = tr is not None and tr < T_LOCK_FRACTION * expected_locked_transmission(cal)
    return dict(ival=ival, x=x, T=tr, at_edge=(ival < vmin + margin or ival > vmax - margin),
                x_big=abs(x) > UNLOCK_X_FRACTION * cal["lobe_height"], t_low=t_low)


def cmd_lock(rp, max_time=None):
    cal = load_cal()
    rp.setup_dither()
    t0 = time.time()
    sim_t = [0.0]
    f = open(LOG_FILE, "w", newline="")
    w = csv.writer(f)
    w.writerow(["t_s", "state", "ugf_hz", "ival_V", "x_raw_V", "transmission_V"])

    def log(state, phase, ugf):
        tt = sim_t[0] if rp.simulate else time.time() - t0
        w.writerow([f"{tt:.2f}", phase, ugf, f"{state['ival']:.6f}", f"{state['x']:.6f}",
                    "" if state["T"] is None else f"{state['T']:.5f}"])

    if rp.has_transmission:
        print(f"Transmission monitor on: expected locked level {expected_locked_transmission(cal)*1e3:.0f} mV")
    else:
        print("No transmission monitor (SR810_VISA unset): unlocks far from resonance cannot be detected; "
              "watch the photodiode on the scope.")
    tries = 0
    try:
        while tries <= RELOCK_TRIES:
            print(f"Parking at V0 = {cal['v0']*1e3:+.3f} mV, clamp {cal['clamp'][0]*1e3:+.1f}..{cal['clamp'][1]*1e3:+.1f} mV")
            if engage(rp, cal, log):
                print("LOCKED. Watching (Ctrl-C to hold and stop).")
                good = []
                x_big_since = None
                while True:
                    rp.wait(WATCH_PERIOD)
                    sim_t[0] += WATCH_PERIOD
                    st = status(rp, cal)
                    log(st, "locked", UGF_STEPS[-1])
                    now = sim_t[0] if rp.simulate else time.time()
                    bad = st["t_low"] if st["T"] is not None else st["x_big"]
                    if not bad:
                        x_big_since = None
                    elif x_big_since is None:
                        x_big_since = now
                    lost = st["at_edge"] or (x_big_since is not None and now - x_big_since > UNLOCK_X_TIME)
                    if not lost:
                        good.append(st["ival"])
                        good = good[-50:]
                    else:
                        why = ("correction at the clamp" if st["at_edge"] else
                               "transmission low" if st["T"] is not None else "error signal large")
                        print(f"UNLOCK ({why}) at ival {st['ival']*1e3:+.3f} mV")
                        break
                    if max_time and (now - (0 if rp.simulate else t0)) > max_time:
                        print("simulation time limit reached")
                        return cal
            else:
                good = [cal["v0"]]
            tries += 1
            if tries > RELOCK_TRIES:
                break
            center = float(np.median(good)) if good else cal["v0"]
            rp.pid0.i = 0
            print(f"Relock {tries}/{RELOCK_TRIES}: searching around {center*1e3:+.3f} mV")
            found = relock_search(rp, cal, center)
            if found is None:
                print("  TEM00 not found inside the allowed range: re-centre with the PZT driver DC knob, "
                      "then run calibrate again.")
                break
            cal.update(found)
        print("Giving up after repeated unlocks: integrator off, output held.")
    except KeyboardInterrupt:
        print("\nCtrl-C")
    finally:
        rp.pid0.i = 0
        print(f"Integrator off, output held at {float(rp.pid0.ival)*1e3:+.3f} mV. Log: {LOG_FILE}")
        f.close()
    return cal


def relock_search(rp, cal, center):
    """Sweep around the last good point, widening x3 each time at a constant sweep rate.
    Accept only a feature at least 0.6x the calibrated lobe height (TEM00, not a higher mode)."""
    hw = 0.5 * (cal["clamp"][1] - cal["clamp"][0])
    ampl = SWEEP_AMPL
    while ampl <= max(hw, SWEEP_AMPL):
        freq = SWEEP_FREQ * SWEEP_AMPL / ampl
        try:
            a = run_calibration(rp, center, ampl=ampl, freq=freq, plot=False)
        except RuntimeError:
            a = None
        if a and a["lobe_height"] > 0.6 * cal["lobe_height"] and abs(a["v0"]) < OUT_LIMIT:
            print(f"  found TEM00 at {a['v0']*1e3:+.3f} mV (sweep +/-{ampl*1e3:.0f} mV)")
            return dict(v0=a["v0"], slope=a["slope"], lobe_height=a["lobe_height"],
                        clamp=[max(a["v0"] - hw, -OUT_LIMIT), min(a["v0"] + hw, OUT_LIMIT)])
        ampl *= 3
    rp.setup_lock_pid(ival=center)
    return None


def cmd_stop(rp, zero=False):
    rp.pid0.i = 0
    rp.sweep_off()
    if zero:
        v = float(rp.pid0.ival)
        for k in range(40, -1, -1):
            rp.pid0.ival = v * k / 40
            rp.wait(0.05)
    print(f"Stopped. OUT1 correction held at {float(rp.pid0.ival)*1e3:+.3f} mV.")


# ============================ plots =======================================
def plot_calibration(t, x, ramp, center, a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4.5))
    dr = np.gradient(ramp, t)
    for sel, lab, c in ((dr > 0, "up-sweep", "#c0392b"), (dr < 0, "down-sweep", "#2471a3")):
        ax.plot((center + ramp[sel]) * 1e3, x[sel] * IN1_SCALE, ".", ms=1.5, color=c, label=lab)
    ax.axvline(a["v0"] * 1e3, color="k", ls="--", lw=1, label=f"V0 = {a['v0']*1e3:+.3f} mV")
    vv = np.linspace(a["v0"] - 0.3 * a["lobe_sep"], a["v0"] + 0.3 * a["lobe_sep"], 10)
    ax.plot(vv * 1e3, a["slope"] * (vv - a["v0"]) * IN1_SCALE, color="#27ae60", lw=2,
            label=f"slope S = {a['slope']:+.2f} (raw V/V)")
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set(xlabel="drive on OUT1 [mV]", ylabel="SR810 X [V]", title="Calibration sweep")
    ax.legend(fontsize=8)
    fig.savefig(os.path.join(HERE, "calibration.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)


def plot_wide(t, x, ramp, center, modes, cal):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot((center + ramp) * 1e3, x * IN1_SCALE, ".", ms=1, color="#2471a3")
    for v, _ in modes:
        ax.axvline(v * 1e3, color="0.6", ls=":", lw=1)
    ax.axvspan(cal["clamp"][0] * 1e3, cal["clamp"][1] * 1e3, color="#27ae60", alpha=0.12, label="clamp")
    ax.set(xlabel="drive on OUT1 [mV]", ylabel="SR810 X [V]", title="Wide sweep: modes and clamp")
    ax.legend(fontsize=8)
    fig.savefig(os.path.join(HERE, "widesweep.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)


# ============================ simulation model ============================
class _Obj:
    pass


class FakeRedPitaya:
    """Cavity + SR810 + Red Pitaya model: quasi-static dither error signal, 4-pole lock-in filter,
    PyRPL-style PID (output = p e + ival, d ival/dt = 2 pi i e, clamped), drifting resonance."""

    KAPPA = 0.0057                     # linewidth in OUT1 volts
    MODES = [(0.0, 1.0), (0.120, 0.30), (-0.085, 0.12)]   # (offset from TEM00 [V], relative height)
    DRIFT_RATE = 0.0004                # V/s slow drift
    DIFFUSION = 0.0003                 # V/sqrt(s)
    SENS = 0.5                         # SR810 sensitivity
    NOISE_RAW = 3e-4                   # raw IN1 noise rms
    DT = 2e-4

    def __init__(self, seed=3):
        self.rng = np.random.default_rng(seed)
        self.t = 0.0
        self.vc = 0.0031                # TEM00 position on OUT1
        self.kicks = []                  # (time, jump) pairs to test unlock/relock
        self.filt = np.zeros(LOCKIN_POLES)
        self.in2_vpp = DG922_DITHER_VPP
        self.asg0 = _Obj()
        self.asg0.waveform, self.asg0.frequency, self.asg0.amplitude = "ramp", 1.0, 0.0
        self.asg0.offset, self.asg0.output_direct = 0.0, "off"
        self.asg0.setup = self._asg_setup
        self.pid0, self.pid1 = self._pid(), self._pid()
        self.scope = _Obj()
        self.scope.input1, self.scope.input2, self.scope.duration = "in1", "in2", 0.1
        self.scope.trigger_source, self.scope.average = "immediately", True
        self.scope.single = self._scope_single
        self.sampler = _Sampler(self)
        self.last = {}

    @staticmethod
    def _pid():
        p = _FakePid()
        p.input, p.output_direct, p.setpoint, p.p, p.i, p.ival = "off", "off", 0.0, 0.0, 0.0, 0.0
        p.max_voltage, p.min_voltage, p.inputfilter = 1.0, -1.0, [0, 0, 0, 0]
        return p

    def _asg_setup(self, **kw):
        for k, v in kw.items():
            setattr(self.asg0, k, v)
        self.asg_t0 = self.t

    def _asg_value(self):
        a = self.asg0
        if a.output_direct == "off" or a.amplitude == 0:
            return 0.0
        ph = ((self.t - getattr(self, "asg_t0", 0.0)) * a.frequency) % 1.0
        tri = 1 - 4 * abs(ph - 0.5)                         # triangle starting at -1
        return a.offset + a.amplitude * tri

    def _step(self):
        dt = self.DT
        self.t += dt
        self.vc += self.DRIFT_RATE * dt + self.DIFFUSION * math.sqrt(dt) * self.rng.standard_normal()
        for k in [k for k in self.kicks if k[0] <= self.t]:
            self.vc += k[1]
            self.kicks.remove(k)
        dither_pp = self.in2_vpp * self.pid1.p if (self.pid1.input == "in2" and self.pid1.output_direct in ("out1", "both")) else 0.0
        asg = self._asg_value()
        e = self.sampler_value_in1() - self.pid0.setpoint
        p0 = self.pid0
        if p0.input == "in1" and p0.i != 0:
            p0.ival = float(np.clip(p0.ival + 2 * math.pi * p0.i * e * dt, p0.min_voltage, p0.max_voltage))
        pid_out = float(np.clip(p0.p * e + p0.ival, p0.min_voltage, p0.max_voltage)) if p0.output_direct != "off" else 0.0
        v = asg + pid_out
        # quasi-static SR810 X for the drive v
        x = 0.0
        if dither_pp > 0:
            m = dither_pp / self.KAPPA
            for off, h in self.MODES:
                x += h * h1(2 * (v - (self.vc + off)) / self.KAPPA, m)
        x_out = np.clip(10 * math.sqrt(2) * 1.0 * x / self.SENS, -10.9, 10.9)
        a = 1 - math.exp(-dt / LOCKIN_TAU)
        y = x_out
        for k in range(LOCKIN_POLES):
            self.filt[k] += a * (y - self.filt[k])
            y = self.filt[k]
        self.last = dict(in1=y / IN1_SCALE, asg0=asg, out1=v, out2=asg + pid_out,
                         pid0=pid_out, in2=0.0)

    def transmission(self):
        """Average photodiode level over a dither cycle at the present drive (V_pk = 1 V)."""
        v = self.last.get("out1", 0.0)
        d = 0.5 * self.in2_vpp * self.pid1.p
        ph = np.linspace(0, 2 * np.pi, 64, endpoint=False)
        tot = 0.0
        for off, h in self.MODES:
            tot += h * np.mean(1 / (1 + (2 * (v + d * np.sin(ph) - (self.vc + off)) / self.KAPPA) ** 2))
        return float(tot) + 0.002 * self.rng.standard_normal()

    def sampler_value_in1(self):
        return self.last.get("in1", 0.0)

    def advance(self, dt):
        for _ in range(int(round(dt / self.DT))):
            self._step()

    def _scope_single(self):
        n = int(round(self.scope.duration / self.DT))
        out = np.zeros((2, n))
        dither_pp = self.in2_vpp * self.pid1.p
        for k in range(n):
            self._step()
            for c, name in enumerate((self.scope.input1, self.scope.input2)):
                if name == "in2":
                    val = 0.5 * self.in2_vpp * math.sin(2 * math.pi * 1000 * self.t)
                elif name == "out1":
                    val = self.last["out1"] + 0.5 * dither_pp * math.sin(2 * math.pi * 1000 * self.t)
                elif name == "in1":
                    val = self.last["in1"] + self.NOISE_RAW * self.rng.standard_normal()
                else:
                    val = self.last.get(name, 0.0)
                out[c, k] = val
        self.scope.times = np.arange(n) * self.DT
        return out


class _FakePid:
    I_STEP = 1.0 / (2 ** 32 * 2 * math.pi * 8e-9)        # PyRPL integral-gain resolution, ~0.0046 Hz

    @property
    def i(self):
        return self._i

    @i.setter
    def i(self, v):
        self._i = round(v / self.I_STEP) * self.I_STEP


class _Sampler:
    def __init__(self, fake):
        self.fake = fake

    @property
    def in1(self):
        return self.fake.last.get("in1", 0.0) + self.fake.NOISE_RAW * self.fake.rng.standard_normal()


# ============================ main ========================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "sweep", "calibrate", "widesweep", "lock", "stop", "demo"])
    ap.add_argument("--simulate", action="store_true", help="run against the built-in model")
    ap.add_argument("--zero", action="store_true", help="with stop: ramp the correction back to 0 V")
    args = ap.parse_args()
    rp = RP(simulate=args.simulate)
    if args.command == "check":
        cmd_check(rp)
    elif args.command == "sweep":
        cmd_sweep(rp)
    elif args.command == "calibrate":
        cmd_calibrate(rp)
    elif args.command == "widesweep":
        cmd_widesweep(rp)
    elif args.command == "lock":
        cmd_lock(rp, max_time=20.0 if args.simulate else None)
    elif args.command == "stop":
        cmd_stop(rp, zero=args.zero)
    elif args.command == "demo":
        if not args.simulate:
            sys.exit("demo runs only with --simulate")
        rp.r.kicks = [(rp.r.t + 25.0, 0.004), (rp.r.t + 40.0, 0.025)]   # a small and a large jump
        for name, fn in (("check", cmd_check), ("calibrate", cmd_calibrate), ("widesweep", cmd_widesweep),
                         ("calibrate", cmd_calibrate)):
            print(f"\n== {name}")
            fn(rp)
        print("\n== lock (a 4 mV jump at t=25 s, a 25 mV jump at t=40 s)")
        cmd_lock(rp, max_time=40.0)


if __name__ == "__main__":
    main()
