# Cavity lock — hand-off package (2026-10-31)

Everything from the claude.ai project "lock a cavity PZT to the laser with a Red Pitaya", packaged for Claude Code.

## Start here
1. Unzip this folder next to your project, for example into `...\redpitaya_locking\cavity_lock_handoff`.
2. Open the folder in Claude Code. `CLAUDE.md` loads automatically as project memory.
3. Paste the prompt from `CLAUDE_CODE_PROMPT.md`, the schematic / Moku-style UI task.

## Wiring (Red Pitaya STEMlab 125-14)
| Port | Connection | Feature |
| --- | --- | --- |
| IN1 | cavity transmission photodiode (LV jumper below 1 V, else HV + "IN1 scale" 20) | cavity lock |
| OUT1 | PZT driver MOD IN (ramp + dither, or setpoint + dither; never beyond ±0.95 V) | cavity lock |
| IN2 | AOM pickoff photodiode, 1st-order beam (LV jumper, ~0.5–0.8 V at the operating point) | intensity servo |
| OUT2 | 50 Ω tee → inverting bias driver → mixer IF → RF amp → AOM (0 V = bias level) | intensity servo |

OUT2 is no longer a scope monitor. The intensity hardware is described in `docs/intensity_hardware.md` (not built yet).

## Second app: error signal + PZT summer for an external servo box
`app/errsig_app.py` (port 8060). IN1 ← photodiode, **OUT1 → servo box** (error signal × gain + offset),
**IN2 ← servo box** correction (HV jumper for ±15 V), **OUT2 → PZT driver** = IN2 × gain + DC offset + dither, or the
alignment ramp + dither (Ramp toggle). No feedback on the Red Pitaya. Ramp, Auto phase, pick a peak, "Park at peak",
close the loop on the servo box. Run: `python app\errsig_app.py --host rp-f0fa92.local`.

### PI servo for an external error signal (`app/pi_app.py`, port 8075)
IN1 and OUT1 only. IN1 ← your error signal; OUT1 = offset + PI (pid0, raw P and I as you type them, setpoint,
output clamps) or, with the Ramp toggle, a ramp around the offset for acquiring the error signal (PI off). Optional
input filter in front of the PI (4 first-order stages, > 0 low-pass, < 0 high-pass), off by default. The scope shows
IN1 and OUT1 (ramp: vs time or IN1 vs OUT1). Run: `python app\pi_app.py --host rp-f0fa92.local`.

### Dual-phase lock-in PI + intensity PI (`app/lockin_pi_app.py`, port 8085)
Lock on IN1/OUT1: dual-phase lock-in on `iq2` (X and Y, phase set by hand), raw PI (`pid0`, input X or Y), ramp /
offset (`asg0`), dither on OUT1. Intensity on OUT2: raw PI (`pid2`, input IN2 by default, IN1 selectable). Each PI
has raw P, I, setpoint, output clamps and a 4-stage input filter switched by its Filter toggle (off by default).
Nothing is calculated for you. Run: `python app\lockin_pi_app.py --host rp-f0fa92.local`.

### Spin-off: 2-phase lock-in (`app/errsig2ph_app.py`, port 8065)
Same wiring and workflow, but the demodulation is a dual-quadrature lock-in on `iq2` (the only PyRPL IQ module with
both quadratures wired out): X = `iq2`, Y = `iq2_2`, shared low-pass (up to 4 stages) and gain. The scope shows X, Y
and R; θ (the error signal's angle in the X–Y plane) is read from one sweep, and Auto phase rotates it into X in one
step. In servo mode θ is estimated live from the residual error fluctuations. OUT1 can carry X or Y.
Run: `python app\errsig2ph_app.py --host rp-f0fa92.local`.

## Status
- **v5 — AOM intensity stabilization** (2026-11-02): a second feature on IN2/OUT2 (`pid2` servo, `asg1` Bode test sine).
  Two icons at the top left switch between the *Cavity lock* and *Intensity* views; each icon has a status dot, both
  loops always run in hardware and share the scope (scheduled). Intensity: swept-sine Bode plot (open loop: the plant and
  its gain K; locked: G, disturbance suppression, closed loop), PI lock from a target bandwidth or P/I dropdowns,
  setpoint adjuster, watchdog (railed, no light + re-engage). No scan. Plan: `docs/intensity_plan.md`. Tested on the fake only.
- **v4** (2026-11-01): the settings sidebar is now a Moku-style schematic of the real FPGA chain, with switches
  (Scan, Dither, Lock, Output, Auto-relock), probe points (2 scope channels) and Lock Assist crosshairs.
  Plan and register map: `docs/schematic_plan.md`; screenshots: `docs/screenshots/`. Tested on the fake only.
- **Measure Bode plot** (2026-11-02): swept-sine Bode with the internal generator (now asg0) and scope, filled in point
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
| `app/ui.py` | Dash UI: the two views (schematic canvases, popovers, embedded scopes), view icons |
| `app/intensity.py` | AOM intensity servo: settings, Bode measurement, controller (pid2 / asg1) |
| `app/intensity_schematic.py` | The intensity block diagram as data (blocks, registers, writers, wires, switches, probes) |
| `docs/intensity_plan.md` | Intensity plan: module allocation, settings, modes, watchdog, scope schedule, layout |
| `docs/intensity_hardware.md` | Intensity hardware: photodiode, bias driver, mixer, RF chain, what it means for the software |
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
python test_intensity.py                       # intensity servo on the fake
python test_isolation.py                       # both loops at once: register isolation, scope schedule
python serve_v3.py   (then, in a 2nd terminal)  python ui_v4.py ; python ui_v5.py   # browser tests (pip install playwright; playwright install chromium)
```

## Online copies
- Study doc: <https://claude.ai/artifact/HjMYekzFVDwpCB3jPw6qkP>
- Calibration & locking guide for the earlier SR810 approach: <https://claude.ai/artifact/BNnm1itnuL6EM1scPooKrk>
