# Schematic (Moku-style) UI: plan

Status: **approved** (2026-11-01): no IIR; Output switch and `off` mode in; layout as sketched.

Note on sources: the Moku manual PDFs (download.liquidinstruments.com, photonics.laser2000.co.uk) are outside this
session's allowed network domains, so this plan relies on the summary in `moku_reference.md`.

## 1. Principle: one model, two consumers

A new module `app/schematic.py` holds the diagram as data:

- `BLOCKS`: id, label, kind (`dsp` / `node` / `plant`), position and size on a fixed canvas, the parameters it
  owns, and a `write(ctl)` function that writes exactly the registers drawn on that block.
- `WIRES`: polylines between block ports, each with an `active` rule evaluated from the register readback
  (for example the asg0 → Σ wire is drawn live only when `asg0.output_direct == "out1"`).
- `SWITCHES`: sit on wires; each has a state rule (from registers and `ctl.mode`) and a command.
- `PROBES`: points on wires; each is a real scope signal or greyed out with the reason.

Both consumers read this model:

- **Hardware:** `configure()` becomes "call `write(ctl)` of every static block". An apply finds the blocks that own
  the changed keys and calls only their `write()`. Context-dependent blocks (ramp, PID, limiter) write only in the
  mode where the register is live; see the table. The lock sequence (`_zoom_and_lock`, `_engage`, `_relock`) keeps
  its transient writes; they use the same block writers where possible (`PID.write`, `Limiter.write`).
- **Drawing:** the Dash layout builds boxes, switches and probe points from the same model. A clientside callback
  draws the wires as SVG and fills each box's readback line.

Every box shows **hardware truth**: `ctl.hw` is a readback of the registers, taken after every command that wrote
something (about 20 reads ≈ 5 ms, never inside the fast tick), plus `ctl.req` (what the user typed before snapping).
For example "LPF 250 → 303.6 Hz ×2". `s` keeps the snapped values, as now, so Save files are unchanged.

## 2. Blocks, parameters, FIELDS keys and registers

Units are UI units as in `FIELDS`. "When" = the condition under which the block writes the register.

