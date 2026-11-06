"""
lockin_pi_schematic.py -- the diagram of lockin_pi_app.py as data (blocks, wires, switches, probes) and its state
for the browser. Drawn by lockin_pi_ui.py with the clientside code of the main app (ui.DIAG_JS, ui.POP_JS).

Top: LOCK  IN1 -> iq2 (high-pass, X and Y mixers, low-pass, gain) -> [Filter] -> pid0 (PI on X or Y) -> [PI] -> sum
           with the dither (iq2 NCO) [Dither] and the ramp / offset (asg0) [Ramp] -> [Output] -> OUT1 -> PZT driver
Bottom: INTENSITY  IN2 (or IN1) -> [Filter] -> pid2 (PI) -> [Servo] -> OUT2 -> AOM
"""
import schematic as SCH

P = SCH.P
PREFIX = ""
CANVAS_W, CANVAS_H = 1340, 670

BLOCKS = [
    # lock plant
    dict(id="photodiode", label="Photodiode", kind="plant", x=20, y=12, w=150, h=58, params=[], opts=[],
         note="IN1: the photodiode. IN1 jumper LV (+/-1 V) or HV (+/-20 V), set by hand."),
    dict(id="cavity", label="Cavity", kind="plant", x=330, y=12, w=150, h=58, params=[], opts=[],
         note="The cavity: its transmission is the photodiode signal."),
    dict(id="driver", label="PZT driver", kind="plant", x=640, y=12, w=210, h=58, params=[], opts=[],
         note="External: driven by OUT1 (offset or ramp + PI + dither)."),
    # lock-in iq2
    dict(id="bandpass", label="High-pass", kind="dsp", x=20, y=125, w=140, h=110,
         params=[P("ac_hz", "iq2.acbandwidth")], opts=[],
         note="iq2 input high-pass, in front of both mixers (0 = off). 1 stage, factor-2 ladder."),
    dict(id="mixer", label="×", kind="node", x=196, y=128, w=40, h=40, params=[], opts=[],
         note="X mixer: IN1 (high-passed) times the LO. X = 'iq2'."),
    dict(id="mixerq", label="×", kind="node", x=196, y=192, w=40, h=40, params=[], opts=[],
         note="Y mixer: IN1 (high-passed) times the LO + 90 deg. Y = 'iq2_2' (the second quadrature, wired out "
              "only on iq2 in this FPGA)."),
    dict(id="lowpass", label="Low-pass (X and Y)", kind="dsp", x=268, y=125, w=150, h=122,
         params=[P("lpf_hz", "iq2.bandwidth"), P("lpf_order", "iq2.bandwidth", widget="radio")], opts=[],
         note="iq2 low-pass after both mixers: one register, 1 to 4 first-order stages, factor-2 ladder."),
    dict(id="amp", label="Gain", kind="dsp", x=446, y=125, w=110, h=110,
         params=[P("lia_gain", "iq2.quadrature_factor", widget="int")], opts=[],
         note="iq2.quadrature_factor: integer gain for X and Y."),
    dict(id="phase", label="Phase", kind="dsp", x=141, y=250, w=150, h=74,
         params=[P("phase_deg", "iq2.phase")], opts=[],
         note="iq2.phase, set by hand: it rotates X and Y. The ramp shows the signal angle in the X-Y plane "
              "(display only; 0 deg = all in X). Nothing is set automatically."),
    dict(id="dither", label="Dither generator", kind="dsp", x=20, y=345, w=250, h=110,
         params=[P("dither_hz", "iq2.frequency"), P("dither_mvpp", "iq2.amplitude")], opts=[],
         note="iq2's oscillator: the LO of both mixers and, through the Dither switch, summed into OUT1."),
    # lock PI
    dict(id="filter", label="Filter (pid0 input)", kind="dsp", x=590, y=118, w=160, h=160,
         params=[P(k, "pid0.inputfilter") for k in ("filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz")], opts=[],
         note="pid0's input filter: 4 first-order stages, > 0 low-pass corner, < 0 high-pass, 0 = stage off. "
              "Written only while the Filter switch is on (off: all stages 0). The FPGA rounds the corners."),
    dict(id="pid", label="PI (pid0)", kind="dsp", x=850, y=105, w=230, h=185,
         params=[P("pi_input", "pid0.input", widget="radio"), P("p", "pid0.p"), P("i", "pid0.i"),
                 P("setpoint_mv", "pid0.setpoint"), P("clamp_lo_mv", "pid0.min_voltage", inline=False),
                 P("clamp_hi_mv", "pid0.max_voltage", inline=False)], opts=[],
         note="e = (X or Y, filtered when the Filter switch is on) - setpoint; out = p e + ival, clamped. p and i are "
              "PyRPL's pid0.p and pid0.i exactly as typed (no conversion); the readback shows the FPGA's rounding."),
    dict(id="ramp", label="Ramp / offset (asg0)", kind="dsp", x=320, y=345, w=260, h=118,
         params=[P("offset_mv", "asg0.offset"), P("ramp_hz", "asg0.frequency"), P("ramp_mvpp", "asg0.amplitude"),
                 P("ramp_shape", "asg0.waveform", inline=False)], opts=[],
         note="Ramp switch on: a ramp around the offset (PI off); the scope shows several periods triggered on it. "
              "Ramp off: the offset as a DC level under the PI."),
    dict(id="sum", label="Σ", kind="node", x=1120, y=395, w=40, h=40, params=[], opts=[],
         note="Everything with output_direct = out1 is summed: asg0 (ramp / offset), pid0 (PI), iq2 (dither)."),
    dict(id="out1", label="OUT1", kind="node", x=1222, y=385, w=106, h=60, params=[], opts=[],
         note="OUT1 -> PZT driver. The DAC clips at +/-1 V."),
    # intensity
    dict(id="aom", label="AOM + pickoff PD", kind="plant", x=20, y=530, w=180, h=60, params=[], opts=[],
         note="External: OUT2 drives the AOM RF power; the pickoff photodiode goes to IN2."),
    dict(id="int_in", label="Input", kind="dsp", x=240, y=515, w=120, h=90,
         params=[P("int_input", "pid2.input", widget="radio")], opts=[],
         note="pid2's input: IN2 (the AOM pickoff photodiode) or IN1."),
    dict(id="int_filter", label="Filter (pid2 input)", kind="dsp", x=400, y=490, w=160, h=160,
         params=[P(k, "pid2.inputfilter") for k in ("int_filt1_hz", "int_filt2_hz", "int_filt3_hz",
                                                    "int_filt4_hz")], opts=[],
         note="pid2's input filter, as for the lock: written only while its Filter switch is on."),
    dict(id="int_pid", label="Intensity PI (pid2)", kind="dsp", x=680, y=485, w=230, h=165,
         params=[P("int_p", "pid2.p"), P("int_i", "pid2.i"), P("int_setpoint_mv", "pid2.setpoint"),
                 P("int_clamp_lo_mv", "pid2.min_voltage", inline=False),
                 P("int_clamp_hi_mv", "pid2.max_voltage", inline=False)], opts=[],
         note="e = input (filtered when its Filter switch is on) - setpoint; out = p e + ival, clamped. PyRPL's "
              "pid2.p and pid2.i exactly as typed. Servo off: OUT2 = 0 V."),
    dict(id="out2", label="OUT2", kind="node", x=1040, y=537, w=106, h=60, params=[], opts=[],
         note="OUT2 -> AOM driver."),
]
BLOCK = {b["id"]: b for b in BLOCKS}

