# AOM intensity stabilization on IN2/OUT2: plan

Status: **draft, waiting for approval** (2026-11-02).

## 1. What the current code looks like (branch base = main @ 66052d5, v4 + swept-sine Bode)

- `app/schematic.py` is the cavity model: `BLOCKS` (params → FIELDS key → PyRPL attribute), `WIRES`, `SWITCHES`,
  `PROBES`, block writers (`WRITERS`, `STATIC` written by `configure()`, `LIVE` only in their mode),
  `apply_changed()`, `READBACK`/`read_hw()` (register readback → `ctl.hw`), `switch_states()` and
  `diagram_state()` (readback text, live wires, switch pills, probe badges).
- Settings: `FIELDS` in `cavity_lock_app.py`, one flat dict `ctl.s`, version-checked applies
  `("apply", (values, version))`; `ui.py` has exactly one `s-{key}` input per key (inline or in a hidden
  popover), one big tick callback (frame store, diag store, status, log, settings push when the version changes),
  clientside `SCOPE_JS`/`DIAG_JS`/`BODE_JS`/`POP_JS`; measurement figures have their own callbacks.
- Scope captures: there is no scheduler. `Controller.run()` dispatches on the cavity mode: `_acquire_tick()`
  (in1+iq0 over one ramp period triggered on asg0, plus out1/asg0 or probe captures every `drive_every`) or
  `_live_tick()` (33 ms in1+iq0, watchdog). Lock sequences and measurements block the loop with `time.sleep`
  and long captures (noise: 1.07 s traces; Bode: up to 2.1 s per capture, minutes per sweep).
- OUT2 today: `pid1` monitor (`out2` block, `w_out2`, keys `out2_signal`/`out2_gain`, probe `out2`, wire `out2`).
  Start-up sets `asg1, pid2, iq1, iq2` `output_direct = off` and reserves `pid1, asg1, iq1, pid2`.
- Tests: `test_v3.py` 62/63 on this container (the one failure is the existing closed-loop-Bode accuracy check,
  5.7 % and 12.6 % at its first and last point vs a 5 % limit; noise-limited, pre-existing). `ui_v4.py` not run yet.

### Open item from the schematic/Bode work that collides with this task
**Measure Bode plot uses `asg1`, `iq1` and `pid2`**: *closed loop* injects asg1 into OUT1; *circuit only*
drives the spare copies iq1 → pid2 with asg1. Under the new allocation the cavity must never write pid2 or asg1.
Proposal (needs your choice, see §9):
- *closed loop*: use **asg0** as the swept-sine source (it is idle while locked; the ramp is off). Scope captures
  (asg0, pid0) and (asg0, iq0). Same analysis. `switch_states` shows Scan ON only for the ramp waveforms.
- *circuit only*: asg0 → **iq1** (lock-in copy at baseband) and a PID copy on **pid1** with `output_direct = off`,
  both returned to off at the end. In scan mode the ramp is paused for the sweep and restarted afterwards.
  Alternatives: (b) measure only iq1 and compute the PID from the pid0 register values (pid1 never touched),
  (c) drop circuit-only.

## 2. FPGA module allocation (goes into CLAUDE.md)

| Module | Owner | Use |
| --- | --- | --- |
| iq0 | cavity | dither NCO + demodulation |
| asg0 | cavity | ramp; swept-sine source of the cavity Bode (proposal) |
| pid0 | cavity | lock, output_direct out1 |
| OUT1 | cavity | only cavity modules route to out1 |
| pid2 | intensity | input in2, output_direct out2 (the servo) |
| asg1 | intensity | power scan, output_direct out2; scope trigger for the scan |
| pid1 | nobody | off (p = i = 0, output off). Kept free: only pid0/pid1 pair in differential mode |
| iq1, iq2 | network analyzer | free (NA takes one); iq1 also the cavity circuit-only copy (proposal) |
| scope | shared | scheduled, §6 |

