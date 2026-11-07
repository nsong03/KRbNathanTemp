# Cavity PZT dither lock + AOM intensity servo (Red Pitaya + PyRPL) — project memory

Owner: Nathan Song (KRb lab). Windows 10/11 laptop, VS Code, Python environment where `import pyrpl` works.
Working folder on the laptop: `C:\Users\songo\Desktop\P-Physics\KRbGeneral\redpitaya_locking\lockcavityPZTtolaser`.

## What this is
A browser app (`app/cavity_lock_app.py`, Dash + Plotly) that locks the length of an optical cavity to a laser.
It dithers the cavity PZT, demodulates the transmitted intensity with the Red Pitaya's own lock-in (PyRPL `iq0`),
and integrates the error with `pid0`. The user confirmed that v2 **locks and is robust** on the real hardware. v3 added speed + diagnostics;
v4 replaces the settings sidebar with a Moku-style **schematic** of the real signal chain (`docs/schematic_plan.md`).
v5 (current, 2026-11-02) adds a second feature on IN2/OUT2: **AOM intensity stabilization** (`pid2` + `asg1`), with two
icons at the top left that switch between a "Cavity lock" and an "Intensity" view (`docs/intensity_plan.md`,
hardware: `docs/intensity_hardware.md`, not built yet). v3–v5 have been tested only against the fake so far.

## Hardware
- Red Pitaya STEMlab 125-14, hostname `rp-f0fa92.local`. PyRPL 0.9.9.1. Its FPGA bitstream matches the GitHub `pyrpl-fpga/pyrpl` Verilog at tag 0.9.9.1.
- IN1 ← cavity transmission photodiode. Use the LV jumper below 1 V; with HV, set the app's "IN1 scale" to 20.
- OUT1 → PZT driver MOD IN. OUT1 carries the ramp plus the dither, or the setpoint plus the dither. The app never commands beyond ±0.95 V.
- IN2 ← AOM pickoff photodiode (1st-order beam, LV jumper, about 0.5–0.8 V at the operating point).
- OUT2 → 50 Ω tee → inverting bias driver (V_drv = |V_bias| − 10·V_OUT2) → Schottky + 100 Ω → ZX05-1+ mixer IF
  (current-controlled attenuator; LO = 80 MHz, +4 dBm) → ZHL-1-2W+ (32 dB) → AOM. More OUT2 = less light (K < 0);
  OUT2 = 0 V = bias level (safe default). Useful OUT2 range only about ±0.1–0.2 V. The old OUT2 monitor is gone.
- PZT driver: model unknown. Its bandwidth with the PZT is assumed to be 1 kHz (setting `driver_bw_hz`).
- Cavity linewidth κ ≈ 40 MHz (setting `kappa_mhz`). It is used only to convert volts to Hz.
- Earlier setups used a Rigol DG922 (dither and reference) and an SR810 lock-in. Both are now replaced by the Red Pitaya; see `earlier/`.

## Run
```
pip install "dash>=2.9" scipy
python app\cavity_lock_app.py --host rp-f0fa92.local        # opens http://127.0.0.1:8050
```
- `--port`, `--config` (PyRPL config name, default `cavity_lock_app`) and `--no-browser` are also available.
- Settings are saved to `cavity_lock_settings.json` next to the app with the Save and Load buttons. Keep the keys backward compatible: the user has saved files.
- Ctrl-C quits. The Red Pitaya keeps its state, so a running lock stays on.

## Second app: error signal + PZT summer (`app/errsig_app.py`, 2026-11-04)
A separate, smaller web app for an EXTERNAL servo box. No PID feedback runs on the Red Pitaya.
- Wiring: IN1 ← photodiode; **OUT1 → servo box** (error = `eo_gain` × iq0 + `eo_offset_mv`, clamp `eo_limit_mv`);
  **IN2 ← servo box correction** (HV jumper for its ±15 V: ±20 V range, 30 V absolute max; LV is ±1 V, 6 V absolute
  max → limiter); **OUT2 → PZT driver** = `in2_gain` × IN2 + DC offset (`ramp_center_mv`) + dither.
  "Ramp (align)" toggle: OUT2 = ramp + dither, the IN2 correction off.
- Modules: iq0 (dither → out2, demodulates in1), pid1 (pass-through iq0 → out1), pid0 (pass-through in2 → out2, only
  in servo mode), asg0 (ramp or DC offset → out2); pid0/pid1 have I = 0 always (the FPGA cannot route iq0 or in2 to a
  DAC by itself). pid2, asg1, iq1, iq2 off. On HV the FPGA sees IN2/20: gain 1 maps ±15 V to ±0.75 V on OUT2;
  `in2_scale` (default 20) is display only.
- Run: `python app\errsig_app.py --host rp-f0fa92.local` → http://127.0.0.1:8060 (settings `errsig_settings.json`).
  Only one app may own the Red Pitaya at a time.
- Files: `errsig_app.py` (`ErrsigController`, reuses the analysis helpers of `cavity_lock_app.py`),
  `errsig_schematic.py` (model), `errsig_ui.py` (Dash; reuses `ui.CSS`, `ui.SCOPE_JS`, `ui.DIAG_JS`, `ui.POP_JS`).
- Modes `acquire` (Ramp on), `servo` (Ramp off: correction + DC + dither), `busy` (auto phase), `off` (Output off,
  OUT2 = 0 V). "Park at peak" puts the DC offset on the target peak's error zero crossing and enters servo mode; the
  error slope there is shown (V of OUT1 per V of OUT2) for the servo box gain and sign. Setpoint ±1/±10 mV steps move
  the ramp centre / DC offset. Auto phase, Lock Assist targets, dither calibration (on OUT2 while ramping).
- Tests: `test/test_errsig.py` (23 checks incl. an end-to-end lock through a simulated servo box: OUT1 → integrator →
  IN2 → OUT2); `serve_errsig.py` (port 8061) + `ui_errsig.py` (Playwright, 17 checks). Both set `FAKE_PZT_OUT=out2`.
- Fake: `eng.pzt_out = "out2"` (env `FAKE_PZT_OUT`) makes OUT2 drive the PZT; `eng.ext_on/ext_i/ext_v/ext_lim` model the
  servo box (integrates OUT1 into IN2). An ASG with amplitude 0 outputs its offset (like the hardware).

