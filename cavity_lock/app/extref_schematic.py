"""
extref_schematic.py -- the diagram of extref_app.py as data (blocks, wires, switches, probes) and its state for the
browser. Same building blocks and look as lockin_pi_schematic (drawn with ui.DIAG_JS / ui.POP_JS); every parameter is
inline in its box.

  external dither generator -> PZT driver (summed outside with OUT1)  and  -> IN2
  IN2 -> Reference (iq0 / iq1: the external phase vs the NCO; Track switch) -> NCO -> Phase -> X / Y mixers
  IN1 -> high-pass -> X / Y mixers -> low-pass -> gain -> [Filter] -> PI (pid0) -> [PI] -> sum -> [Output] -> OUT1
                                                                   PI -> [PI → OUT2] -> OUT2
                                   X / Y -> Error output (pid1) -> [Error → OUT2] -> OUT2
  Ramp / offset (asg0) -> [Ramp] -> sum
"""
from types import SimpleNamespace

import schematic as SCH

P = SCH.P

BLOCKS = [
    # plant
    dict(id="photodiode", label="Photodiode", kind="plant", x=20, y=12, w=150, h=58, params=[], opts=[],
         note="IN1: the photodiode. IN1 jumper LV (+/-1 V) or HV (+/-20 V), set by hand."),
    dict(id="cavity", label="Cavity", kind="plant", x=330, y=12, w=150, h=58, params=[], opts=[],
         note="The cavity: its transmission is the photodiode signal."),
    dict(id="driver", label="PZT driver (Σ external)", kind="plant", x=640, y=12, w=230, h=58, params=[], opts=[],
         note="External: OUT1 (correction or ramp) summed with the external dither, outside the Red Pitaya."),
    dict(id="extgen", label="External dither (sine)", kind="plant", x=1040, y=12, w=250, h=58, params=[], opts=[],
         note="Your function generator: the dither for the PZT (summed with OUT1 outside) and, 5 Vpp, into IN2 as "
              "the lock-in reference. IN2 jumper on HV (+/-20 V range); on LV (+/-1 V) 5 Vpp would clip."),
    # reference
    dict(id="reference", label="Reference (iq0 / iq1 on IN2)", kind="dsp", x=20, y=345, w=270, h=150,
         params=[P("ref_hz", "iq0.frequency"), P("ref_lpf_hz", "iq0.bandwidth"), P("in2_scale", None)], opts=[],
         note="iq0 and iq1 demodulate IN2 with the same NCO as iq2 (synchronized; iq1 = iq0 + 90 deg): the phase of "
              "the external sine relative to the NCO. With Track on, the app shifts the phase of iq0, iq1 and iq2 "
              "together about 20 times a second to hold that phase, and corrects the NCO frequency if the phase "
              "keeps drifting. Find f measures the frequency on IN2 and sets the NCO. IN2 scale (display only): "
              "20 on the HV jumper."),
    dict(id="phase", label="Phase", kind="dsp", x=141, y=250, w=150, h=74,
         params=[P("phase_deg", "iq2.phase")], opts=[],
         note="iq2.phase relative to the tracked reference, set by hand: it rotates X and Y. The ramp traces show "
              "the signal angle in the X-Y plane (display only; 0 deg = all in X)."),
    # lock-in iq2
    dict(id="bandpass", label="High-pass", kind="dsp", x=20, y=125, w=140, h=110,
         params=[P("ac_hz", "iq2.acbandwidth")], opts=[],
         note="iq2 input high-pass, in front of both mixers (0 = off). 1 stage, factor-2 ladder."),
    dict(id="mixer", label="×", kind="node", x=196, y=128, w=40, h=40, params=[], opts=[],
         note="X mixer: IN1 (high-passed) times the NCO (locked to the reference). X = 'iq2'."),
    dict(id="mixerq", label="×", kind="node", x=196, y=192, w=40, h=40, params=[], opts=[],
         note="Y mixer: IN1 (high-passed) times the NCO + 90 deg. Y = 'iq2_2'."),
    dict(id="lowpass", label="Low-pass (X and Y)", kind="dsp", x=268, y=125, w=150, h=122,
         params=[P("lpf_hz", "iq2.bandwidth"), P("lpf_order", "iq2.bandwidth", widget="radio")], opts=[],
         note="iq2 low-pass after both mixers: 1 to 4 first-order stages, factor-2 ladder. Keep it well below the "
              "dither frequency (the 2f ripple)."),
    dict(id="amp", label="Gain", kind="dsp", x=446, y=125, w=110, h=110,
         params=[P("lia_gain", "iq2.quadrature_factor", widget="int")], opts=[],
         note="iq2.quadrature_factor: integer gain for X and Y."),
    # PI
    dict(id="filter", label="Filter (pid0 input)", kind="dsp", x=590, y=118, w=160, h=160,
         params=[P(k, "pid0.inputfilter") for k in ("filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz")], opts=[],
         note="pid0's input filter: 4 first-order stages, > 0 low-pass, < 0 high-pass, 0 = off. Written only "
              "while the Filter switch is on."),
    dict(id="pid", label="PI (pid0)", kind="dsp", x=850, y=100, w=240, h=220,
         params=[P("pi_input", "pid0.input", widget="radio"), P("p", "pid0.p"), P("i", "pid0.i"),
                 P("setpoint_mv", "pid0.setpoint"), P("clamp_lo_mv", "pid0.min_voltage"),
                 P("clamp_hi_mv", "pid0.max_voltage")], opts=[],
         note="e = (X or Y, filtered when the Filter switch is on) - setpoint; out = p e + ival, clamped to "
              "[min, max] (the limiters). p and i are PyRPL's pid0.p / pid0.i exactly as typed."),
    # error output
    dict(id="errout", label="Error output (pid1)", kind="dsp", x=620, y=440, w=250, h=112,
         params=[P("eo_source", "pid1.input", widget="radio"), P("eo_gain", "pid1.p"),
                 P("eo_offset_mv", "pid1.ival"), P("eo_limit_mv", "pid1.max_voltage")], opts=[],
         note="OUT2 = gain x (X or Y) + offset, clamped to +/- the limit. pid1 is only a gain stage (I = 0)."),
    # ramp, sums, outputs
    dict(id="ramp", label="Ramp / offset (asg0)", kind="dsp", x=320, y=345, w=260, h=142,
         params=[P("offset_mv", "asg0.offset"), P("ramp_hz", "asg0.frequency"), P("ramp_mvpp", "asg0.amplitude"),
                 P("ramp_shape", "asg0.waveform", widget="radio")], opts=[],
         note="Ramp switch on: the alignment ramp around the offset (PI off OUT1). Ramp off: the offset as DC under "
              "the PI. The scope triggers on it."),
    dict(id="sum", label="Σ", kind="node", x=1120, y=395, w=40, h=40, params=[], opts=[],
         note="OUT1 sum: asg0 (ramp / offset) and pid0 (PI). No dither: it is added outside."),
    dict(id="out1", label="OUT1", kind="node", x=1222, y=385, w=106, h=60, params=[], opts=[],
         note="OUT1 -> PZT driver (summed with the external dither there)."),
    dict(id="out2", label="OUT2", kind="node", x=1222, y=470, w=106, h=60, params=[], opts=[],
         note="OUT2: the error signal (pid1) or the PI (pid0), one at a time."),
]
WIRES = [
    dict(id="pd_in1", kind="plant", pts=[(95, 70), (95, 125)]),
    dict(id="drv_cav", kind="plant", pts=[(640, 41), (480, 41)]),
    dict(id="cav_pd", kind="plant", pts=[(330, 41), (170, 41)]),
    dict(id="gen_drv", kind="plant", pts=[(1040, 41), (870, 41)]),
    dict(id="gen_in2", kind="plant", pts=[(1300, 70), (1300, 90), (1332, 90), (1332, 558), (155, 558), (155, 495)]),
    dict(id="bp_mix", kind="sig", pts=[(160, 148), (196, 148)]),
    dict(id="bp_mixq", kind="sig", pts=[(160, 212), (196, 212)]),
    dict(id="mix_lpf", kind="sig", pts=[(236, 148), (268, 148)]),
    dict(id="mixq_lpf", kind="sig", pts=[(236, 212), (268, 212)]),
    dict(id="lpf_amp", kind="sig", pts=[(418, 148), (446, 148)]),
    dict(id="lpfq_amp", kind="sig", pts=[(418, 212), (446, 212)]),
    dict(id="amp_f", kind="sig", pts=[(556, 148), (590, 148)]),
    dict(id="ampq_f", kind="sig", pts=[(556, 212), (590, 212)]),
    dict(id="xy_eo", kind="sig", pts=[(575, 212), (586, 212), (586, 470), (620, 470)]),
    dict(id="f_pid", kind="sig", pts=[(750, 198), (850, 198)]),
    dict(id="pid_sum", kind="drv", pts=[(1090, 198), (1140, 198), (1140, 395)]),
    dict(id="pid_out2", kind="drv", pts=[(1060, 320), (1060, 486), (1222, 486)]),
    dict(id="eo_out2", kind="drv", pts=[(870, 512), (1222, 512)]),
    dict(id="ref_phase", kind="lo", pts=[(216, 345), (216, 322)]),
    dict(id="lo_mix", kind="lo", pts=[(216, 250), (216, 232)]),
    dict(id="lo_90", kind="lo", pts=[(216, 192), (216, 168)]),
    dict(id="ramp_sum", kind="drv", pts=[(580, 415), (1120, 415)]),
    dict(id="sum_out", kind="drv", pts=[(1160, 415), (1222, 415)]),
    dict(id="out1_drv", kind="plant", pts=[(1318, 385), (1318, 82), (755, 82), (755, 70)]),
]
SWITCHES = [
    dict(id="filt", label="Filter", x=800, y=198),
    dict(id="pi", label="PI", x=1115, y=198),
    dict(id="pi_out2", label="PI → OUT2", x=1150, y=486),
    dict(id="eo", label="Error → OUT2", x=960, y=512),
    dict(id="ramp", label="Ramp", x=860, y=415),
    dict(id="output", label="Output", x=1191, y=415),
    dict(id="track", label="Track", x=155, y=527),
]
SWITCH_TITLES = {
    "filt": "Filter: pid0's input filter on (the stage values) / off (all stages 0)",
    "pi": "PI → OUT1 summer (turns the ramp off); the PI can run with this off and PI → OUT2 on",
    "pi_out2": "PI → OUT2 (turns Error → OUT2 off)",
    "eo": "Error → OUT2: the error signal (pid1: gain x X or Y + offset) on OUT2 (turns PI → OUT2 off)",
    "ramp": "Ramp → OUT1 summer: the alignment ramp (takes the PI off OUT1)",
    "output": "Output: OUT1 on / off (off: OUT1 = 0 V)",
    "track": "Track: hold the NCO phase on the IN2 reference (off: the NCO runs free)",
}
PROBES = [
    dict(id="in1", signal="in1", x=95, y=100, label="in1: photodiode (IN1)"),
    dict(id="hp", signal=None, x=178, y=148, why="after the high-pass: inside iq2, not a scope signal"),
    dict(id="mix", signal=None, x=252, y=148, why="raw mixer output: inside iq2, not a scope signal"),
    dict(id="iq2", signal="iq2", x=573, y=148, label="iq2: X"),
    dict(id="iq2_2", signal="iq2_2", x=573, y=232, label="iq2_2: Y"),
    dict(id="pid0", signal="pid0", x=1140, y=300, label="pid0: PI output"),
    dict(id="asg0", signal="asg0", x=620, y=415, label="asg0: ramp / offset"),
    dict(id="out1", signal="out1", x=1318, y=300, label="out1: OUT1"),
    dict(id="in2", signal="in2", x=1332, y=200, label="in2: the external reference (IN2)"),
    dict(id="pid1", signal="pid1", x=900, y=512, label="pid1: error output"),
    dict(id="out2", signal="out2", x=1222, y=540, label="out2: OUT2"),
]
TERMINALS = [dict(text="IN1", x=104, y=106), dict(text="X", x=242, y=128), dict(text="Y", x=242, y=192),
             dict(text="NCO", x=222, y=328), dict(text="90°", x=222, y=172), dict(text="OUT1 → PZT driver", x=1010, y=88),
             dict(text="IN2 ← external sine (HV jumper)", x=900, y=545)]