Isolation rules, enforced by `test_isolation.py` on a fake that logs every register write with the source file
of the app frame that made it:
- cavity code (`cavity_lock_app.py`, `schematic.py`) never writes pid2 or asg1 and never sets any
  `output_direct` to `out2`/`both`;
- intensity code (`intensity.py`, `intensity_schematic.py`) never writes iq0, asg0, pid0 and never routes
  anything to out1/both.

Removed: the OUT2 monitor (block, writer, wire, probe, `pid1` in `configure()` and `READBACK`), the start-up line
that switches pid2/asg1 off. Start-up now sets `pid1` off (p = i = 0, output off) so an old v4 monitor that is
still running does not add onto the servo's OUT2. Old settings files: `out2_signal`/`out2_gain` are not in
`DEFAULTS` any more, so Load and start-up drop them silently (tested).

## 3. Verified PyRPL facts (0.9.9.1 Python source, fetched this session)
- `scope._trigger_sources` contains `"asg1": 9`: the power scan can trigger the scope on asg1.
- `hardware_modules/sampler.py` exists: `r.sampler.in2`, `.pid2`, `.in1` … are single 14-bit register reads of the
  current sample (one LAN round trip). **Verify on hardware** in the read-only check (the bitstream must have it).
  The app uses it only for the background watchdogs and falls back to the last scope frame plus `ival` without it.
- pid: `p` 24-bit, norm 2^12 → max ≈ 2048, step 2.4e-4; `i` 24-bit, norm 2^32·2π·8 ns → step 0.0046 Hz,
  max ≈ 38.9 kHz; D commented out; `inputfilter` = up to 4 stages, positive = low-pass, negative = high-pass,
  power-of-2 ladder (docstring and `FilterRegister`); `differential_mode_enabled` only for pid0/pid1.
- Error = input − setpoint; negative feedback needs i·K < 0; |i·K| = integrator unity-gain frequency.

## 4. Intensity settings (all keys prefixed `int_`, saved in the same settings file)

| Key | Units | Default | Register / use |
| --- | --- | --- | --- |
| `int_out2_limit_mv` | mV | 300 | app-side absolute OUT2 limit for scan and clamps (like OUT_LIMIT for OUT1) |
| `int_scan_lo_mv`, `int_scan_hi_mv` | mV | −200, +200 | asg1 offset/amplitude (triangle), clipped to the OUT2 limit |
| `int_scan_hz` | Hz | 50 | asg1.frequency |
| `int_scan_avg` | – | 4 | traces averaged per scan frame |
| `int_setpoint_mv` | mV of IN2 | 500 | pid2.setpoint |
| `int_filter_hz` | Hz (<0 = high-pass, 0 = off) | 0 | pid2.inputfilter (snapped, readback shown) |
| `int_filter_stages` | 1–4 | 1 | pid2.inputfilter list length |
| `int_gain_mode` | bandwidth / manual | bandwidth | how p, i are found |
| `int_ugf_khz` | kHz | 30 | i = −f_u/K |
| `int_corner_khz` | kHz | 1000 | p = i / f_corner |
| `int_p`, `int_i_hz` | –, Hz (signed, as written) | 0, 0 | pid2.p / pid2.i in manual mode |
| `int_gain_ramp` | bool | True | engage in steps f_u/10, f_u/3, f_u |
| `int_clamp_auto` | bool | True | clamp from the scan |
| `int_clamp_margin` | fraction of peak light | 0.05 | bright clamp where light = (1 − margin)·peak, on the near side of the peak |
| `int_clamp_lo_mv`, `int_clamp_hi_mv` | mV | −150, +150 | pid2.min/max_voltage when auto is off |
| `int_rail_s` | s | 0.2 | railed fault after the output sits at a clamp this long |
| `int_light_frac` | × setpoint | 0.3 | below → "no light" (hold) |
| `int_auto_reengage` | bool | True | re-lock when the light is back for 0.2 s |
| `int_mw_per_v` | mW/V | 0 (off) | show photodiode power in mW |
| `int_window_us` | µs | 1050 | live trace length (snapped to a scope duration) |
| `int_noise_avg` | – | 3 | RIN traces per band |
| `int_rin_mode` | closed / open vs closed | closed | RIN measurement |
| `int_na_start_hz`, `int_na_stop_hz`, `int_na_points` | Hz, Hz, – | 1e3, 2e6, 40 | network analyzer |
| `int_na_amp_frac` | × setpoint | 0.02 | NA excitation, converted with K, kept inside the clamp |
| `int_probe_a`, `int_probe_b` | signal | in2, pid2 | scope probes of the intensity view |
| `int_panels` | list | traces, history, scan | intensity panels |
| display only (plant, grey) | | | |
| `int_bias_v` | V | 2.5 | bias pot magnitude (V_drv = \|V_bias\| − 10·V_OUT2), for the RF level estimate |
| `int_if_full_ma` | mA | 25 | driver current limit = "full IF" |
| `int_rf_src_dbm` | dBm | 4 | 80 MHz source into the LO |
| `int_amp_gain_db` | dB | 32 | ZHL-1-2W+ |
| `int_aom_max_dbm` | dBm | 31 | AOM rating: warn when the estimate exceeds it |
| `int_pd_bw_mhz` | MHz | 5 | photodiode pole (loop model) |
| `int_aom_delay_us` | µs | 0.25 | acoustic transit (loop model) |