## Spin-off: 2-phase lock-in (`app/errsig2ph_app.py`, 2026-11-04)
A copy of the errsig app (files `errsig2ph_app.py` / `errsig2ph_schematic.py` / `errsig2ph_ui.py`, independent of
`errsig_*`) with a dual-quadrature lock-in. Same wiring (IN1 PD, OUT1 error, IN2 correction, OUT2 summer/ramp).
- Lock-in on **iq2**: in 0.9.9.1 only DSP module 7 (`iq_2_outputs` in red_pitaya_dsp.v, QUADRATUREFILTERSTAGES 4)
  wires `signal2_o`, so X = `iq2` (output_signal quadrature) and Y = `iq2_2` (DSP index 14). Both share the low-pass
  register 0x124 (1–4 stages here, `lpf_order` 1–4), the high-pass and `quadrature_factor` (g3 scales both). The
  PyRPL network analyzer / spectrum analyzer also use iq2: they cannot run while this app owns it. iq0 output off.
- Modules: iq2 (dither → out2, demodulation), pid1 (X or Y → out1, setting `eo_source` "X"/"Y"), pid0 (in2 → out2,
  servo mode), asg0 (ramp / DC). iq0, iq1, asg1 outputs off; pid2 off.
- Sweeps: two triggered captures per frame, (in1, iq2) and (iq2, iq2_2) (X and Y from the same instant). Live: the
  same two, untriggered. `xy_theta(x, y)`: principal axis of the centred X–Y scatter (θ in −90..90°) and quality q
  (variance ratio along/across; shown only for q ≥ 3). Sweep θ uses the target-peak window (`_half_window`).
- Auto phase: θ from one sweep → `iq2.phase = phase ± θ`; the rotation direction is found on the first run (if the
  residual is not < θ/2 the other sign is used) and kept in `ctl._phase_sign` (runtime). Then +180° if the X slope
  is positive. No clear axis (q < 3) → the 4-phase fit of errsig (logged).
- Servo mode: live θ from the residual fluctuations (meaningful only when cavity-frequency noise dominates the
  isotropic detection noise; on the fake q ≈ 1–2, "no clear axis").
- Run: `python app\errsig2ph_app.py --host rp-f0fa92.local` → http://127.0.0.1:8065 (settings `errsig2ph_settings.json`).
- Fake: env `FAKE_LOCKIN=iq2` → the engine demodulates with iq2: X = A cos(phase − lag), Y = A sin(phase − lag), own
  filter state for Y, hist tuples 8 long (`ey` last), iq2 takes 4 filter stages (`IQ._stages`). Default `iq0`
  leaves the other apps' fake unchanged.
- Tests: `test/test_errsig2ph.py` (37 checks: θ from one sweep within 3° of the fake at two phases, Auto → |θ| < 3°
  with a negative slope, 4 filter stages, X/Y output source, the errsig checks), `serve_errsig2ph.py` (port 8066) +
  `ui_errsig2ph.py` (23 checks). Screenshots `docs/screenshots/errsig2ph_*.png`.

## Third app: plain PI servo (`app/pi_app.py`, 2026-11-06)
User request: accept an error signal, output the correction through a PI, **nothing fancy**: raw P and I set by hand,
filters optional and off by default, a ramp toggle for acquisition, **IN1 and OUT1 only**.
- `PIController` (+ `pi_ui.py`, its own small CSS/clientside scope; no schematic). pid0: input in1, p, i (Hz),
  setpoint, min/max clamp, inputfilter (keys `filt1_hz`..`filt4_hz`, 0 = off) → out1. asg0 → out1: ramp (Ramp on)
  or DC offset `offset_mv` (Ramp off). OUT1 = offset + PI. p/i/filter snapped to the FPGA values and pushed back.
- Modes `ramp` (PI off, ival 0), `dc` (offset only), `pi`, `off` (OUT1 = 0 V). Toggles Output / Ramp / PI with
  interlocks (Ramp on turns PI off and vice versa; nothing but Output works while Output is off). Reset integrator.
  The gains are written whenever changed; PI on resets ival to 0 and routes pid0 to out1.
- Start-up: pid0 running in1 → out1 with non-zero p or i → adopted untouched (settings read back). Else ramp.
  Other modules routed to out1/both are switched off (logged); OUT2 is never touched.
- Scope: ramp = one period triggered on asg0, (in1, out1), x axis time or OUT1 (`xaxis`); else untriggered
  `live_ms`. Stats: IN1 mean/rms, OUT1 mean, integrator, warning when ival is within 1 % of a clamp.
- Run: `python app\pi_app.py --host rp-f0fa92.local` → http://127.0.0.1:8075 (settings `pi_settings.json`).
- Fake: env `FAKE_PLANT=errin` → `Engine.step_errin` / `traces_errin`: IN1 = dispersive cavity error
  (`ext_err`, E_PK 0.4 V, slope −140 V/V), pid0 a real P + I with its input filter, OUT1 = asg0 + pid0.
- Tests: `test/test_pi.py` (28: ramp acquisition shows the resonance, snapping, lock, drift, setpoint, filters,
  clamps, wrong sign → clamp, interlocks, write log, restart adoption), `serve_pi.py` (port 8076) + `ui_pi.py` (15).

## Fourth app: dual-phase lock-in PI + intensity PI (`app/lockin_pi_app.py`, 2026-11-06)
User request: IN1/OUT1 = dual-phase lock-in + PI + ramp; intensity servo on OUT2; **raw P and I only, no
auto-calculation or auto-setting**; optional toggleable filters (off by default). The user wrote "input 1 and output
2" for the intensity servo; the documented hardware has the AOM pickoff on IN2, so `int_input` defaults to in2 and
in1 is selectable.
- **User rules for this app (2026-11-06): the schematic diagram with switches (like errsig / the cavity app), NOT a
  settings sidebar. P and I keys are `p`/`i` and `int_p`/`int_i`: PyRPL's own `pid.p` / `pid.i`, passed through
  unchanged. "Do NOT change the I to Hz ... don't autoconvert": no labels in Hz, and typed values are NEVER rewritten
  with the FPGA-rounded readback (no snapping push-back in this app, for any key); the rounded hardware values
  appear only in the diagram's readback text.** Old settings files with `i_hz` / `int_i_hz` load via `LEGACY`.
