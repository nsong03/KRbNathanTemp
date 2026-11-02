# Cavity lock — hand-off package (2026-10-31)

Everything from the claude.ai project "lock a cavity PZT to the laser with a Red Pitaya", packaged for Claude Code.

## Start here
1. Unzip this folder next to your project, for example into `...\redpitaya_locking\cavity_lock_handoff`.
2. Open the folder in Claude Code. `CLAUDE.md` loads automatically as project memory.
3. Paste the prompt from `CLAUDE_CODE_PROMPT.md`, the schematic / Moku-style UI task.

## Status
- **v4** (2026-11-01): the settings sidebar is now a Moku-style schematic of the real FPGA chain, with switches
  (Scan, Dither, Lock, Output, Auto-relock), probe points (2 scope channels) and Lock Assist crosshairs.
  Plan and register map: `docs/schematic_plan.md`; screenshots: `docs/screenshots/`. Tested on the fake only.
- **Measure Bode plot** (2026-11-02): swept-sine Bode with the internal generator (asg1) and scope, filled in point
  by point, with Stop. *closed loop* (locked): G, disturbance suppression S, closed-loop T and the plant, with the
  model, UGF and phase margin. *circuit only*: the lock-in chain and PID measured on spare copies of the blocks,
  OUT1 untouched. Tested on the fake only.
- `app/cavity_lock_app.py` **v3** added speed and diagnostics:
  - v2 locks robustly on the real hardware.
  - v3 adds speed (about 10 redraws/s), *Measure noise*, *Measure loop* and the loop model. It is tested against the fake; check it on hardware.
- The loop is limited by the lock-in low-pass, which is tied to the dither frequency, and by the 10 Hz default loop bandwidth. The FPGA (about 0.2 µs) and the electronics noise are not the limit.

## Contents
| Path | What |
| --- | --- |
| `CLAUDE.md` | Project memory for Claude Code: hardware, run command, architecture, verified PyRPL facts, tests, pitfalls |
| `CLAUDE_CODE_PROMPT.md` | Ready-to-paste prompt for the next task (schematic block-diagram UI) |
| `app/cavity_lock_app.py` | The app (analysis + Controller). Run: `python app\cavity_lock_app.py --host rp-f0fa92.local` |
| `app/schematic.py` | The block diagram as data: blocks, parameter → register map, writers, wires, switches, probes |
| `app/ui.py` | Dash UI: schematic canvas, popovers, embedded scope |
| `app/test/` | `fake/pyrpl.py` hardware stand-in; `test_v3.py` controller + schematic test; `serve_v3.py` + `ui_v4.py` Playwright UI test; timing probes |
| `docs/schematic_plan.md` | Schematic UI plan: blocks → FIELDS keys → PyRPL attributes, switches and interlocks, probe map, layout |
| `docs/findings.md` | One-page summary of all findings |
| `docs/speed_latency_noise_study.md` | The full study: latency table, Verilog results, component noise vs commercial parts, next measurements |
| `docs/pyrpl_reference.md` | FPGA signal chain at component level, module attributes, probe-able signals, limits |
| `docs/moku_reference.md` | Moku Laser Lock Box and Lock-in UI conventions, and how each Moku block maps onto this Red Pitaya build |
| `docs/history.md` | What was tried, in order, and provenance notes |
| `fpga/redpitaya_latency_sim/` | Icarus Verilog latency simulation of PyRPL's `red_pitaya_dsp`: `fetch_rtl.py`, `run_sim.py`, testbench and example output |
| `earlier/notebook/` | The first approach, an SR810 + DG922 + Red Pitaya calibration and lock notebook |
| `earlier/dg922/` | DG922 arbitrary-waveform files (ramp + dither) and generators |
| `earlier/multipeak_sim/` | Error signal and lock-point shift with neighboring cavity modes |

## Quick test without hardware
```
cd app\test
python test_v3.py                              # controller end to end on the fake
python serve_v3.py   (then, in a 2nd terminal)  python ui_v4.py   # browser test, needs: pip install playwright; playwright install chromium
```

## Online copies
- Study doc: <https://claude.ai/artifact/HjMYekzFVDwpCB3jPw6qkP>
- Calibration & locking guide for the earlier SR810 approach: <https://claude.ai/artifact/BNnm1itnuL6EM1scPooKrk>
