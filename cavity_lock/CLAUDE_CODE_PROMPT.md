# Prompt for Claude Code — schematic (Moku-style) UI for the cavity lock app

Paste everything below the line into Claude Code, opened in the unzipped `cavity_lock_handoff` folder.

---

I'm continuing a project from a claude.ai chat. Read `CLAUDE.md`, `README.md` and everything in `docs/` first. They describe the hardware, the working app (`app/cavity_lock_app.py`, v3), verified PyRPL facts, test tooling, and the pitfalls already hit. Then read the Moku manuals linked in `docs/moku_reference.md`, at least the Laser Lock Box manual (block diagram, switches, probes, Lock Assist, lock stages) and the Lock-in Amplifier manual. Use WebFetch.

## Goal
Turn the app's settings sidebar into a **schematic, Moku-style block diagram** of the real signal chain. The user should be able to:
- adjust the knobs on each block to optimize the error signal;
- flip switches to scan for the error signal and then lock.

Every element is a box on the diagram with a label and its own editable settings:
- the dither generator;
- the ramp (scan);
- the band-pass filter;
- the amplifier;
- the low-pass filter;
- the mixer;
- the phase delay;
- the PID / integrator;
- the output summer and limiter.

**The actual programming must reflect the diagram.** What the diagram shows is exactly what the FPGA does, and every control writes the register it is drawn on.

## The chain to draw (this is what the hardware really does — see docs/pyrpl_reference.md)
```
IN1 (photodiode) ─▶ [Band-pass / AC: iq0.acbandwidth high-pass, 1st order] ─▶ (×) Mixer ─▶ [Low-pass: iq0.bandwidth, 1–2 stages]
                                                                                   ▲
                       [Dither generator: iq0 NCO, freq + amplitude] ─▶ [Phase delay: iq0.phase] ┘
     ─▶ [Amplifier: iq0.quadrature_factor, integer] ─▶ ●error (iq0) ─▶ [Setpoint: pid0.setpoint] ─▶ [PID: pid0 P + I (loop bandwidth)] ─▶ [Limiter: pid0 min/max = clamp]
                                                                                                                            │
[Ramp / scan: asg0 triangle|sawtooth, freq, amplitude, center] ───────────────────────────────▶ (Σ) ◀──────────────────────┘
[Dither generator] ───────────────────────────────────────────────────────────────────────────▶ (Σ) ─▶ [OUT1 limit ±0.95 V] ─▶ OUT1 ─▶ PZT driver ─▶ cavity ─▶ photodiode
OUT2 monitor: [pid1 pass-through, source select + gain] ─▶ OUT2
```
- **Draw the dither generator once**, feeding both the mixer (the LO, through the phase delay) and Σ. That is Moku's demodulation source "Modulation".
- The dither has two contexts, acquisition and lock (`lock_same_dither`). Show which one is live.
- **Band-pass.** The hardware has only `iq0`'s input high-pass before the mixer; the low-pass after the mixer makes it a band-pass overall. Label the boxes honestly, for example "Band-pass (AC high-pass)". A true band-pass or notch on IN1 is possible with the `iir` module (in1 → iir → iq0). Add it only if you implement it for real, default off, and show its latency.
- Show external parts (PZT driver, cavity, photodiode) as greyed plant boxes with their display-only settings: `driver_bw_hz`, `kappa_mhz`, `in1_scale`.

## Requirements
1. **One model, two consumers.**
   - Define the schematic as data: blocks, ports, wires, parameters and switches. Map each parameter to an existing `FIELDS` key and the PyRPL attribute it writes.
   - Generate both the drawing and the hardware writes from that model.
   - Add a test asserting that every hardware-affecting `FIELDS` key belongs to exactly one block, and that every block parameter changes the matching register on the fake.
2. **Show hardware truth.**
   - Each box shows the value the FPGA really uses, read back through `_snap_to_hardware()`. Examples: "LPF 250 Hz → 303.6 Hz", integer gain.
   - Expose only settings the hardware has: low-pass order 1 or 2, 1st-order stages on a factor-2 ladder, never Butterworth-style types; integer gain; 1 high-pass stage; I given as the loop unity-gain frequency; the `i` resolution warning.
3. **Switches** on the diagram, like Moku's output and enable switches:
   - **Scan**: `asg0` on/off.
   - **Dither**: `iq0.output_direct` on/off.
   - **Lock**: runs the existing lock sequence — zoom, phase re-fit, zero crossing, park, gain ramp. Turning it off unlocks and holds.
   - Optionally **Output** (OUT1 enable) and **Auto-relock**.
   - Define sane interlocks. Lock on turns Scan off, like Moku's "Disable scan" stage. Scan on while locked asks or unlocks first. The switch states always mirror `ctl.mode` and the real registers.
4. **Probes.**
   - Clickable probe points on wires, at most 2 at a time because the Red Pitaya scope has 2 channels, feeding the live scope panel.
   - Offer only points that are real DSP signals: `in1`, `iq0` (error), `asg0` (ramp), `pid0` (PID output), `out1`, `out2`. Grey out the rest (after the high-pass, raw mixer output) with a tooltip saying why.
   - Keep the current panels — transmission vs error with the clickable peaks, drive, setpoint history, noise spectrum, loop Bode — as the "embedded scope" area.
5. **Lock Assist.** Keep click-a-peak-to-target and "Use tallest peak". Also mark the error zero crossings on the scan, like Moku's crosshairs, and let a click on one target that peak.
6. **Keep everything that works:**
   - all features, settings keys and Save/Load compatibility;
   - auto-phase, the watchdog and auto-relock;
   - noise and loop measurements, and the loop model with the predicted phase margin;
   - OUT2 monitor and hardware snapping;
   - speed: at least 7 redraws/s acquiring and 10/s locked, with a server tick of a few ms;
   - the Controller-in-main-thread architecture and the version-checked applies.
   - Avoid the Dash pitfalls listed in `CLAUDE.md`.
7. **Implementation freedom.**
   - Suggested: blocks as absolutely positioned Dash components on a fixed canvas, so native inputs keep working, with wires in an SVG layer drawn by a small clientside callback. Click a block to open a compact settings popover, or show 1–3 key knobs inline with the rest in the popover.
   - Keep `python app\cavity_lock_app.py --host rp-f0fa92.local` working. Splitting into a small package is fine.
   - Any new dependency must pip-install cleanly on Windows.

## Process
1. Before building the UI, write `docs/schematic_plan.md`: the block list with each parameter → `FIELDS` key → PyRPL attribute, the switch and interlock logic, the probe map, and an ASCII sketch of the layout. Show it to me and wait for my OK.
2. Implement, then test against the fake (`app/test/fake/pyrpl.py`; extend it if needed, e.g. setpoint or iir).
   - Update `test_v3.py`.
   - Write a Playwright test like `ui_v3.py` that edits every block parameter, flips every switch, places probes, locks via the switch, runs both measurements, measures redraws per second, and saves screenshots. Look at the screenshots yourself and fix layout problems.
3. **Do not touch the real Red Pitaya** unless I say so. It drives a PZT. When I do, start with a read-only connection check.
4. Finish with a short summary, the Windows run command, and the screenshots.