HOMES = {"scope": ["live_ms", "int_live_ms", "ramp_periods", "xaxis", "ui_hz", "probe_every"],
         "scope_bar": ["probe_a", "probe_b"]}

OWNER = {}
for _b in BLOCKS:
    for _p in _b["params"]:
        OWNER[_p["key"]] = _b["id"]
for _h, _ks in HOMES.items():
    for _k in _ks:
        OWNER[_k] = _h


def ownership_problems(keys):
    seen = {}
    for b in BLOCKS:
        for k in [p["key"] for p in b["params"]]:
            seen.setdefault(k, []).append(b["id"])
    for h, ks in HOMES.items():
        for k in ks:
            seen.setdefault(k, []).append(h)
    return [f"{k}: no home" for k in keys if k not in seen] + [f"{k}: in {v}" for k, v in seen.items() if len(v) > 1]


WIRES = [
    dict(id="pd_in1", kind="plant", pts=[(95, 70), (95, 125)]),
    dict(id="drv_cav", kind="plant", pts=[(640, 41), (480, 41)]),
    dict(id="cav_pd", kind="plant", pts=[(330, 41), (170, 41)]),
    dict(id="bp_mix", kind="sig", pts=[(160, 148), (196, 148)]),
    dict(id="bp_mixq", kind="sig", pts=[(160, 212), (196, 212)]),
    dict(id="mix_lpf", kind="sig", pts=[(236, 148), (268, 148)]),
    dict(id="mixq_lpf", kind="sig", pts=[(236, 212), (268, 212)]),
    dict(id="lpf_amp", kind="sig", pts=[(418, 148), (446, 148)]),
    dict(id="lpfq_amp", kind="sig", pts=[(418, 212), (446, 212)]),
    dict(id="amp_f", kind="sig", pts=[(556, 148), (590, 148)]),
    dict(id="ampq_f", kind="sig", pts=[(556, 212), (590, 212)]),
    dict(id="f_pid", kind="sig", pts=[(750, 198), (850, 198)]),
    dict(id="pid_sum", kind="drv", pts=[(1080, 198), (1140, 198), (1140, 395)]),
    dict(id="nco_phase", kind="lo", pts=[(216, 345), (216, 322)]),
    dict(id="lo_mix", kind="lo", pts=[(216, 250), (216, 232)]),
    dict(id="lo_90", kind="lo", pts=[(216, 192), (216, 168)]),
    dict(id="dith_sum", kind="drv", pts=[(270, 362), (295, 362), (295, 322), (1128, 322), (1128, 397)]),
    dict(id="ramp_sum", kind="drv", pts=[(580, 415), (1120, 415)]),
    dict(id="sum_out", kind="drv", pts=[(1160, 415), (1222, 415)]),
    dict(id="out1_drv", kind="plant", pts=[(1318, 385), (1318, 95), (745, 95), (745, 70)]),
    dict(id="aom_in", kind="plant", pts=[(200, 560), (240, 560)]),
    dict(id="in_f", kind="sig", pts=[(360, 560), (400, 560)]),
    dict(id="if_pid", kind="sig", pts=[(560, 565), (680, 565)]),
    dict(id="ipid_out2", kind="drv", pts=[(910, 567), (1040, 567)]),
    dict(id="out2_aom", kind="plant", pts=[(1093, 597), (1093, 662), (110, 662), (110, 590)]),
]

