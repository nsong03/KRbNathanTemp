# History of the project (what was tried, in order)

1. **SR810 + DG922 + Red Pitaya notebook** (`earlier/notebook/`).
   - Setup: DG922 CH1 sent a 1 kHz, 40 mVpp dither into Red Pitaya IN2, and `pid1` (×0.1) passed it to OUT1. DG922 CH2 provided the SR810 reference. The SR810 X output went to IN1, and `pid0` integrated it.
   - Two-part workflow: calibrate (V0, slope S, linewidth, clamp from the mode spacing), then lock (gain steps 1, 2, 5, 10 Hz, then watch and relock).
   - The guide is the claude.ai doc "Cavity PZT Dither Lock — Calibration & Locking Guide" (<https://claude.ai/artifact/BNnm1itnuL6EM1scPooKrk>). `cavity_lock_cli_with_fake.py` is the script version.
2. **DG922 arbitrary waveforms** (`earlier/dg922/`): a triangle sweep plus a sine dither in one channel, with a matching square reference in Advanced mode. These were used before the Red Pitaya generated the dither itself.
3. **Multi-peak simulation** (`earlier/multipeak_sim/`): what the error signal looks like when the sweep crosses several modes, and how much a neighboring mode shifts the lock point.
4. **App v1**: the browser app replaced the notebook. The Red Pitaya now does everything: `iq0` makes the dither and demodulates, `asg0` makes the ramp, and `pid0` locks. The SR810 and DG922 are no longer needed.
5. **App v2**: the user's redesign.
   - Acquisition mode first: a 20 Hz, 500 mVpp ramp with a 1 kHz, 30 mVpp dither; all panels visible with toggles; "Auto-optimize phase"; click a peak to target it.
   - Then LOCK! with its own controllable dither, showing the photodiode, error, setpoint and setpoint history.
   - A component-level diagram of the FPGA chain was drawn; the text version is in `docs/pyrpl_reference.md`.
   - A Windows `ModuleNotFoundError: pyrpl` turned out to be the wrong VS Code interpreter.
   - User verdict: "It locks and is robust."
6. **App v3** (current): speed and diagnostics.
   - Faster browser drawing and server tick.
   - *Measure noise* and *Measure loop*, and a loop model with the predicted phase margin.
   - Verilog latency simulation package (`fpga/`) and a component noise comparison against commercial RF and lock-in parts.
   - Findings: `docs/speed_latency_noise_study.md`, an export of the claude.ai doc <https://claude.ai/artifact/HjMYekzFVDwpCB3jPw6qkP>.
7. **App v4** (Claude Code, 2026-11-01): a Moku-style schematic UI. Every block is a box with its register readback and
   settings, generated from one model (`app/schematic.py`) that also does the hardware writes. Switches for Scan, Dither,
   Lock, Output (new `off` mode) and Auto-relock; 2 probe points feed the embedded scope; Lock Assist crosshairs at the
   error zero crossings; new setting `setpoint_mv` (`pid0.setpoint`). No IIR band-pass (user decision).
   Plan: `docs/schematic_plan.md`. The Moku manual PDFs could not be fetched from that session (network policy);
   the design follows `moku_reference.md`.

## Notes on provenance
- The latency numbers come from an earlier run of `run_sim.py`. A later sandbox refused to run downloaded Verilog, so it was not re-run. The 9- and 12-clock results match a hand count of the Verilog. Re-run it on Windows with Icarus to confirm.
- All app tests ran against `app/test/fake/pyrpl.py`, not hardware. The user verified v2 locking on the real Red Pitaya; v3 has not been reported on hardware yet.
