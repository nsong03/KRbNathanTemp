# Red Pitaya (PyRPL) latency simulation

The Red Pitaya FPGA code that PyRPL uploads is written in **Verilog**, not VHDL.
This folder simulates that code with Icarus Verilog, a free Verilog simulator. It measures, clock cycle by clock cycle, how long a signal takes from IN1 to OUT1 along the paths `cavity_lock_app.py` uses. It also measures how fast the lock-in's error signal responds with the app's dither and filter settings.

The simulation runs the real `red_pitaya_dsp` module. That includes PyRPL's routing, the output summing tree, the IQ (lock-in) block and the PID block, all at PyRPL tag 0.9.9.1. Nothing is re-implemented.

## Setup (Windows)

1. Install Icarus Verilog from <https://bleyer.org/icarus/>. During installation, tick "Add executable folder(s) to the user PATH". The installer also includes GTKWave for viewing waveforms.
2. Open a new terminal in this folder and run:

```
python fetch_rtl.py      # downloads PyRPL's Verilog into rtl\ and makes rtl_sim\
python run_sim.py        # about 2 minutes; add --fast to skip the lock-in runs
```

Options:
- `--settings ..\cavity_lock_settings.json` simulates your saved app settings.
- `--driver-bw 1000` sets the small-signal bandwidth of your PZT driver with the PZT connected.
- `--vcd` writes `pid_p.vcd` and `iq_unfiltered.vcd`, which you can open with `gtkwave pid_p.vcd`.

matplotlib is optional. If it is installed, `run_sim.py` also writes `lockin_step_response.png`.

## What it measures

| Experiment | Path | Result (app defaults) |
|---|---|---|
| `pid_p` | IN1 → PID0 (P = 1, no filters) → OUT1 | **9 cycles = 72 ns** inside the FPGA logic |
| `iq_unfiltered` | IN1 → IQ0 demodulator (filters off) → PID0 → OUT1 | **12 cycles = 96 ns** |
| delays outside the logic | ADC pipeline (6 cycles), FPGA I/O registers (2), output DDR stage (1), DAC latch (1), filters and settling (~35 ns) | total about 187 ns (PID path) and 211 ns (lock-in path) |
| `lockin_app` | the lock-in as the app sets it up: 1 kHz dither, 76 Hz high-pass, 2 × 304 Hz low-pass | error reaches 50 % after **0.81 ms** and 90 % after **1.9 ms** |
| `lockin_fast` | 20 kHz dither, 2 × 2.4 kHz low-pass | 50 % after 0.11 ms and 90 % after 0.25 ms |

The ~190 ns analog-in to analog-out figure agrees with published measurements on Red Pitaya PID loops, which report about 200 ns.

The key point is that the FPGA's own delay is about **4000 times shorter** than the delay of the lock-in's low-pass filter. The filter, together with the PZT driver and the PZT itself, is what limits how fast the lock can be. `run_sim.py` ends with a phase budget that shows this at candidate loop bandwidths.

## Simulation-only edits (`rtl_sim/`)

`fetch_rtl.py` keeps the original files in `rtl/` and writes a copy to `rtl_sim/` with these edits. Each one makes the simulator behave the way the synthesized FPGA already does.

- `red_pitaya_iq_fgen_block.v`: the sine table index is cut to 11 bits. Synthesis does this implicitly; a simulator returns X for an out-of-range index instead. The oscillator's phase accumulator also starts at 0, which is the FPGA power-up value.
- `red_pitaya_iq_block.v`: the frequency register starts at 0, its power-up value before the first reset edge.
- `red_pitaya_pid_block.v`: `kd_reg_s`, which is never assigned when the derivative gain is disabled, starts at 0 as it does in the FPGA.

The IIR module is replaced by `iir_stub.v`. The real module contains simulation debug code that Icarus rejects, and the lock does not use it.

## Files

- `fetch_rtl.py`: downloads the Verilog and writes the simulation copy.
- `run_sim.py`: computes register values the way PyRPL does, runs the experiments and prints the results.
- `tb_latency.v`: the testbench, configured by a `tb_config.vh` that `run_sim.py` generates for each run.
- `iir_stub.v`: the stand-in for the IIR module.
- `example_output/`: the output of a full run with the app's default settings.
