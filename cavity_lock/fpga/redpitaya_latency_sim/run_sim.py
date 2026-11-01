#!/usr/bin/env python3
"""
run_sim.py -- simulate the latency of the Red Pitaya (PyRPL 0.9.9.1) signal chain used by cavity_lock_app.py.

It drives PyRPL's own Verilog (red_pitaya_dsp and every module it instantiates) with Icarus Verilog and
measures, clock cycle by clock cycle:
  1. pid_p          IN1 -> PID0 (P = 1, no filters) -> OUT1                      pure digital latency
  2. iq_unfiltered  IN1 -> IQ0 demodulator (filters off) -> PID0 -> OUT1         latency of the lock-in path
  3. lockin_app     the lock-in exactly as the app configures it (dither frequency, high-pass, low-pass x n):
                    response of the error signal to a sudden change of the dither signal on the photodiode
  4. lockin_fast    the same with a 20 kHz dither (low-pass 2 x f/8), for comparison
Then it adds the converter and I/O-register delays outside red_pitaya_dsp and prints a phase budget.

Usage
  python fetch_rtl.py                       # once: downloads PyRPL's Verilog (tag 0.9.9.1) into ./rtl
  python run_sim.py                         # uses the app's default settings
  python run_sim.py --settings ../cavity_lock_settings.json --driver-bw 1000
Needs Icarus Verilog (iverilog + vvp on PATH); matplotlib is optional (for the plot).
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
F_CLK = 125e6
T_CLK_NS = 8.0

RTL_FILES = ["red_pitaya_dsp.v", "red_pitaya_pid_block.v", "red_pitaya_filter_block.v", "red_pitaya_lpf_block.v",
             "red_pitaya_trigger_block.v", "red_pitaya_iq_block.v", "red_pitaya_iq_fgen_block.v",
             "red_pitaya_iq_demodulator_block.v", "red_pitaya_iq_modulator_block.v", "red_pitaya_product_sat.v",
             "red_pitaya_saturate.v", "red_pitaya_pfd_block.v"]

# Delays outside red_pitaya_dsp (cycles of the 125 MHz clock unless noted), with where each number comes from.
OUTSIDE = [
    ("ADC pipeline (LTC2145-14, full-rate CMOS)", 6.0, "LTC2145-14 datasheet: pipeline latency 6 cycles"),
    ("FPGA ADC input register", 1.0, "red_pitaya_top.v: adc_dat_a <= adc_dat_a_i (one register)"),
    ("FPGA DAC output register", 1.0, "red_pitaya_top.v: dac_dat_a <= ... (one register)"),
    ("ODDR output stage (A/B interleave)", 1.0, "red_pitaya_top.v: ODDR oddr_dac_dat, one dac_clk_1x cycle"),
    ("DAC latches (AD9767 input + DAC latch)", 1.0, "AD9767 datasheet: double-buffered latches, ~1 cycle"),
    ("analog filters + DAC settling", 35.0 / T_CLK_NS, "AD9767 settling 35 ns; 50 MHz input/output filters"),
]

# ---- PyRPL register encodings (pyrpl/attributes.py, hardware_modules/iq.py, pid.py) ----
def clog2(x):
    return 1 if x < 2 else int(math.floor(math.log2(float(x)))) + 1


def filter_reg(bandwidths, stages, shiftbits, minbw):
    """FilterRegister.from_python: list of corner frequencies [Hz] (negative = high-pass) -> register word,
    plus the corner frequencies the FPGA really implements."""
    maxshift = clog2(F_CLK / minbw)
    word, actual = 0, []
    for i in range(stages):
        bw = bandwidths[i] if i < len(bandwidths) else 0
        if bw == 0:
            actual.append(0.0)
            continue
        alpha = 1.0 - math.exp(-abs(bw) * 2.0 * math.pi / F_CLK)
        shift = int(round(math.log2(alpha * 2 ** maxshift)))
        shift = min(max(shift, 0), 2 ** shiftbits - 1)
        a = 2.0 ** shift / 2 ** maxshift
        f_real = -math.log(1.0 - a) / 2.0 / math.pi * F_CLK
        actual.append(-f_real if bw < 0 else f_real)
        word |= (shift | 0x80 | (0x40 if bw < 0 else 0)) << (8 * i)
    return word, actual


def freq_reg(f):
    return int(round(f / F_CLK * 2 ** 32)) & 0xFFFFFFFF


def phase_reg(deg):                                  # IQ phase register is inverted in PyRPL
    return int(round(-deg / 360.0 * 2 ** 32)) % 2 ** 32


def s14(v):
    return int(round(v * 2 ** 13)) & 0x3FFF


# module numbers and DSP routing codes (red_pitaya_dsp.v)
PID0, IQ0, ADC1 = 0, 5, 10
OUT1 = 1


def base(module):
    return module << 16


def routing(writes, module, inp, out):
    writes.append((base(module) | 0x00, inp, f"input_select[{module}]"))
    writes.append((base(module) | 0x04, out, f"output_select[{module}]"))


def pid_p_only(writes, inp):
    routing(writes, PID0, inp, OUT1)
    writes += [(base(PID0) | 0x120, 0, "pid0 input filter off"), (base(PID0) | 0x104, 0, "pid0 setpoint 0"),
               (base(PID0) | 0x10C, 0, "pid0 ki 0"), (base(PID0) | 0x108, 4096, "pid0 kp = 1 (2^12)"),
               (base(PID0) | 0x124, s14(-1.0), "pid0 min -1 V"), (base(PID0) | 0x128, s14(1.0) - 1, "pid0 max +1 V")]


def iq_setup(writes, f_hz, phase_deg, hp_hz, lp_list, qf):
    hp_word, hp_real = filter_reg([-hp_hz] if hp_hz else [], 1, 5, 50)
    lp_word, lp_real = filter_reg(lp_list, 2, 5, 10)
    routing(writes, IQ0, ADC1, 0)
    writes += [(base(IQ0) | 0x100, 0, "iq0 flags"), (base(IQ0) | 0x104, phase_reg(phase_deg), "iq0 phase"),
               (base(IQ0) | 0x108, freq_reg(f_hz), "iq0 frequency"), (base(IQ0) | 0x10C, 0, "iq0 output: quadrature"),
               (base(IQ0) | 0x110, 0, "iq0 g1 (gain 0)"), (base(IQ0) | 0x11C, 0, "iq0 g4 (gain 0)"),
               (base(IQ0) | 0x114, 0, "iq0 amplitude 0"), (base(IQ0) | 0x118, qf, "iq0 quadrature factor"),
               (base(IQ0) | 0x120, hp_word, "iq0 input high-pass"), (base(IQ0) | 0x124, lp_word, "iq0 low-pass")]
    return hp_real, lp_real


def config_text(writes, defines):
    lines = [f"`define {k} {v}" for k, v in defines.items()]
    lines += [f"wr(32'h{a:08X}, 32'h{d & 0xFFFFFFFF:08X}); // {c}" for a, d, c in writes]
    # pulse the global sync line: resets the IQ oscillator phase accumulators to a defined 0
    lines += ["wr(32'h0000000C, 32'h00000000); // sync low", "wr(32'h0000000C, 32'h000000FF); // sync high"]
    # clear the PID integrators: before the sync pulse the oscillator outputs were undefined (X) in simulation,
    # and X * (ki = 0) is X, so the integrator would hold X forever (the FPGA has no X: it stays 0)
    lines += ["repeat (50) @(posedge clk); // let the oscillators and demodulators fill their pipelines"]
    lines += [f"wr(32'h{base(m) | 0x100:08X}, 32'h00000000); // pid{m} ival = 0" for m in (0, 1, 2)]
    return "\n".join(lines) + "\n"


def run(rtl, name, writes, defines):
    work = tempfile.mkdtemp(prefix=f"rpsim_{name}_")
    with open(os.path.join(work, "tb_config.vh"), "w") as f:
        f.write(config_text(writes, defines))
    srcs = [os.path.join(HERE, "tb_latency.v"), os.path.join(HERE, "iir_stub.v")] + [os.path.join(rtl, x) for x in RTL_FILES]
    vvp = os.path.join(work, "sim.vvp")
    cmd = ["iverilog", "-g2012", "-I", work, "-o", vvp, "-s", "tb_latency"] + srcs
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"iverilog failed for {name}:\n{r.stderr}")
    t0 = time.time()
    r = subprocess.run(["vvp", "-n", vvp], capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        sys.exit(f"vvp failed for {name}:\n{r.stderr}")
    return r.stdout, time.time() - t0


def step_experiment(rtl, name, writes, step=1000, vcd=False):
    d = {"MODE_STEP": 1, "SETTLE": 200, "STEP": step, "MAXWAIT": 10000}
    if vcd:
        d["DUMP"] = '"' + os.path.join(HERE, f"{name}.vcd").replace("\\", "/") + '"'
    out, dt = run(rtl, name, writes, d)
    lat = None
    resp = []
    for line in out.splitlines():
        if line.startswith("LATENCY_CYCLES"):
            lat = int(line.split()[1])
        elif line.startswith("STEPRESP"):
            _, k, v = line.split()
            resp.append((int(k), int(v)))
    return lat, resp, dt


def lockin_experiment(rtl, name, writes, f_hz, phase_deg, t_step_us, t_end_us, amp=2000, b2f=600, decim=125):
    defines = {"MODE_LOCKIN": 1, "F_HZ": f"{f_hz:.6f}", "PH": f"{math.radians(-phase_deg):.9f}",   # in phase with the demodulation LO
               "AMP": f"{amp:.1f}", "B2F": f"{b2f:.1f}", "T_STEP_US": f"{t_step_us:.3f}",
               "T_END_US": f"{t_end_us:.3f}", "DECIM": decim}
    out, dt = run(rtl, name, writes, defines)
    t, y = [], []
    for line in out.splitlines():
        if line.startswith("S "):
            _, a, b = line.split()
            t.append(float(a))
            y.append(int(b))
    return t, y, dt


def crossing_times(t, y, t_step, fracs=(0.1, 0.5, 0.9)):
    """Times after the step at which the cycle-averaged response first reaches each fraction of its final value."""
    import numpy as np
    t, y = np.asarray(t), np.asarray(y, dtype=float)
    pre = y[t < t_step]
    y0 = float(np.mean(pre[len(pre) // 2:])) if len(pre) else 0.0
    yf = float(np.mean(y[t > t[-1] - 0.15 * (t[-1] - t_step)]))
    span = yf - y0
    out = {}
    after = t >= t_step
    for fr in fracs:
        target = y0 + fr * span
        idx = np.flatnonzero(after & ((y - target) * np.sign(span) >= 0))
        out[fr] = float(t[idx[0]] - t_step) if len(idx) else float("nan")
    ripple = float(np.std(y[t > t[-1] - 0.15 * (t[-1] - t_step)]))
    return out, y0, yf, ripple


def theory_times(fc_list):
    """10/50/90 % times [us] for a cascade of equal first-order low-passes (numerically)."""
    import numpy as np
    fcs = [abs(f) for f in fc_list if f > 0]
    if not fcs:
        return {0.1: 0.0, 0.5: 0.0, 0.9: 0.0}
    tau = 1.0 / (2 * math.pi * min(fcs))
    t = np.linspace(0, 20 * tau * len(fcs), 200001)
    y = np.ones_like(t)
    resp = np.zeros_like(t)
    # step response of n identical poles: 1 - e^{-x} sum_{k<n} x^k/k!
    n = len(fcs)
    x = t / tau
    s = np.zeros_like(t)
    term = np.ones_like(t)
    for k in range(n):
        if k:
            term = term * x / k
        s += term
    resp = y - np.exp(-x) * s
    return {fr: float(t[np.argmax(resp >= fr)] * 1e6) for fr in (0.1, 0.5, 0.9)}


def phase_budget(latency_s, lp_list, driver_bw, ugfs):
    rows = []
    for f in ugfs:
        ph_delay = 360.0 * f * latency_s
        ph_lpf = sum(math.degrees(math.atan(f / fc)) for fc in lp_list if fc > 0)
        ph_drv = math.degrees(math.atan(f / driver_bw)) if driver_bw else 0.0
        rows.append((f, ph_delay, ph_lpf, ph_drv, 90.0 + ph_delay + ph_lpf + ph_drv))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl", default=os.path.join(HERE, "rtl_sim"), help="simulation copy of PyRPL's Verilog (fetch_rtl.py)")
    ap.add_argument("--settings", help="cavity_lock_settings.json saved by the app (else the app defaults)")
    ap.add_argument("--driver-bw", type=float, default=1000.0,
                    help="small-signal bandwidth of your PZT driver with the PZT connected [Hz] (default 1000)")
    ap.add_argument("--fast", action="store_true", help="only the latency experiments (skip the slow lock-in runs)")
    ap.add_argument("--vcd", action="store_true", help="also write pid_p.vcd and iq_unfiltered.vcd (open in GTKWave)")
    args = ap.parse_args()
    if not shutil.which("iverilog") or not shutil.which("vvp"):
        sys.exit("Icarus Verilog not found: install it (see README) so that 'iverilog' and 'vvp' are on PATH")
    missing = [f for f in RTL_FILES if not os.path.exists(os.path.join(args.rtl, f))]
    if missing:
        sys.exit(f"missing Verilog files in {args.rtl}: {missing}\nrun: python fetch_rtl.py")

    s = {"acq_dither_hz": 1000.0, "acq_dither_mvpp": 30.0, "lock_same_dither": True, "lock_dither_hz": 1000.0,
         "phase_deg": 0.0, "lpf_hz": 303.6, "lpf_order": 2, "ac_hz": 75.0, "err_gain": 1}
    if args.settings:
        with open(args.settings) as f:
            s.update(json.load(f))
    f_d = s["acq_dither_hz"] if s.get("lock_same_dither", True) else s["lock_dither_hz"]
    lp_app = [s["lpf_hz"]] * int(min(s["lpf_order"], 2))
    print(f"app settings: dither {f_d:g} Hz, high-pass {s['ac_hz']:g} Hz, low-pass {s['lpf_hz']:g} Hz x{len(lp_app)}, "
          f"phase {s['phase_deg']:g} deg, quadrature factor {int(s['err_gain'])}\n")
    results = {"settings": s}

    # 1. pure digital latency through a PID
    w = []
    pid_p_only(w, ADC1)
    lat_pid, resp_pid, dt = step_experiment(args.rtl, "pid_p", w, vcd=args.vcd)
    print(f"[1] IN1 -> PID0 (P=1) -> OUT1 inside red_pitaya_dsp: {lat_pid} cycles = {lat_pid*T_CLK_NS:.0f} ns  ({dt:.1f} s)")

    # 2. demodulation path with all filters off (constant LO so the demodulator is a pure gain)
    w = []
    iq_setup(w, 0.0, -90.0, 0, [], 1)                # phase register +90 deg -> demodulation LO = +1
    pid_p_only(w, IQ0)
    lat_iq, resp_iq, dt = step_experiment(args.rtl, "iq_unfiltered", w, step=4000, vcd=args.vcd)
    print(f"[2] IN1 -> IQ0 (filters off) -> PID0 -> OUT1: {lat_iq} cycles = {lat_iq*T_CLK_NS:.0f} ns  ({dt:.1f} s)")

    outside = sum(c for _, c, _ in OUTSIDE)
    tot_pid = lat_pid + outside
    tot_iq = lat_iq + outside
    print("\n    delays outside red_pitaya_dsp:")
    for n, c, src in OUTSIDE:
        print(f"      {n:<44s} {c:5.1f} cycles  ({src})")
    print(f"    => analog in to analog out: PID path ~{tot_pid*T_CLK_NS:.0f} ns, lock-in path ~{tot_iq*T_CLK_NS:.0f} ns")
    print("       (published measurements of Red Pitaya PID loops: ~200 ns)\n")
    results.update(latency_cycles_pid=lat_pid, latency_cycles_iq=lat_iq, outside_cycles=outside,
                   total_ns_pid=tot_pid * T_CLK_NS, total_ns_iq=tot_iq * T_CLK_NS)

    runs = {}
    if not args.fast:
        # 3. the lock-in as the app sets it up
        w = []
        hp_real, lp_real = iq_setup(w, f_d, s["phase_deg"], s["ac_hz"], lp_app, int(s["err_gain"]))
        pid_p_only(w, IQ0)
        n_lp = len([x for x in lp_real if x > 0])
        tau = 1 / (2 * math.pi * min(x for x in lp_real if x > 0))
        t_step = 3 * tau * 1e6
        t_end = t_step + 6 * tau * 1e6 * max(n_lp, 1)
        t, y, dt = lockin_experiment(args.rtl, "lockin_app", w, f_d, s["phase_deg"], t_step, t_end, decim=250)
        runs["lockin_app"] = dict(t=t, y=y, t_step=t_step, lp=lp_real, hp=hp_real, f=f_d)
        print(f"[3] lock-in as configured by the app (simulated {t_end/1000:.1f} ms in {dt:.0f} s)")
        # 4. a faster configuration for comparison
        f_fast = 20000.0
        w = []
        hp_f, lp_f = iq_setup(w, f_fast, 0.0, f_fast / 16, [f_fast / 8, f_fast / 8], 1)
        pid_p_only(w, IQ0)
        tau_f = 1 / (2 * math.pi * lp_f[0])
        t_step_f = 3 * tau_f * 1e6
        t_end_f = t_step_f + 12 * tau_f * 1e6
        t2, y2, dt = lockin_experiment(args.rtl, "lockin_fast", w, f_fast, 0.0, t_step_f, t_end_f, decim=25)
        runs["lockin_fast"] = dict(t=t2, y=y2, t_step=t_step_f, lp=lp_f, hp=hp_f, f=f_fast)
        print(f"[4] lock-in with a 20 kHz dither and two low-pass stages at f/8 (simulated {t_end_f/1000:.2f} ms in {dt:.0f} s)\n")
        for key, r in runs.items():
            ct, y0, yf, rip = crossing_times(r["t"], r["y"], r["t_step"])
            th = theory_times(r["lp"])
            lp_txt = " + ".join(f"{x:.1f} Hz" for x in r["lp"] if x > 0)
            print(f"    {key}: dither {r['f']:g} Hz, high-pass {abs(r['hp'][0]) if r['hp'] else 0:.1f} Hz, low-pass {lp_txt}")
            print(f"      error signal reaches 10/50/90 %: {ct[0.1]:8.1f} / {ct[0.5]:8.1f} / {ct[0.9]:8.1f} us"
                  f"   (ideal low-pass cascade: {th[0.1]:.1f} / {th[0.5]:.1f} / {th[0.9]:.1f} us)")
            print(f"      residual ripple at 2f: {rip:.1f} counts rms on a {yf - y0:.0f}-count step")
            r.update(times=ct, theory=th, ripple=rip, step=yf - y0)
            results[key] = {k: v for k, v in r.items() if k not in ("t", "y")}

    # phase budget
    lat_s = tot_iq * T_CLK_NS * 1e-9
    print(f"\nphase lag at candidate loop bandwidths (integrator 90 deg + FPGA/converters + error low-pass + PZT driver "
          f"at {args.driver_bw:g} Hz); keep the total under ~135 deg for a stable lock:")
    budgets = {"app": lp_app}
    if "lockin_fast" in runs:
        budgets["fast (20 kHz dither)"] = [x for x in runs["lockin_fast"]["lp"] if x > 0]
    for label, lps in budgets.items():
        print(f"  error low-pass of the {label} configuration: " + " + ".join(f"{x:.0f} Hz" for x in lps))
        print(f"    {'UGF [Hz]':>9s} {'delay':>8s} {'low-pass':>9s} {'driver':>8s} {'total':>8s}")
        rows = phase_budget(lat_s, lps, args.driver_bw, [10, 30, 100, 300, 1000, 3000])
        for f, a, b, c, tot in rows:
            print(f"    {f:9.0f} {a:8.3f} {b:9.1f} {c:8.1f} {tot:8.1f}{'   <- too much' if tot > 135 else ''}")
        results[f"phase_budget_{label.split()[0]}"] = rows
    with open(os.path.join(HERE, "sim_results.json"), "w") as f:
        json.dump(results, f, indent=2, default=float)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if runs:
            fig, axs = plt.subplots(1, 2, figsize=(11, 3.8))
            for ax, (key, r) in zip(axs, runs.items()):
                import numpy as np
                t = (np.asarray(r["t"]) - r["t_step"]) / 1000.0
                ax.plot(t, r["y"], lw=0.7, label="simulated OUT1 (error signal)")
                tt = np.linspace(0, t[-1], 400)
                n = len([x for x in r["lp"] if x > 0])
                tau_ms = 1e3 / (2 * math.pi * min(x for x in r["lp"] if x > 0))
                x = tt / tau_ms
                ssum = sum(x ** k / math.factorial(k) for k in range(n))
                ax.plot(tt, r["step"] * (1 - np.exp(-x) * ssum), "k--", lw=1, label=f"{n}-pole low-pass theory")
                ax.axvline(0, color="gray", lw=0.6)
                ax.set_title(f"{key}: dither {r['f']/1000:g} kHz")
                ax.set_xlabel("time after the error appears [ms]")
                ax.set_ylabel("OUT1 [DAC counts]")
                ax.legend(fontsize=8)
            fig.tight_layout()
            fig.savefig(os.path.join(HERE, "lockin_step_response.png"), dpi=130)
            print("\nplot: lockin_step_response.png")
    except ImportError:
        pass
    print("results: sim_results.json")


if __name__ == "__main__":
    main()
