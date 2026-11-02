# Cavity PZT dither lock (Red Pitaya + PyRPL) — project memory

Owner: Nathan Song (KRb lab). Windows 10/11 laptop, VS Code, Python environment where `import pyrpl` works.
Working folder on the laptop: `C:\Users\songo\Desktop\P-Physics\KRbGeneral\redpitaya_locking\lockcavityPZTtolaser`.

## What this is
A browser app (`app/cavity_lock_app.py`, Dash + Plotly) that locks the length of an optical cavity to a laser.
It dithers the cavity PZT, demodulates the transmitted intensity with the Red Pitaya's own lock-in (PyRPL `iq0`),
and integrates the error with `pid0`. The user confirmed that v2 **locks and is robust** on the real hardware. v3 added speed + diagnostics;
v4 (current) replaces the settings sidebar with a Moku-style **schematic** of the real signal chain (`docs/schematic_plan.md`).
v3 and v4 have been tested only against the fake so far.

## Hardware
- Red Pitaya STEMlab 125-14, hostname `rp-f0fa92.local`. PyRPL 0.9.9.1. Its FPGA bitstream matches the GitHub `pyrpl-fpga/pyrpl` Verilog at tag 0.9.9.1.
- IN1 ← cavity transmission photodiode. Use the LV jumper below 1 V; with HV, set the app's "IN1 scale" to 20.
- OUT1 → PZT driver MOD IN. OUT1 carries the ramp plus the dither, or the setpoint plus the dither. The app never commands beyond ±0.95 V.
- OUT2 → scope monitor (`pid1` used as a pass-through: error, setpoint, ramp or photodiode).
- IN2 is free.
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

## Architecture (keep it)
- Files: `app/cavity_lock_app.py` (analysis, `Controller`, `main`), `app/schematic.py` (the diagram as data: blocks,
  parameters → FIELDS key → PyRPL attribute, block writers, wires, switches, probes, readback text),
  `app/ui.py` (Dash layout and callbacks, `build_app(ctl, A)`; `cavity_lock_app.build_app(ctl)` wraps it).
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
  - Applies are version-checked: `("apply", (values, version))`. The controller bumps the version with `_bump(keys)`;
    a stale apply keeps the controller's value for those keys and applies the user's other edits (it used to be
    dropped whole, silently losing an edit made right after a snap or phase fit).
- `configure()` clears `pid0.inputfilter`, then runs the static block writers (iq0, pid1 = OUT2 monitor or D,
  pid2 = band-pass). Never clear pid1/pid2 filters there: that wiped D's high-pass on every setting change (fixed).
- **Band-pass** (block "Band-pass"): `bp_on` routes in1 → pid2 (P = 1, I = 0, 4-stage input filter: `bp_hp_order` ×
  high-pass at `bp_hp_hz`, `bp_lp_order` × low-pass at `bp_lp_hz`) → `iq0.input = "pid2"`. Off: iq0 reads in1.
  iq0's own AC high-pass (`ac_hz`) stays. The loop model includes it through its baseband equivalent
  `bandpass_equivalent()`: [BP(f_d+f)/BP(f_d) + conj(BP(f_d−f)/BP(f_d))]/2.
- **D** (block "D (pid1)"): the FPGA has no derivative stage, so pid1 runs on the error with a high-pass input stage at
  `d_corner_hz` (+ optional low-pass `d_lp_hz`) and gain p = −(corner/`d_ugf_hz`)/S, summed into OUT1. Loop term
  (jf/f_D)/(1 + jf/corner). Live only while locked (`_d_update()` at engage, unlock, park, ramp, Output off). While
  `d_on`, pid1 is not the OUT2 monitor. Without enough P, I and D cancel at √(UGF·f_D): `pid_null_warning()`.
- **Corner dropdowns**: `ctl.options` (`lpf`, `ac`, `pidf`) read at connect from PyRPL (`iq0.bandwidths`,
  `iq0.inputfilter_options`, `pid2.inputfilter_options`; formula fallback). Widgets in `schematic.P(widget=...)`:
  `ladder:<kind>[0]` (dropdown, 0 = with "off"), `radio:a,b,c`. Snapped values are stored to 6 significant digits.
  - `_snap_to_hardware()` reads back what the FPGA really uses (filter corners on a factor-2 ladder, integer `quadrature_factor`) and shows those values in the UI.