LOCK = SimpleNamespace(BLOCKS=BLOCKS, WIRES=WIRES, SWITCHES=SWITCHES, PROBES=PROBES, TERMINALS=TERMINALS,
                       CANVAS_W=1340, CANVAS_H=570, PREFIX="")
SCOPE_KEYS = ["live_ms", "xaxis", "ramp_periods", "ui_hz", "probe_every", "probe_a", "probe_b"]


def geometry(M=LOCK):
    return dict(W=M.CANVAS_W, H=M.CANVAS_H, prefix=M.PREFIX,
                wires=[dict(id=w["id"], kind=w["kind"], pts=w["pts"]) for w in M.WIRES])


def ownership_problems(keys):
    seen = {}
    for b in BLOCKS:
        for k in [p["key"] for p in b["params"]]:
            seen.setdefault(k, []).append(b["id"])
    for k in SCOPE_KEYS:
        seen.setdefault(k, []).append("scope")
    out = [f"{k}: no home" for k in keys if k not in seen]
    out += [f"{k}: in {v}" for k, v in seen.items() if len(v) > 1]
    return out


def _f(x, fmt="{:g}"):
    return "?" if x is None else fmt.format(x)


def diagram_state(snap, sw):
    s, hw, mode, ref = snap["settings"], snap["hw"], snap["mode"], snap.get("ref") or {}
    busy = mode == "connecting"
    B = {"photodiode": [], "cavity": [], "driver": [("← OUT1 + external dither", "dim")], "mixer": [], "mixerq": [],
         "sum": [], "extgen": [("→ PZT driver (Σ)  ·  → IN2", "dim")]}
    lvl = ref.get("level")
    fnd = ref.get("find") or {}
    rl = [(f"NCO {snap.get('f_nco', 0):.4f} Hz", "")]
    if lvl is None:
        rl.append(("reference: not read yet", "dim"))
    elif ref.get("mismatch"):
        rl.append((f"WRONG FREQUENCY: IN2 {ref.get('f_in2', 0):.4f} Hz → press Find f", "warn"))
    elif not ref.get("ok"):
        rl.append(("NO REFERENCE on IN2", "warn"))
    elif not sw["track"]:
        rl.append((f"level {lvl*1e3:.1f} mV · tracking OFF", "dim"))
    else:
        r_ = ref.get("rate")
        rl.append((("LOCKED" if ref.get("locked") else "acquiring") + f" · corr {snap.get('corr', 0):+.1f}°"
                   + (f" · {r_:+.2f}°/s" if r_ is not None else ""), "" if ref.get("locked") else "warn"))
    if fnd.get("ok"):
        rl.append((f"IN2: {fnd['f']:.4f} Hz, {fnd['vpp']:.2f} Vpp" + (" CLIPPING (LV jumper?)" if fnd.get("clip")
                                                                      else ""), "warn" if fnd.get("clip") else "dim"))
    B["reference"] = rl
    B["bandpass"] = [(f"HP {_f(hw.get('iq2.acbandwidth'), '{:.1f}')} Hz", "")]
    bw = [b for b in (hw.get("iq2.bandwidth") or []) if b and b > 0]
    B["lowpass"] = [(f"LP {bw[0] if bw else 0:.1f} Hz ×{len(bw)}", ""), (f"{6*len(bw)} dB/oct · X and Y", "dim")]
    B["amp"] = [(f"×{_f(hw.get('iq2.quadrature_factor'))}", "")]
    B["phase"] = [(f"{s.get('phase_deg', 0):+.1f}° (+ tracking)", "")]
    th = snap.get("theta")
    if th:
        B["phase"].append((f"signal at {th['theta']:+.1f}° in X-Y" if th["q"] >= 3 else "X-Y: no clear axis", "dim"))
    f = [x for x in (hw.get("pid0.inputfilter") or []) if x]
    B["filter"] = [("bypassed (Filter switch off)", "dim")] if not sw["filt"] else \
        [("on, all stages 0", "dim")] if not f else \
        [(", ".join((f"LP {x:.1f}" if x > 0 else f"HP {-x:.1f}") for x in f) + " Hz", "")]
    src = {"iq2": "X", "iq2_2": "Y"}.get(hw.get("pid0.input"), hw.get("pid0.input") or "?")
    pl = [(f"input {src} · clamp {(hw.get('pid0.min_voltage') or 0)*1e3:+.0f}.."
           f"{(hw.get('pid0.max_voltage') or 0)*1e3:+.0f} mV", "dim"),
          (f"hw p {_f(hw.get('pid0.p'), '{:+.6g}')}, i {_f(hw.get('pid0.i'), '{:+.6g}')}", "")]
    iv = snap.get("ival")
    if iv is not None and hw.get("pid0.output_direct") not in (None, "off"):
        pl.append((f"integrator {iv*1e3:+.2f} mV", ""))
        lo, hi = hw.get("pid0.min_voltage"), hw.get("pid0.max_voltage")
        if lo is not None and hi is not None and hi > lo and (iv <= lo + 0.01 * (hi - lo) or iv >= hi - 0.01 * (hi - lo)):
            pl.append(("AT THE CLAMP", "warn"))
    B["pid"] = pl
    esrc = {"iq2": "X", "iq2_2": "Y"}.get(hw.get("pid1.input"), hw.get("pid1.input") or "?")
    B["errout"] = [(f"OUT2 = {_f(hw.get('pid1.p'), '{:+.4g}')} × {esrc} {(hw.get('pid1.ival') or 0)*1e3:+.1f} mV, "
                    f"±{(hw.get('pid1.max_voltage') or 0)*1e3:.0f} mV", "" if sw["eo"] else "dim")]
    a_amp, a_off = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
    if hw.get("asg0.output_direct") != "out1":
        B["ramp"] = [("off", "dim")]
    elif a_amp > 0:
        wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
        B["ramp"] = [(f"RAMP {wf} {_f(hw.get('asg0.frequency'), '{:.4g}')} Hz, "
                      f"{(a_off-a_amp)*1e3:+.0f}..{(a_off+a_amp)*1e3:+.0f} mV", "")]
    else:
        B["ramp"] = [(f"DC {a_off*1e3:+.2f} mV", "")]
    parts = [n for n, on in (("ramp" if sw["ramp"] else "offset", hw.get("asg0.output_direct") == "out1"),
                             ("PI", sw["pi"])) if on]
    B["out1"] = [("±1 V DAC" if mode != "off" else "0 V (off)", "" if mode != "off" else "warn"),
                 ("+".join(parts) if parts else "nothing", "dim")]
    B["out2"] = [("error" if sw["eo"] else "PI" if sw["pi_out2"] else "0 V", "" if (sw["eo"] or sw["pi_out2"])
                  else "dim")]
    xsel = hw.get("pid0.input") != "iq2_2"
    out_on = mode != "off"
    wires = dict(pd_in1=True, drv_cav=True, cav_pd=True, gen_drv=True, gen_in2=True, bp_mix=True, bp_mixq=True,
                 mix_lpf=True, mixq_lpf=True, lpf_amp=True, lpfq_amp=True, amp_f=xsel, ampq_f=not xsel,
                 xy_eo=sw["eo"], f_pid=True, pid_sum=sw["pi"], pid_out2=sw["pi_out2"], eo_out2=sw["eo"],
                 ref_phase=bool(ref.get("ok")), lo_mix=True, lo_90=True,
                 ramp_sum=hw.get("asg0.output_direct") == "out1", sum_out=out_on, out1_drv=out_on)
    pa, pb = s.get("probe_a", "off"), s.get("probe_b", "off")
    probes = {q["id"]: ("A" if q["signal"] and q["signal"] == pa else "B" if q["signal"] and q["signal"] == pb
                        else "") for q in PROBES}
    return dict(boxes=B, wires=wires, mode=mode, probes=probes,
                sw={x["id"]: dict(on=bool(sw[x["id"]]), busy=busy, label=x["label"]) for x in SWITCHES})