| Block (label on diagram) | Parameter | FIELDS key | PyRPL attribute | When | Shown on the box (readback) |
| --- | --- | --- | --- | --- | --- |
| **Ramp / scan** (asg0) | shape | `ramp_shape` | `asg0.waveform` (`ramp` = triangle, `halframp` = sawtooth) | Scan on | `triangle 20 Hz` |
| | frequency | `ramp_hz` | `asg0.frequency` | Scan on | |
| | amplitude | `ramp_mvpp` | `asg0.amplitude` (= pp/2, clipped to OUT1 limit − dither) | Scan on | `−250..+250 mV` (after clipping) |
| | center | `ramp_center_mv` | `asg0.offset` | Scan on | |
| **Dither generator** (iq0 NCO) | ACQ frequency | `acq_dither_hz` | `iq0.frequency` | ctx = acq | `1000 Hz · 30 mVpp (meas 30.0)` and a badge **ACQ** / **LOCK** for the live context |
| | ACQ amplitude | `acq_dither_mvpp` | `iq0.amplitude` (= pp/2/amp_cal) | ctx = acq | |
| | lock = acq | `lock_same_dither` | selects which pair is written | ctx = lock | |
| | LOCK frequency | `lock_dither_hz` | `iq0.frequency` | ctx = lock, not same | (greyed while not live) |
| | LOCK amplitude | `lock_dither_mvpp` | `iq0.amplitude` | ctx = lock, not same | |
| **Phase delay** (LO) | phase | `phase_deg` | `iq0.phase` | always | `+35.0°`; "Auto" button = Auto-optimize phase |
| **Band-pass (AC high-pass)** | high-pass corner | `ac_hz` | `iq0.acbandwidth` | always | `HP 100 → 75.9 Hz, 1st order` (fixed 1 stage, no order control) |
| **Mixer** (×) | — (node) | — | `iq0.input = "in1"`, demod source = Modulation (fixed) | always | `LO = dither (Modulation)` |
| **Low-pass** | corner | `lpf_hz` | `iq0.bandwidth = [f]*order` | always | `250 → 303.6 Hz × 2 stages (12 dB/oct)` |
| | order | `lpf_order` | (same list) — dropdown 1 / 2 only | always | |
| **Amplifier** | gain | `err_gain` | `iq0.quadrature_factor` (integer input, step 1) | always | `×1 (integer)` |
| **Setpoint** | error offset | `setpoint_mv` **(new key, default 0)** | `pid0.setpoint` | always | `0.0 mV ≙ +0.00 mV of drive` (offset / slope when known) |
| **PID (P + I)** | loop bandwidth (I) | `ugf_hz` | `pid0.i = −ugf/S` | locked (else i = 0) | `UGF 10 Hz → i = −0.242 Hz (|i·S| 9.97 Hz)`, PM from the model, **i-resolution warning** if −ugf/S rounds to 0 or is off by > 10 % |
| | P | `p_gain` | `pid0.p = −p/S` | locked | |
| | (state) | — | `pid0.ival` read only | — | `out +14.9 mV` |
| **Limiter** | auto clamp | `clamp_auto` | `pid0.min_voltage`, `pid0.max_voltage` | locked (±0.95 V otherwise) | `+−105.8..+134.2 mV` |
| | half-width | `clamp_mv` | (same) | locked, auto off | |
| **Σ** (node) | — | — | inputs = every module with `output_direct = out1` | — | live inputs listed |
| **OUT1 limit** | — | constant `OUT_LIMIT` | app-side ±0.95 V (DAC saturates at ±1 V) | — | `±0.95 V` |
| **OUT2 monitor** (pid1) | source | `out2_signal` | `pid1.input` / `pid1.output_direct` | always | `error ×1 → OUT2` |
| | gain | `out2_gain` | `pid1.p` | always | |
| *PZT driver* (plant, grey) | bandwidth | `driver_bw_hz` | none (loop model only) | — | `1 kHz (assumed)` |
| *Cavity* (plant, grey) | linewidth | `kappa_mhz` | none (Hz units only) | — | `κ 40 MHz = 5.85 mV` once measured |
| *Photodiode* (plant, grey) | IN1 scale | `in1_scale` | none (scales traces; jumper is set by hand) | — | `×1 (LV)` |

**Non-register keys** stay in `FIELDS` and live in the popover of the switch or panel they configure:

| Where | Keys |
| --- | --- |
| Lock switch popover ("Lock stages") | `refine_phase`, `gain_ramp` |
| Auto-relock switch (watchdog) | `auto_relock` (the switch itself), `unlock_frac`, `unlock_s` |
| Embedded scope header | `averages`, `prominence`, `lock_window_ms`, `panels`, `ui_hz`, `drive_every`, `probe_a`, `probe_b` **(new)** |
| Measurements toolbar popover | `noise_avg`, `na_start_hz`, `na_stop_hz`, `na_points`, `na_amp_frac` |

New keys: `setpoint_mv`, `probe_a`, `probe_b`. Old saved files load unchanged (missing keys keep their defaults,
unknown keys are ignored as now).

**Tests** (`test_v3.py`, extended):
1. Every `FIELDS` key is owned exactly once (block parameter, plant parameter or a non-register home above).
2. Every block parameter has a register; on the fake, changing it through `apply` (in the mode where it is live)
   changes that register, and the readback on the box equals the register.

## 3. Switches and interlocks

Switch states are **derived**, never stored: from `ctl.hw` (register readback) and `ctl.mode`.