- Modes: `connecting` → `acquire` ↔ `locking` → `locked` / `held`, plus `measuring` and `off` (Output switch).
  - `_lock()` → `_zoom_and_lock()`: a slow sweep across the chosen peak with the lock dither, then a 4-phase cos² fit of the phase, then the zero crossing and slope.
  - `_engage()`: parks at the zero crossing, then ramps the gain in thirds (ugf/10, ugf/3, ugf).
  - `_live_tick()`: the watchdog. `_relock()`: automatic relock.
- Loop gains: `pid0.i = -ugf/S` and `pid0.p = -p_gain/S`, where S is the measured error slope per OUT1 volt. Then |i·S| equals the loop bandwidth, and the minus sign gives negative feedback whatever the sign of S.
- Diagnostics while locked:
  - `_measure_noise()`: Welch spectra in two bands, with band rms.
  - `_measure_loop()`: PyRPL network analyzer on `pid0` and `iq0`. With T = c/x, G = −T/(1+T) and P = (e/x)/(1+T).
- **Measure Bode plot** (`_measure_bode`, toolbar button + mode selector + Stop): swept sine from `asg1` recorded with
  the scope (2 channels: asg1 and the response), complex amplitude by least squares at the exact frequency
  (`fit_sine`: cos, sin, offset, drift, optional extra tones; optional 1-σ error from the residual). Filled in point
  by point (`ctl.bode`, `bode_seq` → `dcc.Store("bode")` → clientside `BODE_JS`); Stop is a `threading.Event` set
  from the UI thread (the sweep blocks the command queue). When done, `analyze_bode()` writes the auto-analysis
  (shown under the plot, `#bode-analysis`, and in the log). Cleanup: analysis, then mode restored, then `configure()`.
  - `closed loop` (locked only; stop ≤ 0.45 × dither): asg1 → OUT1 sum, amplitude = `na_amp_frac` × linewidth.
    Feedback c from pid0, or from OUT1 − x when D is on (pid1 also feeds back; dither tones fitted out). G = −T/(1+T),
    S = 1+T, closed loop = −T, plant = (e/x)/S. Points with G uncertain > 25 % are hollow and left out of margins.
    Stops and holds if the PID reaches the clamp. Low frequencies need excitation (1 + c/x is small): 0.03 linewidth
    gave ±20 % at 4 Hz on the fake, 0.1 gave ±3 %; with D, 0.1 left points noise-limited, 0.5 matched the model.
  - `circuit only` (any mode, OUT1 untouched, asg1 never routed out; any frequency, region above dither/2 shaded):
    copies on spare modules driven by asg1, each measured on its own: `iq1` at 0 Hz (lock-in at baseband), and one
    spare PID (pid2, or pid1 when the band-pass holds pid2; refused if locked with band-pass and D) reconfigured per
    point as P + I, D, band-pass. Auto-ranged per point (amplitude, and copy gain × K read back) so responses stay
    far above the 14-bit step; < 5 steps → hollow. Open loop = lock-in × (P+I+D) × band-pass baseband equivalent.
- UI:
  - The server sends compact data in `dcc.Store("frame")`.
  - A **clientside** callback (`SCOPE_JS`) draws with `Plotly.react` using **SVG `scatter`**. `scattergl` redraws about 2× slower here.
  - The noise and loop figures have **their own callbacks keyed on `meas-seq`**, never in the tick response. Putting them in the tick response froze the page.
  - Numeric inputs use `step="any"` (the integer gain uses step 1).
  - The diagram: blocks are absolutely positioned Dash components on a fixed 1340×515 canvas; every `s-{key}` input
    exists exactly once (inline in a box or in a hidden popover), so the version-checked apply callback is unchanged.
    `DIAG_JS` (clientside) draws the SVG wires once and updates readback text, switch pills and probe badges from
    `dcc.Store("diag")`, which the tick sends only when it changes. `POP_JS` opens/closes popovers.
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
- Every module with `output_direct="out1"` is summed digitally into OUT1, then clipped to ±1 V.

