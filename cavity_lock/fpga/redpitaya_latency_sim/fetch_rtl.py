#!/usr/bin/env python3
"""
fetch_rtl.py -- get PyRPL's FPGA Verilog (the code inside the bitstream PyRPL uploads to the Red Pitaya)
and prepare a simulation copy.

  rtl/      the original files, downloaded from github.com/pyrpl-fpga/pyrpl at tag 0.9.9.1
  rtl_sim/  the same files with simulation-only edits (listed below). Both make the simulator do what
            the synthesized FPGA does; neither changes the hardware's behaviour or timing.

  1. red_pitaya_iq_fgen_block.v: the sine look-up table has 2^11 entries but is indexed with a 17-bit
     phase. Synthesis keeps the low 11 bits; a simulator returns X for an out-of-range index, which kills
     the oscillator half of the time. Edit: index with phase[LUTSZ-1:0].
     Also: the phase accumulator has no reset; the FPGA powers it up at 0, a simulator at X (which would
     poison every filter downstream). Edit: initialise it to 0 (and, in red_pitaya_iq_block.v, the
     frequency register it adds, which is reset on the same clock edge the accumulator first uses it).
  2. red_pitaya_pid_block.v: with DERIVATIVE=0 the module-level reg kd_reg_s is never assigned. The FPGA
     powers it up at 0; a simulator starts it at X and every PID output becomes X. Edit: initialise it to 0.

Usage:  python fetch_rtl.py                 (download)
        python fetch_rtl.py --from DIR      (copy from a local PyRPL checkout's pyrpl/fpga/rtl instead)
"""
import argparse
import os
import shutil
import sys
import urllib.request

TAG = "0.9.9.1"
URL = f"https://raw.githubusercontent.com/pyrpl-fpga/pyrpl/{TAG}/pyrpl/fpga/rtl/"
FILES = ["red_pitaya_dsp.v", "red_pitaya_pid_block.v", "red_pitaya_filter_block.v", "red_pitaya_lpf_block.v",
         "red_pitaya_trigger_block.v", "red_pitaya_iq_block.v", "red_pitaya_iq_fgen_block.v",
         "red_pitaya_iq_demodulator_block.v", "red_pitaya_iq_modulator_block.v", "red_pitaya_product_sat.v",
         "red_pitaya_saturate.v", "red_pitaya_pfd_block.v", "red_pitaya_top.v"]
PATCHES = {
    "red_pitaya_iq_fgen_block.v": [(f"lutrom[phase{k}]", f"lutrom[phase{k}[LUTSZ-1:0]]") for k in (1, 2, 3, 4)]
                                  + [("reg [PHASEBITS-1:0] phase;",
                                      "reg [PHASEBITS-1:0] phase;\n"
                                      "initial phase = 0;  // SIMULATION EDIT: the FPGA powers the accumulator up at 0")],
    "red_pitaya_iq_block.v": [("reg [PHASEBITS-1:0] shift_phase;",
                               "reg [PHASEBITS-1:0] shift_phase;\n"
                               "initial shift_phase = 0;  // SIMULATION EDIT: power-up value before the first reset edge")],
    "red_pitaya_pid_block.v": [("reg signed  [39-DSR  : 0] kd_reg_s      ;",
                                "reg signed  [39-DSR  : 0] kd_reg_s      ;\n"
                                "initial kd_reg_s = 0;  // SIMULATION EDIT: the FPGA powers this unassigned reg up at 0")],
}
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", help="local folder with PyRPL's rtl files")
    a = ap.parse_args()
    raw, sim = os.path.join(HERE, "rtl"), os.path.join(HERE, "rtl_sim")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(sim, exist_ok=True)
    for f in FILES:
        dst = os.path.join(raw, f)
        if a.src:
            shutil.copyfile(os.path.join(a.src, f), dst)
        else:
            print(f"downloading {f}")
            with urllib.request.urlopen(URL + f, timeout=30) as r, open(dst, "wb") as out:
                out.write(r.read())
    for f in FILES:
        text = open(os.path.join(raw, f), encoding="utf-8", errors="replace").read()
        for old, new in PATCHES.get(f, []):
            n = text.count(old)
            if n != 1:
                sys.exit(f"patch for {f} did not match exactly once ({n}x): {old!r}. Has PyRPL changed?")
            text = text.replace(old, new)
            print(f"rtl_sim/{f}: {old}  ->  {new.splitlines()[0]}")
        with open(os.path.join(sim, f), "w", encoding="utf-8") as out:
            out.write(text)
    print(f"done: rtl/ (original, PyRPL {TAG}) and rtl_sim/ (simulation copy)")


if __name__ == "__main__":
    main()