| Switch | On the wire | Shown ON when | Turn ON | Turn OFF |
| --- | --- | --- | --- | --- |
| **Scan** | asg0 → Σ | `asg0.output_direct == "out1"` | from held/off: start acquisition (`_start_acquire`). **From locked: a confirm dialog** "Scan on will unlock"; OK → unlock, then acquire | stop the ramp, park OUT1 at the ramp center with pid0 (i = 0) → mode `held` |
| **Dither** | NCO → Σ | `iq0.output_direct == "out1"` | `iq0.output_direct = out1` (amplitude as set) | `iq0.output_direct = off`. **While locked: unlock and hold first** (the error vanishes). Allowed while acquiring (shows the bare transmission) |
| **Lock** | setpoint → PID (loop closure) | mode ∈ {locking, locked, measuring} | runs the existing sequence: scan off, lock dither, zoom sweep, phase re-fit, zero crossing, park, gain ramp (Moku's "Disable scan" + "Enable integrator" stages). Turns Dither on first if it is off. Refused with a log line if there is no target (acquire) and in `off` | `pid0.i = 0`, hold the setpoint → `held` (as "Unlock (hold)" now) |
| **Output** (OUT1 enable) | Σ → OUT1 | mode ≠ `off` | → acquisition | `pid0.i = 0`; `asg0`, `iq0`, `pid0` `output_direct = off` → OUT1 = 0 V, new mode `off` (live traces continue, watchdog idle) |
| **Auto-relock** | on the Limiter → watchdog | `s["auto_relock"]` | set key | set key |

While the switch shows the lock sequence's own zoom sweep, the Scan switch is ON and labelled "lock sweep"; all
switches are disabled during `locking`, `busy` and `measuring` (a click is queued, as now, and runs afterwards).

## 4. Probes (2 at most)

The Red Pitaya scope has 2 channels. Clicking a probe point places it on channel **A** (if free), else **B**, else
it replaces the older one; clicking a placed probe removes it. Stored as `probe_a` / `probe_b` (`None` = empty).

| Probe point | Signal | Status |
| --- | --- | --- |
| IN1 → Band-pass | `in1` | real |
| Band-pass → Mixer | — | grey: "inside iq0; the high-passed input is not routed to the scope" |
| Mixer → Low-pass | — | grey: "raw mixer output is inside iq0; `quadrature_hf` might expose it but re-routes iq0's output (would break the lock) and is unverified" |
| Amplifier → Setpoint (error) | `iq0` | real |
| Setpoint → PID | — | grey: "error − setpoint exists only inside pid0; probe iq0 and subtract the setpoint" |
| Limiter → Σ (PID output) | `pid0` | real |
| Ramp → Σ | `asg0` | real |
| Dither NCO / LO | — | grey: "the bare NCO is not a scope signal; it appears on out1 when ramp and PID are off" |
| Σ → OUT1 | `out1` | real |
| pid1 → OUT2 | `out2` | real |

Capture strategy (keeps the speed): the main capture stays `in1` + `iq0` (Lock Assist and the watchdog need them).
If both probes are in {`in1`, `iq0`}, the probe row reuses that capture at full rate. Otherwise a second capture of
(A, B) runs every `drive_every` traces, as the "drive" capture does now; it replaces the drive capture. The dither
calibration takes its own `out1` + ramp/setpoint capture only while `_cal_checks > 0` (twice after a dither change),
unless the probes already are exactly that pair. Defaults: A = `out1`, B = `asg0` (the old drive view).

## 5. Embedded scope and Lock Assist

Panels unchanged: transmission vs error (clickable peaks), the probe row (replaces "drive"; the saved key `drive`
is kept and relabelled "probes A/B", two y-axes), setpoint history, noise spectrum, loop Bode.
Lock Assist additions:
- `analyze_sweep` also returns every strong error zero crossing (the error swings past ±20 % of its maximum on
  both sides) per sweep direction, with its drive voltage and slope sign.
- They are drawn as crosshair markers on the error row (green = lockable sign, grey = wrong sign) with a vertical
  dotted line. Clicking one sends `("target_v", v)`: the peak nearest that crossing becomes the target.
- Click-a-peak and "Use tallest peak" stay.

## 6. Layout

```
┌ status bar: mode, setpoint, transmission, traces/s ── [Auto phase] [Tallest] [Noise] [Loop] [Save] [Load] ┐
│ canvas 1240×430 px, boxes absolutely positioned; SVG wire layer underneath                                  │
│                                                                                                             │
│   ┌Photodiode┐◄──────┌Cavity κ┐◄──────┌PZT driver┐◄───────────────────────────────────────────────────┐    │ plant (grey)
│   └────┬─────┘       └────────┘       └──────────┘                                                    │    │
│     IN1│•in1                                                                                          │    │
│   ┌────▼─────┐ •  ┌─┐ •  ┌────────┐  ┌───────┐ •iq0 ┌────────┐      ┌───────┐  ┌───────┐ •pid0 ┌─┐   ┌┴───┐ │
│   │Band-pass │───►│×│───►│Low-pass│─►│Amplif.│─────►│Setpoint│─/L/─►│PID P+I│─►│Limiter│──────►│Σ│─/O/►│OUT1│•out1
│   │AC HP     │    └▲┘    │2×304Hz │  │ ×1    │      │ 0 mV   │ Lock │UGF 10 │  │±106mV │ [AR]  └▲┘   │±.95│ │
│   └──────────┘     │     └────────┘  └───────┘      └────────┘      └───────┘  └───────┘        │▲    └────┘ │
│              ┌─────┴─────┐                                                                     ││          │
│              │Phase delay│                                                                     ││          │
│              │  +35.0°   │                                                                     ││          │
│              └─────▲─────┘                                                                     ││          │
│   ┌────────────────┴────┐  •(NCO, grey)                                                       ││          │
│   │ Dither generator    │──────────────────────────────────────────────────────/Dither/────────┘│          │
│   │ ACQ 1 kHz 30 mVpp   │                                                                       │          │
│   └─────────────────────┘                                                                       │          │
│   ┌─────────────────────┐ •asg0                                                                 │          │
│   │ Ramp / scan  tri    │─────────────────────────────────────────────────────────/Scan/────────┘          │
│   └─────────────────────┘                                                                                  │
│   ┌──────────────────────┐ •out2                                                                           │
│   │ OUT2 monitor (pid1)  │────► OUT2                                                                       │
│   └──────────────────────┘                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ embedded scope: [panels ▾] [probe A: out1] [probe B: asg0] [scope settings ▾]                              │
│ transmission / error (crosshairs, peaks) · probes A/B · setpoint history · noise · Bode                     │
│ analysis text | log                                                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```
`/L/` Lock, `/O/` Output, `[AR]` Auto-relock, `•` probe points (grey ones disabled with a tooltip).

Interaction:
- Each box shows its label, the hardware readback and 1–2 key inputs inline (e.g. Low-pass corner + order).
  Clicking the box title opens a compact popover with the rest of its parameters and a one-line explanation.
- Every `s-{key}` input exists exactly once (inline or in a popover; popovers are hidden, not removed), so the
  existing version-checked apply callback keeps working unchanged.
- Wires: live = coloured (signal blue, drive orange, LO purple), inactive = grey dashed. Updated by the clientside
  callback from a small `diag` store that the tick sends only when the readback changes.
- Canvas is fixed-size; below 1260 px window width it scrolls horizontally.

## 7. Code layout

- `app/cavity_lock_app.py`: analysis + Controller (unchanged structure, Controller in the main thread), `main()`.
  Run command unchanged: `python app\cavity_lock_app.py --host rp-f0fa92.local`.
- `app/schematic.py`: the model (blocks, wires, switches, probes, block writers, readback formatting).
- `app/ui.py`: Dash layout and callbacks (`build_app`), re-exported by `cavity_lock_app`.
- No new dependencies (Dash + scipy only).

## 8. Decisions
- **IIR band-pass / notch on IN1:** not added (user decision). The block is labelled "Band-pass (AC high-pass)".
- **Output switch and `off` mode:** included.
