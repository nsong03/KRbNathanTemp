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
  - Applies are version-checked: `("apply", (values, version))`.
- `configure()` writes `iq0`, `pid1` and the input filters.
  - `_snap_to_hardware()` reads back what the FPGA really uses (filter corners on a factor-2 ladder, integer `quadrature_factor`) and shows those values in the UI.
- Modes: `connecting` → `acquire` ↔ `locking` → `locked` / `held`, plus `measuring` and `off` (Output switch).
  - `_lock()` → `_zoom_and_lock()`: a slow sweep across the chosen peak with the lock dither, then a 4-phase cos² fit of the phase, then the zero crossing and slope.
  - `_engage()`: parks at the zero crossing, then ramps the gain in thirds (ugf/10, ugf/3, ugf).
  - `_live_tick()`: the watchdog. `_relock()`: automatic relock.
- Loop gains: `pid0.i = -ugf/S` and `pid0.p = -p_gain/S`, where S is the measured error slope per OUT1 volt. Then |i·S| equals the loop bandwidth, and the minus sign gives negative feedback whatever the sign of S.
- Diagnostics while locked:
  - `_measure_noise()`: Welch spectra in two bands, with band rms.
  - `_measure_loop()`: PyRPL network analyzer on `pid0` and `iq0`. With T = c/x, G = −T/(1+T) and P = (e/x)/(1+T).
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
- Last results (v4, 2026-11-01, Linux sandbox, fake):
  - `test_v3.py`: 51/51 checks pass. `ui_v4.py`: 52/52 checks pass.
  - Lock, noise, loop (UGF 9.5–12.5 Hz, phase margin 87°), relock and lock-from-held are OK.
  - The browser redraws 7.6/s scanning and 10/s locked (same as v3).
  - Tick callback 3.0 ms incl. frame JSON (`prof_tick.py`); diagram state + hash 0.06 ms.

## Pitfalls already hit
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