## Testing without hardware
- `app/test/fake/pyrpl.py` is a stand-in `pyrpl` module.
  - It simulates a cavity with 3 modes, PZT lag on the dither, the IQ demodulator with n-pole low-pass, a PyRPL-style quantized PID with clamps, the scope, the ASG and a NetworkAnalyzer.
  - Environment variables: `FAKE_DT`; `FAKE_LAN_S` emulates the per-register LAN delay; `REG_COUNT` counts register accesses.
  - The fake dither is 4 % larger than requested (DITHER_SCALE 1.04) to exercise the amplitude calibration.
- Run these from `app/test`:
  - `python test_v3.py` is the controller test. It runs acquire → pick peak → auto-phase → lock with a different lock dither → noise → loop → kick and relock → unlock → lock from held → acquire → sawtooth.
  - `test_v3.py` also checks the schematic: every FIELDS key has exactly one home, every block parameter changes its
    register on the fake (and the readback), all switches and interlocks, probe placement, crosshair targeting.
  - `python serve_v3.py` serves the app on the fake at port 8070, then `python ui_v4.py` runs the Playwright browser test:
    edits every block parameter through its own input, flips every switch (incl. the Scan-while-locked confirm),
    places probes, clicks a crosshair, locks with the switch, runs both measurements, redraws/s, screenshots.
  - `tick_timing.py`, `render_timing.py`, `prof_tick.py` and `bench_v2style.py` are performance probes.
- Bode tests: `test_v3.py` checks that the circuit-only sweep matches its model (< 5 %, < 5°), that asg1 is never
  routed to an output during it, that the closed-loop G equals the fake's true loop gain below 100 Hz (< 5 %), and Stop.
  `ui_v4.py` checks that the plot fills in point by point, both modes complete, and Stop.
- The fake scope records **after** it is armed (untriggered), like the hardware; it used to return the past window.
- Results 2026-11-02, band-pass / D / analysis: `test_v3.py` 94/94; `ui_v4.py` 66/68 (the 2 redraw-rate checks fail on
  this container for old and new code alike: A/B 4–6/s; tick 7.0–8.5 ms new vs 6.0–9.5 ms previous commit).
- Results 2026-11-02 (Bode added, a slower container): `test_v3.py` 63/63; `ui_v4.py` 56/58 functional checks pass,
  the 2 redraw-rate checks fail on that host for the old and the new code alike (A/B: 5–6/s both; tick 7.0 ms old vs
  7.5 ms new there).
- Last results (v4, 2026-11-01, Linux sandbox, fake):
  - `test_v3.py`: 51/51 checks pass. `ui_v4.py`: 52/52 checks pass.
  - Lock, noise, loop (UGF 9.5–12.5 Hz, phase margin 87°), relock and lock-from-held are OK.
  - The browser redraws 7.6/s scanning and 10/s locked (same as v3).
  - Tick callback 3.0 ms incl. frame JSON (`prof_tick.py`); diagram state + hash 0.06 ms.

## Pitfalls already hit
- PyRPL 0.9.9.1's FPGA has **no PID derivative** (`DERIVATIVE = 0` in red_pitaya_pid_block.v; `pid.d` commented out).
- iq0/iq1 have 1 input-filter stage (`INPUTFILTERSTAGES = 1`) and 2 low-pass stages; PIDs have 4 input-filter stages
  (`FILTERMINBW = 10`); iq2 has 4 low-pass stages. Filter latency: low-pass 2 clocks, high-pass 1 clock per stage.
- The fake loop runs at 2 kHz (`FAKE_DT` 0.5 ms): D corners near 1 kHz are beyond what it can simulate.
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

## Style the user likes
Direct, physics-literate explanations; settings shown in real units; values on screen equal what the hardware uses;
robust defaults (auto phase, auto relock, clamps from the mode spacing); no silent fallbacks.