SWITCHES = [
    dict(id="filt", label="Filter", x=800, y=198),
    dict(id="pi", label="PI", x=1110, y=198),
    dict(id="dither", label="Dither", x=700, y=322),
    dict(id="ramp", label="Ramp", x=860, y=415),
    dict(id="output", label="Output", x=1191, y=415),
    dict(id="int_filt", label="Filter", x=620, y=565),
    dict(id="int_on", label="Servo", x=965, y=567),
]
SWITCH_TITLES = {
    "filt": "Filter: pid0's input filter on (the stage values) / off (all stages 0)",
    "pi": "PI: pid0 -> OUT1 on (integrator from 0) / off. Turns the ramp off.",
    "dither": "Dither: iq2's oscillator on OUT1",
    "ramp": "Ramp: on = ramp around the offset on OUT1 (PI off); off = the offset as DC",
    "output": "Output: OUT1 on / off (off: OUT1 = 0 V)",
    "int_filt": "Filter: pid2's input filter on / off",
    "int_on": "Servo: pid2 -> OUT2 on (integrator from 0) / off (OUT2 = 0 V)",
}

PROBES = [
    dict(id="in1", signal="in1", x=95, y=100, label="in1: photodiode (IN1)"),
    dict(id="hp", signal=None, x=178, y=148, why="after the high-pass: inside iq2, not a scope signal"),
    dict(id="mix", signal=None, x=252, y=148, why="raw mixer output: inside iq2, not a scope signal"),
    dict(id="iq2", signal="iq2", x=573, y=148, label="iq2: X"),
    dict(id="iq2_2", signal="iq2_2", x=573, y=212, label="iq2_2: Y"),
    dict(id="pid0", signal="pid0", x=1140, y=300, label="pid0: PI output"),
    dict(id="asg0", signal="asg0", x=620, y=415, label="asg0: ramp / offset"),
    dict(id="out1", signal="out1", x=1318, y=300, label="out1: OUT1"),
    dict(id="in2", signal="in2", x=220, y=560, label="in2: IN2"),
    dict(id="pid2", signal="pid2", x=1010, y=567, label="pid2: intensity PI output"),
    dict(id="out2", signal="out2", x=1093, y=630, label="out2: OUT2"),
]
PROBE_SIGNALS = [p["signal"] for p in PROBES if p["signal"]]