Runtime, not saved: last scan (curve, peak, clamp, K(u), u at setpoint, max lockable light). It is also written
to `intensity_scan.json` next to the app after every scan, so a restart can adopt a running servo with a known K.

## 5. Modes, lock and watchdog

Modes: `off` → `scanning` → `locking` → `locked`, plus `held`, `measuring` and the faults `railed`, `nolight`.

- **off** (Output switch off): pid2 p = i = 0, ival = 0, output off; asg1 off → OUT2 = 0 V = bias level.
- **Power scan** (Scan switch): pid2 p = i = 0, ival = 0 (output stays on/off as it is: it adds 0); asg1 triangle
  between the scan limits on out2; capture (in2, asg1) one period triggered on asg1, averaged. Analysis:
  binned light vs OUT2, the bright direction (sign of K), the efficiency peak (maximum of the smoothed curve; if it
  is at the scan edge: "no peak in range", clamp = scan edge), auto clamp = margin point on the near side,
  K = dV_IN2/dV_OUT2 at the setpoint on the monotonic branch, u(setpoint), max lockable light (at the clamp).
  If the setpoint is above the max lockable light the scan says so (Lock is still allowed; it will rail).
- **Lock** (Lock switch): needs a scan (or the saved one, with a warning). asg1 off → setpoint → clamp
  (min/max_voltage) → ival = u(setpoint) → gains in steps (f_u/10, f_u/3, f_u, 30 ms each, checking ival after
  each step). Bandwidth mode: i = −f_u/K, p = i/f_corner. Shown: requested → register values, |i·K|, the
  integrator cap 38.9 kHz·|K| with a warning if f_u exceeds it, the p quantization, and a model phase margin
  (PI + delay 0.21 µs + AOM delay + photodiode pole + input filter).
- **Unlock** (Lock off): p = i = 0, ival kept → `held` (constant RF at the last drive).
- **Watchdog** (every intensity frame and in the background checks of §6):
  - light < `int_light_frac`·setpoint (two checks in a row) → `nolight`: p = i = 0, ival = median of the last good
    drive values (the integrator will already have run to the bright clamp; restoring avoids a full-power burst
    when the beam comes back). With `int_auto_reengage`, light at the held drive above the threshold for 0.2 s →
    lock again (same engage).
  - ival within 2 % of the clamp span of either clamp for longer than `int_rail_s` → `railed`: hold at the last
    good drive, log it, stay there until the user acts (no auto re-engage: the setpoint is probably unreachable).
- **Start-up**: if pid2.output_direct is out2 and p or i ≠ 0 → adopt as `locked` (K from `intensity_scan.json` if
  present, else "K unknown: gain changes need a scan"). If out2 with zero gains and ival ≠ 0 → adopt as `held`
  (no light change). Otherwise write nothing except asg1 off → OUT2 = 0 V (bias level).

