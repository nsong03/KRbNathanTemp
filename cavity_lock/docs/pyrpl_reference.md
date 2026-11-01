# PyRPL / Red Pitaya FPGA reference (as used by the cavity lock)

PyRPL 0.9.9.1. The FPGA source is Verilog: <https://github.com/pyrpl-fpga/pyrpl> at tag 0.9.9.1, folder `pyrpl/fpga/rtl/`.
Every statement here was read from that source or from PyRPL's Python modules, or checked against the fake and the app on real hardware. Statements marked **(verify)** were not checked on hardware.

## The signal chain the app builds (component level)

```
                 ┌──────────────────────────── iq0 (red_pitaya_iq_block.v) ─────────────────────────────┐
 IN1 ─ ADC ─ reg │ high-pass (acbandwidth, 1 stage) ─▶ ×  mixer ─▶ LPF stage 1 ─▶ LPF stage 2 ─▶ × quadrature_factor │─▶ signal "iq0"
 14 bit 125 MS/s │                                      ▲  (cos/sin of NCO + phase)   (bandwidth list, ≤2)   (integer)   │   (error)
                 │   NCO 32-bit phase acc., 2^11-entry LUT, 17-bit ─┘                                                   │
                 │   NCO ─▶ × amplitude ─▶ iq0 output  ("dither", output_direct = out1)                                 │
                 └───────────────────────────────────────────────────────────────────────────────────────────────────┘
 "iq0" ─▶ pid0 (red_pitaya_pid_block.v):  error = input − setpoint ─▶ [inputfilter, unused] ─▶ P·e + ∫ I·e (ival)
          ─▶ clamp [min_voltage, max_voltage] ─▶ output_direct = out1
 asg0 (ramp / halframp) ─▶ output_direct = out1
 Σ (red_pitaya_dsp.v, 5-clock adder tree of every module with output_direct out1) ─▶ saturate ±1 V ─▶ DAC (AD9767) ─▶ OUT1
 pid1: input = iq0 / pid0 / asg0 / in1, P = out2_gain, I = 0 ─▶ out2   (OUT2 monitor)
```
Latency, from the Verilog simulation and datasheets:
- IN1 → `iq0` (filters off) → `pid0` → OUT1 takes 12 FPGA clocks = 96 ns.
- About 211 ns analog to analog in total.
- Each low-pass stage adds 2 clocks plus its time constant. The high-pass adds 1 clock.

## Modules and attributes the app uses
| Module | Attribute | Notes |
| --- | --- | --- |
| iq0 | `input` | `"in1"` |
| | `frequency` | NCO frequency, register = f/125e6·2^32 |
| | `amplitude` | dither amplitude [V], half of peak-to-peak. The app divides by a measured calibration `amp_cal` |
| | `phase` | demodulation phase [deg]. **The register is inverted** relative to the naive formula; always use the attribute |
| | `bandwidth` | list of low-pass corners [Hz]. **iq0 accepts at most 2** stages, each 1st order. Corners snap to a factor-2 ladder: shift = round(log2(α·2^MAXSHIFT)) + 0x80 |
| | `acbandwidth` | input high-pass corner [Hz], 1 stage (+0x40 flag in the shift register). 100 Hz → 75.9 Hz |
| | `quadrature_factor` | error gain, **integer** (1 = lowest useful) |
| | `gain` | 0. A non-zero value re-modulates the filtered signal onto the output (not used) |
| | `output_signal` | `"quadrature"` = the demodulated, filtered, scaled error that `pid0` and the scope see as `iq0`. Other options exist (`output_direct`, `pfd`, `quadrature_hf`, `off`) **(verify meanings before using)** |
| | `output_direct` | `"out1"`: the NCO dither goes onto OUT1; `"off"`: no dither |
| pid0 | `input` | `"iq0"` when locked |
| | `setpoint` | 0. Lock at the error zero crossing; a non-zero value moves the lock point |
| | `p` | normalized P. Register norm 4096 |
| | `i` | integrator unity-gain frequency [Hz]. Register norm 2^32·2π·8e-9, so the **step is about 0.0046 Hz** |
| | `ival` | integrator state = output voltage. Writing it parks OUT1. Register ×2^13 |
| | `min_voltage`, `max_voltage` | output clamp. The app sets ±0.4·(mode spacing) around the lock point |
| | `inputfilter` | `[0,0,0,0]` (off) |
| | `output_direct` | `"out1"` / `"off"` |
| pid1 | as above | OUT2 monitor: `p = out2_gain`, `i = 0` |
| asg0 | `setup(waveform, frequency, amplitude, offset, trigger_source, output_direct)` | `"ramp"` = **triangle**, `"halframp"` = sawtooth. The ramp is the scope trigger (`trigger_source="asg0"` on the scope) |
| scope | `input1`, `input2`, `duration`, `average`, `rolling_mode`, `trigger_source`, `trigger_delay`, `durations` | 2 channels, 16384 points per trace. Sources are any DSP signal name: `in1`, `in2`, `out1`, `out2`, `iq0`–`iq2`, `iq2_2`, `pid0`–`pid2`, `asg0`, `asg1`, `iir`, `trig` **(verify with `r.scope.inputs`)** |
| scope (0.9.9.1) | `_start_trace_acquisition()`, `curve_ready()`, `_get_trace()` | 0.9.3 uses `_start_acquisition()` / `_get_curve()`. `single()` is the slow fallback |
| networkanalyzer | `p.networkanalyzer.setup(start_freq, stop_freq, points, rbw, average_per_point, amplitude, input, output_direct, acbandwidth, logscale, auto_bandwidth, q_factor_min, auto_amplitude)`, `single()`, `frequencies` | uses a free IQ module. Reserve yours with `.owner` first |
| iir | not used | available for a true band-pass or notch on IN1: route in1 → iir → iq0.input. It adds latency **(verify)** |

Unused modules (`asg1`, `pid2`, `iq1`, `iq2`) get `output_direct = "off"` at start-up so that nothing else writes to OUT1.

## Signals that can and cannot be probed
The scope sees only DSP signal names. With the app's routing:

Can probe:
- `in1`: the photodiode.
- `iq0`: the error after high-pass, mixer, low-pass and gain.
- `asg0`: the ramp.
- `pid0`: the PID output (= setpoint while locked).
- `out1`: the total drive = ramp + dither + PID.
- `out2`, and `in2`.

Cannot probe without re-routing, which disturbs the lock:
- the signal between the high-pass and the mixer;
- the raw mixer output before the low-pass (possibly via `output_signal="quadrature_hf"` **(verify)**);
- the bare dither NCO. It is visible on `out1` only when the ramp and PID are off; the app measures its amplitude from `out1`.

## Practical limits
- The PyRPL `i` resolution (0.0046 Hz) sets the lowest usable integrator gain. If `-ugf/S` rounds to 0, the error slope is too large: reduce the error gain or the dither.
- The lock-in low-pass must sit below the dither frequency, so the dither frequency limits the loop bandwidth:
  - 1 kHz dither with 2 × 304 Hz low-pass and a 1 kHz driver: about 100 Hz at 45° margin;
  - 20 kHz dither with 2 × 2.4 kHz low-pass: about 400 Hz with a 1 kHz driver.
- Register access is a LAN round trip (about 0.25 ms). Cache scope settings and avoid reading back registers in fast loops.