TERMINALS = [dict(text="IN1", x=104, y=106), dict(text="X", x=242, y=128), dict(text="Y", x=242, y=192),
             dict(text="90°", x=222, y=172), dict(text="OUT1 → PZT driver", x=1150, y=78),
             dict(text="INTENSITY", x=24, y=478), dict(text="OUT2 → AOM", x=1100, y=640)]


def geometry():
    return dict(W=CANVAS_W, H=CANVAS_H, prefix=PREFIX,
                wires=[dict(id=w["id"], kind=w["kind"], pts=w["pts"]) for w in WIRES])


READBACK = {
    "iq2": ["frequency", "amplitude", "phase", "bandwidth", "acbandwidth", "quadrature_factor", "output_direct"],
    "pid0": ["input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter", "output_direct"],
    "pid2": ["input", "p", "i", "setpoint", "min_voltage", "max_voltage", "inputfilter", "output_direct"],
    "asg0": ["waveform", "frequency", "amplitude", "offset", "output_direct"],
}


def read_hw(r):
    return SCH.read_hw(r, READBACK)


def _f(x, fmt="{:g}"):
    return "?" if x is None else fmt.format(x)


def _filt_lines(hw, m, on):
    f = [x for x in (hw.get(f"{m}.inputfilter") or []) if x]
    if not on:
        return [("bypassed (Filter switch off)", "dim")]
    if not f:
        return [("on, all stages 0", "dim")]
    return [(", ".join((f"LP {x:.1f}" if x > 0 else f"HP {-x:.1f}") for x in f) + " Hz", "")]


def _pid_lines(hw, m, ival, routed, warn_label):
    lines = [(f"hw p {_f(hw.get(f'{m}.p'), '{:+.6g}')}, i {_f(hw.get(f'{m}.i'), '{:+.6g}')}", ""),
             (f"setpoint {(hw.get(f'{m}.setpoint') or 0)*1e3:+.2f} mV, clamp "
              f"{(hw.get(f'{m}.min_voltage') or 0)*1e3:+.0f}..{(hw.get(f'{m}.max_voltage') or 0)*1e3:+.0f} mV", "dim")]
    lo, hi = hw.get(f"{m}.min_voltage"), hw.get(f"{m}.max_voltage")
    if routed and ival is not None:
        lines.append((f"integrator {ival*1e3:+.2f} mV", ""))
        if lo is not None and hi is not None and hi > lo and (ival <= lo + 0.01 * (hi - lo)
                                                             or ival >= hi - 0.01 * (hi - lo)):
            lines.append((warn_label, "warn"))
    return lines


