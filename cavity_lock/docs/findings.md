# Findings so far (cavity PZT dither lock)

This summarizes everything learned up to the hand-off on 2026-10-31. More detail is in:
- `speed_latency_noise_study.md`: latency, noise and the next measurement plan;
- `pyrpl_reference.md`: FPGA and PyRPL facts;
- `moku_reference.md`: the Moku UI conventions to copy.

## 1. Physics of the dither (transmission) lock
- The error after the lock-in is the first harmonic of the Lorentzian under a sinusoidal dither, quasi-static because f_dither ≪ κ:
  H1(x, m) = Im[z / (√(z−m)·√(z+m))] / m, with z = x + i.
  - x is the detuning in half-widths.
  - m is the dither amplitude in half-widths.
- The steepest error slope comes at a dither of **0.71 κ peak-to-peak**. At that dither, the transmission is modulated at 2f by **34 % peak-to-peak** and the mean transmission is 82 %.
  - A smaller lock dither trades slope for less intensity ripple: 0.3 κ pp keeps 68 % of the slope with 8 % ripple, and 0.1 κ pp keeps 26 % with 1 %.
- Neighboring cavity modes pull the lock point when they are close. `earlier/multipeak_sim/` quantifies the shift against their separation.
  - The app clamps `pid0` to ±0.4 × (distance to the nearest other mode) so the lock cannot hop modes.
- The lock-in phase matters. The app fits it from 4 phases (cos² fit of the error contrast) at the lock dither, because the PZT response lags the dither by tens of degrees.

## 2. What limits the lock
- **The FPGA is not the limit.** IN1 → `iq0` → `pid0` → OUT1 takes 12 clocks (96 ns) of logic and about 211 ns analog to analog. Source: the Verilog simulation in `fpga/redpitaya_latency_sim/`.
- **The lock-in low-pass is the limit.** It must sit below the dither frequency, and 2 × 304 Hz stages take 0.81 ms to reach 50 %. At 45° margin this allows about 100 Hz of loop bandwidth with a 1 kHz dither, and about 400 Hz with a 20 kHz dither and a 1 kHz driver.
- **The default loop bandwidth is 10 Hz**, which is safe but leaves vibration above 10 Hz uncorrected.
- The PZT driver and the PZT (first resonance) set the next limit. Their bandwidth is assumed to be 1 kHz; measure it with *Measure loop*.
- The Python app is never inside the loop. Its speed only affects the display.

## 3. Noise
- Every Red Pitaya block adds at most about 10⁻⁴ of the linewidth of detuning noise over 1 kHz. The blocks are the ADC, the digital mixer, the low-pass, the integer gain, the PI and the DAC. Commercial RF and lock-in parts would not improve this. See the table in `speed_latency_noise_study.md`.
- The DAC step (122 µV) can matter if the linewidth spans only a few mV of OUT1: one step is 2.4 % of a 5 mV linewidth.
  - The fix is a **10:1 divider** between OUT1 and the driver's MOD IN. The cost is the scan range.
- Likely sources of the post-lock intensity noise:
  1. vibration above the 10 Hz loop bandwidth;
  2. the dither itself, through the 2f transmission modulation. This is nearly 100 % at the 30 mVpp default if κ ≈ 6 mV of OUT1;
  3. laser intensity noise, which no lock removes.
- The plan:
  1. *Measure noise*, then *Measure loop*.
  2. Raise the loop bandwidth to about 45° margin.
  3. Shrink the lock dither to 0.2–0.3 κ.
  4. Raise the dither frequency in steps (2, 5, 10, 20 kHz) with the low-pass near dither/8.
  5. Long term: an EOM / PDH or tilt lock, for kHz–MHz bandwidth.

## 4. The app (v3) — design decisions that worked
- Acquisition mode at start-up: a triangle ramp (20 Hz, 500 mVpp) plus a dither (1 kHz, 30 mVpp).
  - The user clicks a transmission peak to target it.
  - "Auto-optimize phase" finds the demodulation phase.
  - All panels are visible at once, with toggles.
- LOCK!:
  1. stops the big ramp;
  2. switches to the lock dither (the same as the acquisition dither unless unticked);
  3. sweeps slowly across the chosen peak and re-fits the phase;
  4. finds the zero crossing and slope;
  5. parks OUT1 there and closes `pid0` with a gain ramp.
- A watchdog runs while locked. It declares an unlock when the transmission stays below 0.4 × the peak for 0.3 s; with auto-relock on, it re-sweeps around the last good point.
- Hardware snapping: corners and the integer gain are read back and displayed, so the screen equals the hardware.
- Speed:
  - The server tick costs about 4 ms; it was 170 ms. The browser redraws 7.6/s acquiring and 10/s locked.
  - The browser draws with client-side `Plotly.react` and SVG.
  - Scope settings are cached, with 5 register accesses per locked trace (down from 24). The locked live window is 33 ms.
- Diagnostics:
  - *Measure noise*: residual detuning and RIN spectra with band rms.
  - *Measure loop*: network analyzer giving G, UGF, phase margin and the plant P, overlaid with the model (integrator + low-pass + driver pole + 211 ns delay).

## 5. Open questions / to measure on hardware
- What dominates the residual noise: dither 2f, vibration or laser RIN?
- The actual linewidth in mV of OUT1. The app prints it after Auto-optimize phase; it decides whether the 10:1 divider is needed.
- The PZT driver model and its bandwidth with the PZT, and where the first PZT resonance sits.
- How high the dither frequency can go before the PZT response drops or resonates.
