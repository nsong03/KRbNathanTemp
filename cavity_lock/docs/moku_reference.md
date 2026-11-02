# Moku (Liquid Instruments) UI conventions to borrow

The goal: the cavity-lock app should present the Red Pitaya chain the way Moku's **Laser Lock Box** and **Lock-in Amplifier** do. That means a block diagram you can click, switches on the signal path, probe points that feed an embedded scope, and a lock assist.
The notes below are from the official user manuals (links at the end), read on 2026-10-31. Read the manuals yourself before designing; the PDFs have screenshots.

## Laser Lock Box: block diagram, in signal order
1. **Inputs.** Input 1 carries the error or reflected signal, Input 2 the monitor or transmitted signal.
   - Front end per input: AC/DC coupling, 50 Ω / 1 MΩ, attenuation.
   - Input gain 0 to +48 dB on Input 1.
2. **Demodulation.** The source is chosen with an advanced icon at the top right of the diagram. Options:
   - "Modulation": the same oscillator as the modulation output. **This is our case**: `iq0` demodulates with its own dither NCO.
   - "Internal": a separate local oscillator.
   - "External (Direct)", "External (PLL)", or "None", which bypasses the mixer.
   - A "Phase Shift" control sets the demodulation phase.
3. **Filter.**
   - Lowpass: order 2 or 4, with a choice of types: Butterworth, Chebyshev I/II, Elliptic, Cascaded, Bessel, Gaussian, Legendre.
   - Or Bandstop, or Custom (coefficients).
4. **Setpoint**: a DC offset added to the error after the filter. It moves the lock point.
5. **Fast PID controller.**
   - Proportional gain [dB], integrator crossover [Hz], differentiator crossover [Hz] and double-integrator crossover.
   - Integrator and differentiator saturation [dB], and overall gain.
   - Each PID block shows an interactive Bode plot of its own response.
6. **Slow PID controller**: runs at 1/64 of the rate and is cascaded on the fast controller's output.
7. **Scan generator.**
   - Enable switch, output channel, shape ("Positive Ramp", "Negative Ramp", "Triangle"), frequency, amplitude.
   - Default: 500 mVpp triangle at 10 Hz on Out 1.
8. **Modulation (aux sine)**: enable, output channel, frequency, amplitude. It is summed into Out 1 or Out 2.
9. **Outputs.**
   - A **switch** on each output path: open gives zero output, and re-closing clears the integrator.
   - Output offset, "Invert", and a voltage limiter with high and low limits.

## Laser Lock Box: interaction
- Tap a block to open its settings. Tap a switch to open or close it.
- Click a **probe point** on a wire to place a probe. The number of probes depends on the device, typically 2–4.
  - Probes feed the embedded **Oscilloscope** and **Data Logger**, opened from icons.
  - The manual's examples probe after demodulation (the error) and on Out 1.
- **Lock Assist**:
  - It turns the scan on, enables the output and places a probe after the setpoint.
  - The scope trigger syncs to the scan, and the scope shows **crosshairs at every error zero crossing**.
  - Pressing a crosshair sets the output offset to that voltage, closes the loop and runs the configured lock stages.
  - Zooming or scrolling the scope changes the scan amplitude and offset, not the time base.
- **Locking stages**: up to 3, disengaged in reverse order. Actions:
  - "Disable scan";
  - "Enable fast feedback", "Enable fast PID integrator", "Enable fast PID double integrator";
  - "Disable fast PID integrator saturation";
  - "Enable Slow feedback", "Enable slow PID integrator".
- Recommended workflow in the PDH quick start:
  1. Modulation on; demodulation from "Modulation".
  2. "View error signal" with Lock Assist.
  3. Vary the phase for the steepest slope; lower the low-pass for easier acquisition.
  4. Set a low fast gain with integrator saturation, then click the crosshair.
  5. Remove the saturation and tune.
  6. Add the slow loop.
  7. Log the error and actuator signals.