def diagram_state(snap, switches):
    s, hw, mode = snap["settings"], snap["hw"], snap["mode"]
    sw = switches
    B = {"photodiode": [], "cavity": [], "driver": [("← OUT1", "dim")], "mixer": [], "mixerq": [], "sum": [],
         "aom": [("← OUT2 · pickoff → IN2", "dim")]}
    B["bandpass"] = [(f"HP {_f(hw.get('iq2.acbandwidth'), '{:.1f}')} Hz", "")]
    bw = [b for b in (hw.get("iq2.bandwidth") or []) if b and b > 0]
    B["lowpass"] = [(f"LP {bw[0] if bw else 0:.1f} Hz ×{len(bw)}", ""), (f"{6*len(bw)} dB/oct · X and Y", "dim")]
    B["amp"] = [(f"×{_f(hw.get('iq2.quadrature_factor'))}", "")]
    B["phase"] = [(f"{_f(hw.get('iq2.phase'), '{:+.1f}')}°", "")]
    th = snap.get("theta")
    if th and mode == "ramp":
        B["phase"].append((f"signal at {th['theta']:+.1f}° in X-Y" if th["q"] >= 3 else "X-Y: no clear axis", "dim"))
    B["dither"] = [(f"{_f(hw.get('iq2.frequency'), '{:.6g}')} Hz, {(hw.get('iq2.amplitude') or 0)*2e3:.2f} mVpp"
                    + ("" if sw["dither"] else " (off)"), "")]
    B["filter"] = _filt_lines(hw, "pid0", sw["filt"])
    src = {"iq2": "X", "iq2_2": "Y"}.get(hw.get("pid0.input"), hw.get("pid0.input") or "?")
    B["pid"] = [(f"input {src}", "dim")] + _pid_lines(hw, "pid0", snap.get("ival"), sw["pi"], "AT THE CLAMP")
    a_amp, a_off = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
    if hw.get("asg0.output_direct") != "out1":
        B["ramp"] = [("off", "dim")]
    elif a_amp > 0:
        wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
        B["ramp"] = [(f"RAMP {wf} {_f(hw.get('asg0.frequency'), '{:.4g}')} Hz", ""),
                     (f"{(a_off-a_amp)*1e3:+.0f}..{(a_off+a_amp)*1e3:+.0f} mV", "dim")]
    else:
        B["ramp"] = [(f"DC {a_off*1e3:+.2f} mV", "")]
    parts = [n for n, on in (("ramp" if sw["ramp"] else "offset", hw.get("asg0.output_direct") == "out1"),
                             ("PI", sw["pi"]), ("dither", sw["dither"])) if on]
    B["out1"] = [("±1 V DAC" if mode != "off" else "0 V (off)", "" if mode != "off" else "warn"),
                 ("+".join(parts) if parts else "nothing", "dim")]
    B["int_in"] = [(f"pid2.input {hw.get('pid2.input')}", "dim")]
    B["int_filter"] = _filt_lines(hw, "pid2", sw["int_filt"])
    B["int_pid"] = _pid_lines(hw, "pid2", snap.get("int_ival"), sw["int_on"], "AT THE CLAMP")
    B["out2"] = [("PI" if sw["int_on"] else "0 V (servo off)", "" if sw["int_on"] else "dim")]
    xsel = hw.get("pid0.input") != "iq2_2"
    out_on = mode != "off"
    wires = dict(pd_in1=True, drv_cav=True, cav_pd=True, bp_mix=True, bp_mixq=True, mix_lpf=True, mixq_lpf=True,
                 lpf_amp=True, lpfq_amp=True, amp_f=xsel, ampq_f=not xsel, f_pid=True, pid_sum=sw["pi"],
                 nco_phase=True, lo_mix=True, lo_90=True, dith_sum=sw["dither"],
                 ramp_sum=hw.get("asg0.output_direct") == "out1", sum_out=out_on, out1_drv=out_on,
                 aom_in=True, in_f=True, if_pid=True, ipid_out2=sw["int_on"], out2_aom=sw["int_on"])
    busy = mode == "connecting"
    swd = {x["id"]: dict(on=bool(sw[x["id"]]), busy=busy, label=x["label"]) for x in SWITCHES}
    pa, pb = s.get("probe_a", "off"), s.get("probe_b", "off")
    probes = {q["id"]: ("A" if q["signal"] and q["signal"] == pa else "B" if q["signal"] and q["signal"] == pb
                        else "") for q in PROBES}
    return dict(boxes=B, wires=wires, sw=swd, probes=probes, mode=mode)