- **User rules (2026-11-06, second round): UI laid out like the cavity app (view icons "Lock" / "Intensity" with
  status dots, one schematic per view); EVERY parameter inline in its box and EVERY scope setting in the scope panel
  ("don't hide things behind layers": popovers only carry notes); the user sets all scope settings (channels,
  timebase, trigger, level, hysteresis, delay, averaging, rolling, ranges); the traces must be live.**
- Why the previous version was choppy (diagnosed 2026-11-06): 3 scope captures per frame ((in1, out1), (iq2, iq2_2),
  (in2, out2)), each a full trace + ~2×16384-sample readout, trigger source switching (PyRPL `call_setup`) between
  them; in ramp mode 2 triggered 268 ms captures → ~0.7 s per update on the hardware. Now: ONE capture per frame.
- Files: `lockin_pi_app.py` (`LockinPIController`, reuses `pi_app` `_scope_set`), `lockin_pi_schematic.py` (two models
  `LOCK` (1340×500, prefix "") and `INT` (1340×260, prefix "i-"), `SCOPE_KEYS`, `diagram_state(snap, sw)` → (lock,
  intensity)), `lockin_pi_ui.py` (ui.CSS + ui.VIEW_CLICK_JS / VIEW_SHOW_JS / DIAG_JS / POP_JS, ids `view-cavity` /
  `view-intensity`, `vbtn-cav` / `vbtn-int`, `dot-cav` / `dot-int`; its own 2-channel SCOPE_JS).
- Scope (`_tick`): settings `sc_ch1`, `sc_ch2` (any of in1, in2, iq2, iq2_2, pid0, pid2, asg0, asg1, out1, out2),
  `sc_duration` (PyRPL `durations`, 131 µs × 2^k), `sc_trigger` (immediately, asg0, asg1, ch1/ch2/ext edges),
  `sc_threshold_mv`, `sc_hysteresis_mv`, `sc_delay_ms` (PyRPL trigger_delay = trace centre after the trigger;
  default duration/2 = trace starts at the trigger), `sc_average`, `sc_rolling`; display only: `sc_view` (time / XY),
  `sc_math` (R), `sc_ch{1,2}_{min,max}_mv` (None = auto), `ui_hz`. Registers written only on change (`_scope_set`);
  the capture is armed and polled without blocking (≤ 50 ms per loop pass, commands served in between); no trigger
  for dur + 0.25 s → `ctl.waiting` ("WAITING FOR TRIGGER"). Rolling (trigger immediately, duration > 0.1 s):
  `_start_acquisition_rolling_mode()` once, then `_get_rolling_curve()` each frame at `ui_hz`. RUN/STOPPED button
  (`scope_run` command). Probe dots put a signal on CH1 / CH2 alternately (badges "1" / "2"). With iq2 and iq2_2 on
  the two channels the Phase box shows the X–Y signal angle (display only).
- Modules: iq2 (dither → out1, demodulates in1: X = iq2, Y = iq2_2, LP 1–4 stages, HP, integer gain, phase by hand),
  pid0 (input iq2 or iq2_2 = `pi_input` X/Y → out1), asg0 (ramp / offset → out1), pid2 (int_input → out2). iq0, iq1,
  pid1, asg1 off.
- Switches: Filter, PI, Dither, Ramp, Output (lock); Filter, Servo (intensity). Filters: `filt_on` / `int_filt_on`
  bools in settings (not in `KEYS`); stage values are written only while the switch is on (else [0, 0, 0, 0]).
- Lock modes ramp / dc / pi / off; intensity `int_on`. Start-up: each PI adopted untouched if running with gains,
  else lock → ramp, intensity → off (OUT2 = 0 V).
- Run: `python app\lockin_pi_app.py --host rp-f0fa92.local` → http://127.0.0.1:8085 (`lockin_pi_settings.json`).
- Fake: with `FAKE_LOCKIN=iq2` and the PZT on out1, pid0 is a full P + I on X or Y (`eng.p_out`); the AOM on OUT2.
  Scope: `traces(..., delay=)` honours trigger_delay for asg triggers; ch1/ch2 edge triggers (level + hysteresis)
  cut the window around the first crossing; ext triggers never fire; `_start_acquisition_rolling_mode` /
  `_get_rolling_curve` (the last `duration`, NaN before the start).
- Tests: `test/test_lockin_pi.py` (44: scope defaults, asg0 trigger at t = 0, writes only on change, > 8 traces/s
  free running, rolling at the browser rate, edge trigger at the level, waiting + commands served, Stop, probe →
  channel, phase by hand, lock point from triggered traces, raw gains, typed values never rewritten, filters, X/Y,
  intensity, restart adoption, write log), `serve_lockin_pi.py` (port 8086) + `ui_lockin_pi.py` (29: views/dots,
  every parameter and scope setting visible, scope dropdowns, ranges, XY, R, rolling, waiting, RUN/STOP, probes).

## Fourth app, v2: `app/lockin_pi2_app.py` (2026-11-06)
User: "make a v2 of this app with the scope settings set back to what they were before, it's actually a bit
convenient. keep it to two periods on the display." v1 (`lockin_pi_app.py`, the user-configured 2-channel scope)
stays as it is.
- Files: `lockin_pi2_app.py` (from lockin_pi_app at commit 7763884: automatic captures (in1, out1) + (iq2, iq2_2),
  triggered on asg0 while ramping with `ramp_periods` (default **2**), live `live_ms` otherwise; intensity
  (int_input, out2) `int_live_ms`; probes A / B with `probe_every`; xaxis time / OUT1) + `lockin_pi2_ui.py` (v1's
  layout: view icons, both schematics from `lockin_pi_schematic.py`, every parameter inline; the scope settings in
  visible bars under each view, no popover; ROWS_JS row scope). Settings `lockin_pi2_settings.json`, port 8095,
  PyRPL config `lockin_pi2_app`.
