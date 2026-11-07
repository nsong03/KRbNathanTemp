#!/usr/bin/env python3
"""
extref_diag.py -- diagnostic for extref_app: is the IN2 reference phase steady, drifting, or really jumping?

Close extref_app (one owner per Red Pitaya) and run:
    python extref_diag.py --host rp-f0fa92.local                 (measures the IN2 frequency itself)
    python extref_diag.py --host rp-f0fa92.local --f 1000.0      (or use a given frequency)

What it does (NO output is switched on, nothing is routed to OUT1 / OUT2):
  1. one scope trace of IN2: frequency (FFT + phase refinement) and peak-peak at the FPGA
  2. iq0 and iq1 on IN2 at that frequency, phases 0 and 90 deg, low-pass --lpf (default min(100, f/10) Hz),
     synchronized (iq.synchronize_iqs), outputs off
  3. for --seconds (default 10): reads r.sampler.iq0 / iq1 every 0.1 s and prints the level and the raw phase
     (no tracking), plus the phase change per step and the mean drift rate.
Reading the result: a smooth phase with a constant small drift = good reference (the app tracks that easily);
steps of tens of degrees = wrong frequency, too little low-pass, or a bad IN2 signal (see the summary at the end).
"""
import argparse
import math
import sys
import time

import numpy as np

from extref_app import measure_freq, wrap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="rp-f0fa92.local")
    ap.add_argument("--config", default="extref_diag")
    ap.add_argument("--f", type=float, default=None, help="reference frequency [Hz]; default: measured on IN2")
    ap.add_argument("--lpf", type=float, default=None, help="reference low-pass [Hz]; default min(100, f/10)")
    ap.add_argument("--seconds", type=float, default=10.0)
    a = ap.parse_args()
    from pyrpl import Pyrpl
    r = Pyrpl(config=a.config, hostname=a.host, gui=False).rp
    for n in ("iq0", "iq1"):
        getattr(r, n).output_direct = "off"
    sc = r.scope
    # 1. IN2 trace: try a long and a short trace (unknown frequency), keep the one with more points per period
    best = None
    for dur in (0.134217728, 0.008388608):
        sc.input1, sc.input2, sc.duration, sc.trigger_source = "in2", "in2", dur, "immediately"
        sc.average = True
        start = getattr(sc, "_start_trace_acquisition", None) or getattr(sc, "_start_acquisition")
        read = getattr(sc, "_get_trace", None) or getattr(sc, "_get_curve")
        start()
        time.sleep(sc.duration)
        while not sc.curve_ready():
            time.sleep(0.001)
        y = np.array(read())[0]
        t = np.arange(len(y)) * sc.duration / len(y)
        if np.ptp(y) < 0.005:
            continue
        f, pp = measure_freq(t, y)
        if f * sc.duration >= 20 and sc.duration / len(y) <= 1 / (8 * f):
            best = (f, pp, sc.duration)
            break
        best = best or (f, pp, sc.duration)
    if best is None:
        print("IN2: no sine found (peak-peak < 5 mV at the FPGA). Check the cable and the IN2 jumper.")
        return 1
    f_meas, pp, dur = best
    print(f"IN2: {f_meas:.4f} Hz, {pp*1e3:.1f} mVpp at the FPGA (x20 on HV = {pp*20:.2f} Vpp; x1 on LV) "
          f"[trace {dur*1e3:.1f} ms]")
    if pp > 1.9:
        print("  WARNING: about 2 Vpp at the FPGA = clipping: IN2 is on the LV jumper? 5 Vpp needs HV.")
    f = a.f if a.f is not None else f_meas
    lpf = a.lpf if a.lpf is not None else min(100.0, f / 10.0)
    if a.f is not None and abs(a.f - f_meas) > 0.05:
        print(f"  NOTE: --f {a.f} differs from the measured {f_meas:.4f} Hz by {a.f - f_meas:+.4f} Hz: the phase "
              f"will rotate at {360*(f_meas - a.f):+.1f} deg/s")
    # 2. reference demodulators
    for n, ph in (("iq0", 0.0), ("iq1", 90.0)):
        iq = getattr(r, n)
        iq.input, iq.gain, iq.amplitude, iq.output_direct = "in2", 0, 0, "off"
        iq.output_signal, iq.frequency, iq.phase = "quadrature", f, ph
        iq.bandwidth, iq.acbandwidth, iq.quadrature_factor = [lpf, lpf], 0, 1
    r.iq0.synchronize_iqs()
    print(f"iq0 / iq1 on IN2 at {r.iq0.frequency:.4f} Hz (register), low-pass {lpf:g} Hz x2, synchronized")
    time.sleep(0.2)
    # 3. raw phase
    print("   t [s]   level [mV]   phase [deg]   step [deg]")
    t0, prev, steps, ths, ts = time.time(), None, [], [], []
    while time.time() - t0 < a.seconds:
        x, y = float(r.sampler.iq0), float(r.sampler.iq1)
        th = math.degrees(math.atan2(y, x))
        st = 0.0 if prev is None else wrap(th - prev)
        print(f"  {time.time()-t0:6.2f}   {math.hypot(x, y)*1e3:9.2f}   {th:+11.2f}   {st:+9.2f}")
        steps.append(st); ths.append(th); ts.append(time.time() - t0)
        prev = th
        time.sleep(0.1)
    st = np.array(steps[1:])
    unwrapped = np.cumsum(np.r_[0, st])
    rate = np.polyfit(np.array(ts), unwrapped + ths[0], 1)[0] if len(ts) > 2 else 0
    resid = unwrapped + ths[0] - np.polyval(np.polyfit(np.array(ts), unwrapped + ths[0], 1), np.array(ts))
    print(f"\nmean drift {rate:+.2f} deg/s (= {rate/360:+.5f} Hz between IN2 and the NCO); "
          f"scatter around the drift {np.std(resid):.2f} deg rms; largest single step {np.max(np.abs(st)):.1f} deg")
    if np.std(resid) < 2 and abs(rate) < 90:
        print("=> GOOD: smooth reference phase. extref_app's tracking will hold it.")
    elif abs(rate) >= 90:
        print("=> The NCO is far from the IN2 frequency (or the frequency wanders): use the measured frequency.")
    else:
        print("=> NOISY phase: lower --lpf (try f/30), check the IN2 signal / jumper, or the reference frequency "
              "is not stable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