## Lock-in Amplifier: relevant conventions
- Demodulation source: "Internal" (frequency entry), "External", "External (PLL)" (with "Auto" and "Reacquire"), or "None".
- Dual-phase mixer, shown as "Rectangular (X/Y)" or "Polar (R/ϕ)". Probes can view I/Q **before** the low-pass.
- Lowpass slope 6/12/18/24 dB/oct, built from cascaded single poles. It can be set by "Time constant" or "Corner Frequency", with τ = 1/(2πf_c).
- Output conditioning: gain in dB, offset, invert, on/off switch.
- Output 2 can carry "Aux oscillator", "Demodulation" (the LO) or "Filtered signal".
- A PID can replace the gain block on one output.

## Moku API names (pymoku, Moku:Lab LaserLockBox) — a useful vocabulary
- Generators and demodulation: `set_scan(amplitude, frequency, phase, waveform='sawtooth'|'triangle', output)`, `set_aux_sine(amplitude, frequency, phase, sync_to_lo, output)`, `set_local_oscillator(frequency, phase, source='internal'|'external'|'external_pll')`.
- Controllers and filter: `set_pid_by_freq(pid_block, kp, i_xover, d_xover, si, sd, enable)` and `set_custom_filter(...)`.
- Outputs: `set_output_enables`, `set_output_range`, `set_offsets(position='pid_input'|'out1'|'out2', offset)`.
- Monitoring: `set_monitor(ch 'A'|'B', source='error'|'pid_fast'|'pid_slow'|'offset_fast'|'in1'|'in2'|'out1'|'out2'|'scan'|'lo'|'aux'|...)`.

## How each Moku block maps onto this Red Pitaya build
| Moku block | Red Pitaya / PyRPL here | Real limits to show in the UI |
| --- | --- | --- |
| Input 1 front end | IN1 jumper (LV ±1 V / HV ±20 V), set by hand | display-only; "IN1 scale" setting |
| (no Moku equivalent) input AC coupling | `iq0.acbandwidth` high-pass | 1st order, factor-2 ladder |
| Modulation oscillator | `iq0` NCO → `iq0.output_direct = out1` | frequency, amplitude (pp); on/off |
| Demodulation "Modulation" + Phase Shift | `iq0` mixer with its own NCO, `iq0.phase` | auto-optimize (4-phase fit) |
| Lowpass | `iq0.bandwidth` | **order 1 or 2 only**, 1st-order stages, factor-2 ladder (not Butterworth etc.) |
| (gain) | `iq0.quadrature_factor` | **integer** |
| Setpoint | `pid0.setpoint` | currently 0 |
| Fast PID | `pid0` P and I (no D in this build's use) | `i` step about 0.0046 Hz; I given as the loop UGF |
| Slow PID | none | — |
| Scan generator | `asg0` triangle (`ramp`) / sawtooth (`halframp`) | frequency, amplitude, center; OUT1 limit ±0.95 V |
| Output switch, limiter | `pid0.output_direct`, `pid0.min/max_voltage`, app `OUT_LIMIT` | clamp from the mode spacing |
| Probes A/B → scope | Red Pitaya scope `input1` / `input2` (2 channels) | only real DSP signals (see pyrpl_reference.md) |
| Lock Assist crosshairs | app: click a transmission peak → target; LOCK! zooms, re-fits the phase and engages at the zero crossing | — |
| Locking stages | app `_engage`: scan off → park → I at ugf/10 → ugf/3 → ugf | — |

## Sources
- [Moku Laser Lock Box user manual (all devices)](https://download.liquidinstruments.com/documentation/manual/instrument/common/UserManual-LaserLockBox.pdf)
- [Moku Lock-in Amplifier user manual](https://download.liquidinstruments.com/documentation/manual/instrument/common/UserManual-Lock-inAmplifier.pdf)
- [Moku:Lab Laser Lock Box manual (Laser 2000 mirror)](https://photonics.laser2000.co.uk/wp-content/uploads/2022/10/Laser-Lock-Box-Usermanual-MokuLab.pdf)
- [pymoku LaserLockBox API](https://pymoku.readthedocs.io/en/2.8.3/laserlockbox.html)
- [Moku Lock-in amplifier configuration guide](https://www.liquidinstruments.com/blog/2019/11/24/lock-in-amplifier-configuration-guide/)
- [Laser Lock Box product page](https://liquidinstruments.com/laser-lock-box/)