- View scheduling like the cavity app: `ctl.view` (set by the UI tick) → only the visible view's captures every frame,
  the hidden one once per second (`_lock_tick` / `_int_tick`, `seq` / `iseq`). Rate on the fake: lock 3.0 traces/s
  while ramping (2 periods at 20 Hz: two triggered 134 ms captures, each waiting for the next ramp start), intensity
  ~33/s. `lockin_pi_schematic.diagram_state(snap, sw, ("probe_a", "probe_b"), ("A", "B"))` for the A / B badges;
  `ownership_problems(keys, scope_keys)`.
- Same controller rules as v1: raw p / i never rewritten, filters behind switches, adoption at start-up, intensity off.
- **OUT1 summer + PI routing (v2, 2026-11-06, user):** "the dither is sent straight to the summer before output 1, and
  then we can toggle on either a ramp or the PI output to the summer. I like ... the PI active without sending it to the
  summer and send it only to output 2, as this lets me tune the PI." Summer = dither (Dither switch) + asg0 (ramp when
  Ramp is on, else the DC offset) + PI (PI switch = mode `pi`). Ramp and PI → summer are mutually exclusive (modes
  ramp / dc / pi). PI → OUT2 (`pi_out2`, bool, saved) is independent: `_pi_route()` → pid0.output_direct = both /
  out1 / out2 / off; `_route_pid()` writes it (integrator from 0 when the PI goes from no output to an output, cleared
  when it has none, kept when it only changes destination). PI → OUT2 switches the intensity servo off and the Servo
  switch is refused while it is on; it works with Output off too. Start-up adopts pid0 on out1 / out2 / both (mode from
  asg0 when pid0 is not on out1). Diagram `LS.LOCK2` (+ block `pi_out2_term`, wire `pid_out2`, switch `pi_out2`).
  Status "OUT2: LOCK PI (PI → OUT2)". Note told to the user: a 1 mVpp dither is ~8 DAC steps (2 V / 2^14 = 0.12 mV),
  so on a scope it is lost in the output noise; and the lock-in low-pass must be well below the dither (607 Hz LP with a
  500 Hz dither leaves a large 2f ripple on X / Y).
- **P / I boxes = PyRPL's PID widget (both lock-in apps, 2026-11-06, user: "exactly like the PI module in the pyrpl
  ... I just want you to implement the pyrpl modules in a good interface. please stop changing things around"):**
  `pyrpl_spinbox.py` copies widgets/spinbox.py NumberSpinBox with set_log_increment (pid_widget.py): ↓ [%.4e] ↑, click /
  Up-Down key = one register step (p 2^-12, i 1/(2^32·2π·8 ns) = 0.004632), stop at 0 when crossing zero, held > 0.1 s
  → ×2 per 0.5 s (halflife; Page Up/Down change it), 20 ms refresh, full precision cached (text shows %.4e), saturated
  to ±2048 / ±38.9 kHz. Browser → Store("pyspin") every 50 ms → controller `spin` command (s[key] = value, write the
  PI; no version bump, no log). Typed text + Enter goes through the normal apply; `PS.parse` keeps the full value
  when the text still reads as the present value.
- Tests: `test/test_lockin_pi2.py` (55, incl. PI routing and the pure-sine OUT1 check), `serve_lockin_pi2.py` (port 8096) + `ui_lockin_pi2.py`
  (36, incl. the P / I arrows: one step, hold ×2 per 0.5 s, zero crossing, keys, large-I single step; PI routing).

## Fifth app: external-reference lock-in PI (`app/extref_app.py`, 2026-11-07)
User: IN1 photodiode, IN2 = an external sine (5 Vpp) = the dither reference, two-phase lock-in for the error signal,
error toggleable on OUT2, PI on the error (raw P / I, limiters), alignment ramp on OUT1; NO dither from the Red Pitaya
(the external dither is summed with OUT1 outside). Keep the module design and displays of lockin_pi2_app.
- **FPGA fact (PyRPL 0.9.9.1 source): an IQ module demodulates only against its own NCO; IN1 x IN2 is impossible.**
  So: iq2 demodulates IN1 (X = iq2, Y = iq2_2) with its NCO at `ref_hz`, amplitude 0, output off. iq0 / iq1
  (input in2, phases corr / corr + 90, outputs off, LP `ref_lpf_hz` x2) measure the reference phase vs the same NCO
  (all three synchronized with `iq.synchronize_iqs()`). Software tracking (`_ref_service`, every 50 ms, also inside
  scope waits): read `r.sampler.iq0 / iq1` (fallback `current_output_signal`), θ = atan2; hold θ at its first value by
  shifting the phase register of iq0 / iq1 / iq2 by `corr` (gain 0.8 per step); the direction (`_sign`) is found once
  by stepping iq0 / iq1 only by +30° (never iq2: a running lock is not disturbed); FLL `_fll`: every 3 s, drift
  rate r > 5°/s → NCO += r / 360 Hz (direction `_fsign` flipped if it got worse). REF_MIN 2 mV demod level → "NO
  REFERENCE". "Find f": scope trace of IN2 (≥ 200 periods, ≥ 8 points / period) → `measure_freq` (FFT peak +
  half-trace phase advance) → ref_hz, NCO, sync. Start-up measures IN2 and only LOGS a mismatch (no auto change).
  Track switch (`ref_track`, saved). IN2 must be on the HV jumper (5 Vpp clips on LV); `in2_scale` 20 is display only.
- Modules: iq0, iq1, iq2, pid0 (PI on X / Y; PI → OUT1 summer exclusive with the ramp; PI → OUT2), pid1 (error →
  OUT2: gain x X / Y + offset, clamp; I = 0; switch `eo_on`), asg0 (ramp / offset → out1). PI → OUT2 and Error → OUT2
  are exclusive. pid2 / asg1 off. Single view (no intensity: IN2 / OUT2 are taken).
- Files: `extref_app.py` (`ExtRefController`, own `capture` with the tracking in its waits), `extref_schematic.py`
  (`LOCK` 1340×570), `extref_ui.py` (from lockin_pi2_ui; Find f button in the Reference box; status OUT1 / OUT2 /
  REF). Port 8105, settings `extref_settings.json`, PyRPL config `extref_app`.