### Measurements (intensity view)
- **Measure RIN** (locked): Welch spectra of in2 in two bands (8.4 ms @ 0.5 µs → up to 1 MHz; 134 ms → 3.7 Hz
  resolution), RIN = √S/⟨V⟩, shown in 1/√Hz and dBc/Hz with band rms; title "in-loop RIN (underestimates the
  real noise)". Mode *open vs closed*: closed, then hold (same drive, loop open), open, then re-engage.
- **Measure loop** (locked): PyRPL network analyzer with output_direct out2, inputs pid2 then in2.
  T = c/x, G = −T/(1+T), plant = (in2/x)/(1+T); amplitude = `int_na_amp_frac`·setpoint/|K|, capped so
  ival ± amplitude stays inside the clamp. UGF and phase margin, with the model.

## 6. Scope schedule (both loops always run in hardware)

One scope, one controller thread. Each pass of `Controller.run()` takes the next scope frame for one loop:
- **visible loop**: every frame not taken below;
- **hidden loop**: a watchdog frame when due: cavity locked/held every 0.15 s (≥ 6.7/s; its 33 ms frame), intensity
  locked/held/fault every 0.2 s (its ~1 ms frame); scanning or off: 1 frame/s (keeps its panel fresh, no watchdog);
- **background checks** (no scope): while one loop runs a blocking sequence or measurement (lock sequence, scope
  captures up to 2.1 s, NA sweeps), the other loop's watchdog runs from register reads every ≤ 0.1 s inside the
  waits (`_wait()` replaces `time.sleep` in sequences and in `capture()`'s arm-to-ready wait): cavity = pid0.ival +
  sampler.in1 (averaged over a few reads), intensity = pid2.ival + sampler.in2. A background check may only hold
  its own loop (write its own PID); re-engage/relock waits for the main loop.
- A measurement in one loop writes only its own modules (NA on iq1/iq2 for out2 or out1) and only takes scope
  frames; the other loop keeps its watchdog through the background checks.
- Expected rates (fake): cavity visible + intensity locked hidden: ~9/s cavity frames; intensity visible + cavity
  locked hidden: ≥ 10/s intensity redraws, cavity watchdog ≥ 6/s.

## 7. Views, UI

```
┌──────┐┌──────┐  status of the visible loop ……  [view's measurement buttons]  [Save] [Load]
│ |()| ││  ☼~  │
│  ●   ││  ●   │   ● green locked · blue scanning/locking/measuring · amber held · red fault · grey off
└Cavity┘└Inten.┘   (each dot always shows its own loop, from either view)
```
- Both views are in the DOM; the icons only toggle `display` (clientside) and store the view in
  `dcc.Store(storage_type="local")`. The tick reads the view as State: it builds the frame, diagram state and
  panels for the visible view only and tells the controller which loop is visible (a scheduler hint, no register
  access). Switching views therefore writes nothing to the hardware (Playwright checks the fake's write counter).
- Every `s-{key}` input of both views exists once; the existing version-checked apply callback covers both.
- Shared log at the bottom (lines tagged `[cav]` / `[int]`).

Intensity schematic (canvas ~1340×470, same machinery as the cavity: model in `app/intensity_schematic.py`):
```
┌ external (display only) ─────────────────────────────────────────────────────────────────────────────────┐
│ ┌Pickoff PD┐◄──1st order──┌AOM ────┐◄──┌RF amp ZHL-1-2W+┐◄──┌Mixer ZX05-1+┐◄─LO─┌80 MHz src┐             │
│ │ gain, mW │               │+29 dBm │   │ +32 dB  +29 dBm│   │ RF −3 dBm   │    │ +4 dBm   │             │
│ └────┬─────┘               └────────┘   └────────────────┘   └──────▲──────┘    └──────────┘             │
│      │                                                         IF  │ 18 mA                               │
│      │                                                   ┌Bias driver ×(−10) + |V_bias|┐◄───────────────┐ │
└──────┼───────────────────────────────────────────────────┴──────────────────────────────┴───────────────┼─┘
    IN2│•in2                                                                                              │
 ┌─────▼──────┐   ┌─────────┐        ┌────────┐   ┌────────┐ •pid2        ┌─┐            ┌────┐            │
 │Input filter│──►│Setpoint │─/Lock/►│PI pid2 │──►│Limiter │──/Output/───►│Σ│───────────►│OUT2│•out2 ──────┘
 │off / LP/HP │   │ 500 mV  │        │30 kHz  │   │−92..+  │  [watchdog]   └▲┘            │±300│
 └────────────┘   └─────────┘        └────────┘   └────────┘               │             └────┘
 ┌Power scan (asg1) triangle 50 Hz −200..+200 mV┐ •asg1                    │
 └──────────────────────────────────────────────┘──────────/Scan/──────────┘
```
Switches: Scan (asg1.output_direct), Lock (pid2 gains), Output (pid2.output_direct). Probes: in2, pid2, asg1, out2
(the raw error inside pid2 is greyed out with the reason, as in the cavity view). The plant boxes show the RF
level estimated from the present drive: I_IF ≈ (|V_bias| − 10·V_OUT2 − 0.3 V)/100 Ω limited to 25 mA,
mixer RF ≈ −1 dBm + 20·log10(I/25 mA), amplifier +32 dB, warning above the AOM rating.

Intensity panels: live traces (probes A/B, default in2 + pid2 with error and setpoint lines), history (mean and
rms relative intensity, drive), power scan (light vs OUT2 with peak, clamp, setpoint, K), RIN spectrum, loop Bode.
Cavity view: unchanged except the OUT2 monitor block is gone (and the Bode test-sine box names asg0).

## 8. Code and tests
- New: `app/intensity.py` (settings `I_FIELDS`, scan analysis, `IntensityServo`: modes, lock, watchdog,
  measurements; called by the Controller from the main thread), `app/intensity_schematic.py` (model),
  view switcher and intensity view in `app/ui.py` (block/switch/probe builders parameterized by the model).
- Controller: owns both; `run()` gets the frame scheduler; `_wait()`; commands for the intensity loop are
  namespaced (`("int_switch", …)`, `("int_probe", …)`, `("int_measure", …)`). Run command unchanged.
- Fake: register-write log (module, attr, value, app file); pid2 + asg1 summed on out2; AOM plant
  (driver with bias → mixer: RF ∝ IF current, saturating, positive only → amplifier → sin² efficiency with a peak →
  photodiode gain, ~0.5 µs delay/lag, RIN = 1/f + vibration lines + white; `FAKE_AOM_DELAY_US`, `FAKE_RIN`).
  The µs loop cannot run in real time in Python, so: slow state at the engine step (implicit-Euler integrator,
  clamps, drifts, step disturbances, blocked beam) + scope windows synthesized from the linearized loop
  (noise shaped by S = 1/(1+G) with the true delay); the NA uses the same G.
- `test_intensity.py`: the six scenarios of the task. `test_isolation.py`: both loops locked, scan, measurements,
  view-independent; write-log assertions in both directions. `test_v3.py` adapted (OUT2 checks removed, Bode on
  asg0). `ui_v5.py` (Playwright): icons + dots, switching while both locked writes nothing, every intensity block
  parameter/switch/probe, both measurements, redraws/s per view, screenshots.
- Docs: CLAUDE.md, README (wiring IN2/OUT2), `docs/intensity_hardware.md`, `docs/pyrpl_reference.md`.

## 9. Decisions needed
1. Cavity Bode: closed loop on **asg0** (proposed) and circuit-only on **iq1 + pid1 (output off, borrowed during the
   sweep)**, or (b) iq1 measured + PID computed from pid0's registers, or (c) drop circuit-only.
2. Start-up adoption of a *held* servo (out2, zero gains, ival ≠ 0) as `held` instead of resetting OUT2 to 0 V.