- Fake: `FAKE_EXTREF=1` (+ `FAKE_LOCKIN=iq2`): external dither f_ext 1000.0137 Hz (`FAKE_F_EXT`), 30 mVpp at the
  PZT, 0.25 Vpp at IN2; shared NCO phase model (`nco_phase`, `sync_ncos`), θ_ext; X / Y = cos / sin(phase − lag −
  θ_ext) × LP attenuation at the beat; `ref_demod` for iq0 / iq1 (sampler, current_output_signal); in2 / iq0 / iq1 /
  pid1 / out2 scope traces; OUT1 carries no dither.
- Tests: `test/test_extref.py` (30: start-up, tracking locked with the 4.9°/s drift, X-Y angle fixed with tracking /
  rotating without, Find f (frequency, 5.02 Vpp), 0.3 Hz pulled in by the FLL, phase by hand, lock with the external
  dither, no dither on OUT1, error → OUT2 maths, exclusivity, restart adoption with iq2 phase moved < 5° only by the
  tracking, write log), `serve_extref.py` (port 8106) + `ui_extref.py` (22).

## FPGA module allocation (keep it; enforced by `test/test_isolation.py`)
| Module | Owner | Use |
| --- | --- | --- |
| iq0, asg0, pid0, OUT1 | cavity | dither + demodulation, ramp (and the cavity Bode test sine), lock |
| iq1, pid1 | cavity, measurement only | copies for the circuit-only Bode, **outputs off**, restored to off after |
| pid2, asg1 | intensity | servo (input in2, output_direct out2), Bode test sine on out2 |
| pid1 (otherwise) | nobody | off. Keep it free: it is pid0's only partner in PyRPL's differential mode |
| iq2 | network analyzer | free (the cavity "Measure loop" NA takes it) |
| scope | shared | scheduled (see "Scope schedule") |
- Cavity code (`cavity_lock_app.py`, `schematic.py`) never writes pid2 or asg1 and never sets any `output_direct` to
  `out2`/`both`. Intensity code (`intensity.py`, `intensity_schematic.py`) never writes iq0, asg0, pid0 and never routes
  to `out1`/`both`. Start-up sets pid1 off (an old v4 OUT2 monitor would add onto the servo).
- Old settings files with `out2_signal` / `out2_gain` load fine (keys not in `DEFAULTS` are ignored).

## Architecture (keep it)
- Files: `app/cavity_lock_app.py` (analysis, `Controller`, `main`), `app/schematic.py` (the cavity diagram as data:
  blocks, parameters → FIELDS key → PyRPL attribute, block writers, wires, switches, probes, readback text),
  `app/intensity.py` (`I_FIELDS`, `plant_gain`, loop model, `Intensity` servo controller, run by the Controller),
  `app/intensity_schematic.py` (the intensity diagram, same structure, element ids prefixed `i-`),
  `app/ui.py` (Dash layout and callbacks for both views, `build_app(ctl, A)`; `cavity_lock_app.build_app(ctl)` wraps it).
- **One model, two consumers.** `configure()` = `SCH.write_static(ctl)` (+ snapping); an apply runs only the writers of
  the blocks that own the changed keys (`SCH.apply_changed`). Ramp, PID and limiter writers are live only in
  `acquire` / `locked`. The UI draws the same `BLOCKS`/`WIRES`/`SWITCHES`/`PROBES`.
- `ctl.hw` = register readback (`SCH.read_hw`, ~22 reads) after every command and lock-sequence step, never in the
  fast tick. `ctl.req` = requested values of keys the FPGA rounds, shown as "250 → 303.6 Hz".
- Switch states are derived (`SCH.switch_states(mode, hw, s)`), never stored. Commands: `("switch", (name, on))`
  with interlocks in `Controller._switch`; `("probe", signal)`; `("target_v", volts)` (Lock Assist crosshair).
  `ctl.dither_on` is the Dither switch (runtime, not saved). Mode `off` = Output switch off (OUT1 = 0 V).
- **The `Controller` owns PyRPL and runs in the main thread** (`ctl.run(stop)`); Dash runs in a daemon thread.
  - All hardware access goes through `ctl.submit(cmd, arg)` → queue → `_do()`.
  - PyRPL is not thread-safe, and every register access is a LAN round trip of about 0.25 ms.
- Settings live in `FIELDS` as `(key, label, default, group, kind, options)`, in UI units (mV, Hz, deg).
  - `ctl.version` increments when the controller changes a setting itself, for example on hardware snapping or a phase fit.
  - The UI pushes the new values only then, so the UI and the controller never fight.
  - Applies are version-checked: `("apply", (values, version))`.
- `configure()` writes `iq0`, `pid0.setpoint` and pid0's input filter.
  - `_snap_to_hardware()` reads back what the FPGA really uses (filter corners on a factor-2 ladder, integer `quadrature_factor`) and shows those values in the UI.
- Modes: `connecting` → `acquire` ↔ `locking` → `locked` / `held`, plus `measuring` and `off` (Output switch).
  - `_lock()` → `_zoom_and_lock()`: a slow sweep across the chosen peak with the lock dither, then a 4-phase cos² fit of the phase, then the zero crossing and slope.
  - `_engage()`: parks at the zero crossing, then ramps the gain in thirds (ugf/10, ugf/3, ugf).
  - `_live_tick()`: the watchdog. `_relock()`: automatic relock.
- Loop gains: `pid0.i = -ugf/S` and `pid0.p = -p_gain/S`, where S is the measured error slope per OUT1 volt. Then |i·S| equals the loop bandwidth, and the minus sign gives negative feedback whatever the sign of S.
- Diagnostics while locked:
  - `_measure_noise()`: Welch spectra in two bands, with band rms.
  - `_measure_loop()`: PyRPL network analyzer on `pid0` and `iq0`. With T = c/x, G = −T/(1+T) and P = (e/x)/(1+T).
- **Measure Bode plot** (`_measure_bode`, toolbar button + mode selector + Stop): swept sine from `asg0` recorded with
  the scope (2 channels: asg0 and the response), complex amplitude by least squares at the exact frequency
  (`fit_sine`: cos, sin, offset, drift). Filled in point by point (`ctl.bode`, `bode_seq` → `dcc.Store("bode")` →
  clientside `BODE_JS`); Stop is a `threading.Event` set from the UI thread (the sweep blocks the command queue).
  - `closed loop` (locked only; asg0 is idle then): asg0 → OUT1 sum, amplitude = `na_amp_frac` × linewidth. Captures (asg0, pid0) and
    (asg0, iq0) per point: G = −T/(1+T), S = 1+T, closed loop = −T, plant = (e/x)/S. Stops and holds if the PID
    output reaches the clamp. At low frequency G comes from 1 + c/x (the loop cancels the sine), so it needs enough
    excitation: 0.03 linewidth gave ±20 % at 4 Hz on the fake, 0.1 gave ±3 %.
  - `circuit only` (OUT1 untouched; the asg0 sine stays off any output): spare copies `iq1` (oscillator at 0 Hz → the
    lock-in chain at baseband: same low-pass and integer gain, no high-pass) → `pid1` (i = UGF, p = P gain, i.e. the
    lock's gains per unit slope), both outputs off. Per-point amplitude keeps the copies' outputs ≈ 0.25 V. While
    scanning, OUT1 is parked at the ramp centre for the sweep and the scan restarts afterwards.
  - Stop frequency is limited to 0.45 × dither frequency.
- **Ramp / scan setpoint** (cavity): the ramp centre `ramp_center_mv` (key unchanged, label "Ramp setpoint (centre)")
  is an inline knob on the Ramp / scan box with ±1/±10 mV buttons (`ramp_nudge`, typed value via `RAMP_TYPED_JS` →
  `ramp_center`, clipped to ±0.95 V). Scanning: moves asg0.offset. After Scan off (`ctl._parked_at_ramp`): moves the
  parked OUT1 level (pid0.ival). Never while locked or held after an unlock. Test: `test/test_ramp_setpoint.py`.
- **Intensity servo** (`intensity.py`, same architecture: main thread, version-checked settings `int_*`, snapped values):
  - **No scan** (user decision 2026-11-02): the only intensity measurement is the swept-sine Bode plot.
  - Modes `off` → `locking` → `locked`, plus `held` (Output on, loop open), `dc`, `measuring`, faults `railed`, `nolight`.
  - **DC output (bypass)** for calibration: block `i_dc` + switch "DC out" (`it.dc_on`, runtime). On: loop open
    (pid2 p = i = 0), clamps opened to ±`int_out2_limit_mv`, pid2.ival = `int_dc_mv` → OUT2 (`set_dc`, writer `w_dc`
    live in mode `dc`). Steps ±0.1/1/10 mV (`int_dc_nudge`), typed value via `DC_TYPED_JS` → `int_dc`, clipped to the
    OUT2 limit (the hand clamps do not apply). Turning it on while locked unlocks first; off → `held` at the level;
    Output off ends it. Measure Bode plot in `dc` = open-loop plant around the level (mode back to `dc` after);
    Lock from `dc` starts at the level. No watchdog in `dc`. Test: `test/test_dc_output.py`.
  - Measure Bode plot: asg1 sine on OUT2 (summed with pid2), point by point, Stop. **Not locked** (off / held): open-loop
    plant P = in2/x around the present drive (amplitude `int_bode_amp_mv`); `plant_gain()` takes its low-frequency
    value as K = dV_IN2/dV_OUT2 (sign from the phase), saved to `intensity_plant.json` (runtime, gitignored).
    **Locked**: captures (asg1, pid2) and (asg1, in2), T = c/x, G = −T/(1+T), S = 1+T, plant = (in2/x)/S, amplitude
    `int_bode_amp_frac` × setpoint/|K| inside the clamp; it also re-measures K at the lock point and updates the shown
    |i·K| (gains unchanged; re-lock or touch a gain setting to apply). Stops and holds if the output reaches a clamp.
  - Lock: asg1 off → setpoint → clamp (`int_clamp_lo_mv` / `int_clamp_hi_mv`; low = bright side, keep it short of the
    efficiency peak) → start from the present drive (0 V = bias after Output on) → gains in steps (f_u/10, f_u/3, f_u).
    Bandwidth mode needs a measured K (refused otherwise): i = −f_u/K, p = i/f_corner. Manual mode: P and I dropdowns
    (1-2-5 ladders, magnitudes), sign −sign(K), or from `int_plant_sign` (default negative) while K is unmeasured.
    D is shown as a disabled dropdown (not in this bitstream). i ≤ 38.9 kHz → cap 38.9 kHz × |K| (warned).
  - Setpoint adjuster: slider (0..1000 mV) + ±0.1/1/10 mV buttons + number box (`int_setpoint`, `int_nudge`
    commands); written live. K is not re-derived for a new setpoint (re-measure with a locked Bode plot).
  - Watchdog: light < `int_light_frac` × setpoint twice → `nolight` (hold at the median of the last good drives; with
    Re-engage on, lock again after 0.2 s of light). Output within 2 % of a clamp for > `int_rail_s` → `railed`
    (hold, log; no auto re-engage).
  - Start-up: pid2 on out2 with non-zero gains → adopted as `locked`, registers untouched (setpoint/filter read
    back). Otherwise pid2 p = i = ival = 0, output off, asg1 off → OUT2 = 0 V, mode `off`.
  - No intensity-noise analysis in the app (user decision).
- **Scope schedule** (`Controller.run` / `_pick`): one frame per pass for one loop. The visible view's loop gets the
  frames; the hidden loop gets 1 frame/s. Its watchdog is the register-only check (`_bg_service`: pid ival + sampler
  photodiode value, every 0.1 s) that runs inside the visible loop's captures and inside every `_wait()` of a
  sequence or measurement (all `time.sleep`s in sequences are `_wait`). Without a sampler the hidden loop instead
  gets watchdog frames every 0.15 s (cavity locked/held) / 0.2 s (intensity locked/held/fault). A background check
  only holds its own loop; a cavity relock it triggers runs in the next cavity frame. Intensity live frames are
  capped at 15/s. `ctl.frames` / `ctl.bg_count` count both.
- UI:
  - Two icons at the top left (`vbtn-cav`, `vbtn-int`) with status dots (green locked, blue scanning/locking/
    measuring, amber held, red fault, grey off). The view lives in `dcc.Store("view", storage_type="local")`; a
    clientside callback only toggles `display`, so switching writes nothing to the hardware. The tick reads the
    view, sets `ctl.view` (scheduler hint) and builds frame/diagram/Bode data for the visible view only.
  - Settings values are pushed by their own callback (`push-version` → `on_push`), not by the tick, so the tick's
    output list stays short with both views' inputs on the page.
  - The server sends compact data in `dcc.Store("frame")`.
  - A **clientside** callback (`SCOPE_JS`) draws with `Plotly.react` using **SVG `scatter`**. `scattergl` redraws about 2× slower here.
  - The noise and loop figures have **their own callbacks keyed on `meas-seq`**, never in the tick response. Putting them in the tick response froze the page.
  - Numeric inputs use `step="any"` (the integer gain uses step 1).
  - The diagram: blocks are absolutely positioned Dash components on a fixed 1340×500 canvas (intensity: 1340×575); every `s-{key}` input
    exists exactly once (inline in a box or in a hidden popover), so the version-checked apply callback is unchanged.
    `DIAG_JS` (clientside) draws the SVG wires once and updates readback text, switch pills and probe badges from
    `dcc.Store("diag")`, which the tick sends only when it changes. `POP_JS` opens/closes popovers.
  - Intensity view: `iframe` / `idiag` / `ibode` stores, `ISCOPE_JS` (live probes A/B, history); main capture
    in2 + pid2, other probe pairs every `drive_every` frames.
  - Probes: the main capture stays in1 + iq0. Probe pairs inside {in1, iq0} reuse it; others take a second capture
    every `drive_every` traces (the old "drive" capture; the `panels` key "drive" is shown as "probes A/B").

## Verified PyRPL facts (see docs/pyrpl_reference.md for more)
- `iq0`:
  - The NCO dither is set by `frequency` and `amplitude` and goes out through `output_direct`.
  - Demodulation: `input`, `phase`, `bandwidth` (a list; **max 2 low-pass stages**, 1st order each), `acbandwidth` (1 high-pass stage), `quadrature_factor` (**integer** gain).
  - `output_signal="quadrature"` gives the internal signal `iq0` that `pid0` and the scope see.
- Filter corners snap to a factor-2 ladder: 250 Hz → 303.6 Hz and 100 Hz → 75.9 Hz.
- The `pid0.i` resolution is about 0.0046 Hz. Below that the integrator is 0, and the app warns.
- `pid0` attributes: `input`, `setpoint`, `p`, `i`, `ival` (integrator state = output), `min_voltage`/`max_voltage` (clamp), `output_direct`, `inputfilter`.
- ASG waveforms: `"ramp"` is a **triangle**, `"halframp"` is a sawtooth.
- Scope acquisition calls differ by version. 0.9.9.1 uses `_start_trace_acquisition`/`_get_trace`/`curve_ready`; 0.9.3 uses `_start_acquisition`/`_get_curve`. The app handles both.
- Reserve modules with `module.owner = "..."` so PyRPL tools (the network analyzer uses an IQ module) don't take them. The network analyzer is `p.networkanalyzer`.
- Every module with `output_direct="out1"` is summed digitally into OUT1, then clipped to ±1 V (same for out2:
  pid2 + asg1).
- From the 0.9.9.1 Python source (fetched 2026-11-02): the scope's trigger sources include `asg1`; `r.sampler.<signal>`
  reads the present value of any DSP signal (one register) — **verify on hardware**; pid `p` ≤ ~2048 (step 2^-12),
  `i` ≤ ~38.9 kHz; `inputfilter` up to 4 stages, negative = high-pass; D commented out; differential mode only
  pid0/pid1.

## Testing without hardware
- Playwright in this container: `pip install playwright`; the tests launch `/opt/pw-browsers/chromium` (env `CHROME`).
- `app/test/fake/pyrpl.py` is a stand-in `pyrpl` module.
  - It simulates a cavity with 3 modes, PZT lag on the dither, the IQ demodulator with n-pole low-pass, a PyRPL-style quantized PID with clamps, the scope, the ASG and a NetworkAnalyzer.
  - Environment variables: `FAKE_DT`; `FAKE_LAN_S` emulates the per-register LAN delay; `REG_COUNT` counts register accesses.
  - `WRITES` logs every register write by app code as (t, module, attr, value, app source file); `WRITE_N`,
    `WRITE_N_NONSCOPE` count them (constructor writes are not logged). `serve_v3.py` exposes `/_test/writes` and
    `/_test/stats`.
  - OUT2: `class AOM`: inverting driver with bias → mixer (RF ∝ IF current, saturating, positive only) → sin²
    efficiency (peak at OUT2 ≈ −63 mV, 0.9 V) → photodiode (delay `FAKE_AOM_DELAY_US`, default 0.5 µs, 5 MHz pole,
    noise 1/f + lines 120 Hz/1.3 kHz/17 kHz + white, scaled by `FAKE_RIN`; ADC noise ~63 nV/√Hz). Slow state every
    2 ms (drift, `aom.disturb`, `aom.blocked`, pid2 running to its equilibrium or a clamp); scope windows synthesized
    (closed loop linearized with the true delay, lines and the asg1 test sine exact).
  - Module sums are routing-aware (`ASG.value(t, out)`); the circuit-only copies are iq1 → pid1 driven by asg0.
  - The fake dither is 4 % larger than requested (DITHER_SCALE 1.04) to exercise the amplitude calibration.
- Run these from `app/test`:
  - `python test_v3.py` is the controller test. It runs acquire → pick peak → auto-phase → lock with a different lock dither → noise → loop → kick and relock → unlock → lock from held → acquire → sawtooth.
  - `test_v3.py` also checks the schematic: every FIELDS key has exactly one home, every block parameter changes its
    register on the fake (and the readback), all switches and interlocks, probe placement, crosshair targeting.
  - `python serve_v3.py` serves the app on the fake at port 8070, then `python ui_v4.py` runs the Playwright browser test:
    edits every block parameter through its own input, flips every switch (incl. the Scan-while-locked confirm),
    places probes, clicks a crosshair, locks with the switch, runs both measurements, redraws/s, screenshots.
  - `tick_timing.py`, `render_timing.py`, `prof_tick.py` and `bench_v2style.py` are performance probes.
- `python test_intensity.py`: no scan functionality; bandwidth lock refused without K; manual lock with the sign
  setting; open-loop Bode → K (sign, within 5 % of the fake) and the plant incl. delay; lock (gains, |i·K|), 120 Hz line
  ×200 and 0.1–3 kHz ×28 suppression, step recovery, beam block → hold → re-engage, Re-engage off, railed fault,
  setpoint adjuster, manual P/I, cap warning, every intensity block parameter → its register, probes, switches,
  closed-loop Bode vs the fake's true loop gain (< 10 %), UGF = |i·K_true|, K updated at the lock point, Stop,
  open-loop Bode when unlocked, old settings file (out2_*, int_scan_*), restart adoption / reset.
- `python test_isolation.py`: open-loop intensity Bode while the cavity scans; both loops locked; hidden-loop checks ≥ 5/s in both views (with and without a
  sampler); a cavity unlock while hidden is caught and relocked; intensity watchdog while hidden; measurements in
  one loop with the other locked; view switching writes nothing; the write log in both directions.
- `ui_v5.py` (Playwright, after `serve_v3.py`): icons/dots, both loops locked through the UI, view switching writes
  nothing (fake counter), view remembered after reload, every intensity block parameter through its own input,
  setpoint buttons/slider, probes, switches, open-loop plant Bode (K) then lock, closed-loop Bode + Stop, redraws/s
  per view, screenshots `v5_*.png`.
- Bode tests: `test_v3.py` checks that the circuit-only sweep matches its model (< 5 %, < 5°), that asg1 is never
  routed to an output during it, that the closed-loop G equals the fake's true loop gain below 100 Hz (< 5 %), and Stop.
  `ui_v4.py` checks that the plot fills in point by point, both modes complete, and Stop.
- The fake scope records **after** it is armed (untriggered), like the hardware; it used to return the past window.
- Results 2026-11-02, after removing the intensity scan: `test_intensity.py` 69/69, `test_isolation.py` 30/30,
  `ui_v5.py` 63/63, `ui_v4.py` 56/56, `test_ramp_setpoint.py` 8/8, `test_v3.py` 62/63 (only the noise-limited
  closed-loop cavity Bode check, 7.2 % at 4 Hz vs 5 %; cavity code unchanged). Open-loop plant Bode on the fake:
  K −8.04 V/V at the bias level (true −8.06); a lock set from it gives |i·K_true| ≈ 35–37 kHz for a 30 kHz setting,
  because K is −9.6 at the lock point; the closed-loop Bode measures and shows that.
- Results 2026-11-02, v5 (Linux container, 4 cores, fake): `test_v3.py` all pass (63), `test_intensity.py` 71/71,
  `test_isolation.py` 29/29, `ui_v4.py` 56/56, `ui_v5.py` 70/70. Redraws/s: cavity scanning 7.4–7.6, cavity locked
  9.8–10 (intensity locked and hidden), intensity locked 10; main (v4 + Bode) on the same host: 7.5 / 10. Tick
  4.5 ms (cavity view, incl. JSON) vs 4.0 ms on main. Intensity Bode on the fake: UGF 30.2 kHz for a 30 kHz setting,
  PM 86°; measured G within 2 % of the fake's true loop gain. The closed-loop cavity Bode accuracy check (5 % at
  4 Hz) is noise-limited and fails in some runs (6.2 %, 5.7 %) on this host too; it passed in the final run.
  Screenshots: `docs/screenshots/v5_*.png`.
- Results 2026-11-02 (Bode added, a slower container): `test_v3.py` 63/63; `ui_v4.py` 56/58 functional checks pass,
  the 2 redraw-rate checks fail on that host for the old and the new code alike (A/B: 5–6/s both; tick 7.0 ms old vs
  7.5 ms new there).
- Last results (v4, 2026-11-01, Linux sandbox, fake):
  - `test_v3.py`: 51/51 checks pass. `ui_v4.py`: 52/52 checks pass.
  - Lock, noise, loop (UGF 9.5–12.5 Hz, phase margin 87°), relock and lock-from-held are OK.
  - The browser redraws 7.6/s scanning and 10/s locked (same as v3).
  - Tick callback 3.0 ms incl. frame JSON (`prof_tick.py`); diagram state + hash 0.06 ms.

## Pitfalls already hit
- Fake: `ts[1] - ts[0]` on epoch timestamps (~1.8e9 s) loses precision (float64 step 0.24 µs); use the mean spacing.
- Dash 4 wraps `dcc.Input(type="number")` in `.dash-input-container` with ± steppers; size that container, not the input.
- Every FIELDS key needs exactly one `s-{key}` component in the layout, or the tick callback errors ("ID not found").
- A switch click sends the desired state (`not ctl.switches()[name]`), so a double click cannot toggle twice.
- Don't put Plotly figures for rarely changing panels in the fast tick callback; return `meas-seq` only when it changes.
- Don't use `scattergl` for the live plots.
- Loop measurement: start below the UGF (`na_start_hz` 4 Hz with a 10 Hz UGF), or no crossing is found.
- Only one process may own the Red Pitaya, so close the PyRPL GUI or notebooks before running the app.
- PyRPL's `iq.phase` register sign is inverted relative to the naive formula. Trust the attribute, not hand-written registers.
- On Windows, `import pyrpl` failed once because VS Code used a different interpreter; pick the environment where PyRPL is installed.
- Dash 4 debounced number inputs: after a callback rewrote the box, re-typing the value the box itself sent before is
  swallowed (type 500, nudge to 510, type 500: no callback). The intensity setpoint box therefore also has a
  clientside Enter/blur path that reads the DOM value (`SP_TYPED_JS` → `int_setpoint`). Other boxes still have the quirk.
- Dash 4 slider: its root element intercepts clicks on anything it overlaps; never give it negative margins.
- Pattern-matching ids with floats render as JSON numbers (`{"d":10}`, not `10.0`): select by text in tests.
- Killing test servers with `pkill -f serve_v3.py` also kills the calling shell (its command line matches).

## Style the user likes
Direct, physics-literate explanations; settings shown in real units; values on screen equal what the hardware uses;
robust defaults (auto phase, auto relock, clamps from the mode spacing); no silent fallbacks.
