"""
errsig2ph_schematic.py -- the 2-phase (dual-quadrature) lock-in spin-off of the error-signal + summer app
(errsig2ph_app.py) as data: one model, two consumers.

Same wiring as errsig_app.py, but the demodulation runs on iq2, the only PyRPL IQ module whose FPGA instance has both
quadratures (red_pitaya_dsp.v: `iq_2_outputs`, j = 7, QUADRATUREFILTERSTAGES = 4):
  X = 'iq2'   = in1 x LO          -> low-pass (shared register, up to 4 stages) -> x quadrature_factor
  Y = 'iq2_2' = in1 x LO(+90 deg) -> the same low-pass and gain
The error output (OUT1, pid1) passes X or Y. R = sqrt(X^2 + Y^2) and theta = atan2(Y, X) are computed in the app.

Wiring:
  IN1  <- cavity transmission photodiode
  OUT1 -> external servo box (error input):   OUT1 = gain x error + offset   (pid1 as a pass-through amplifier)
  IN2  <- external servo box (correction output; HV jumper for its +/-15 V)
  OUT2 -> PZT driver:  servo mode:  OUT2 = gain x IN2 (pid0 pass-through) + DC offset (asg0) + dither (iq2)
                       ramp mode (alignment toggle): OUT2 = ramp (asg0) + dither; the IN2 correction is off
No PID feedback runs on the Red Pitaya: pid0 and pid1 are gain stages only (I = 0). They are needed because the FPGA
cannot route the lock-in output ('iq2') or IN2 to a DAC by itself.

Writers get the controller `c` (errsig2ph_app.TwoPhaseController): settings c.s, PyRPL handle c.r, mode, helpers.
"""
import schematic as SCH

P = SCH.P
PREFIX = ""
CANVAS_W, CANVAS_H = 1340, 500
OUT_LIMIT = 0.95                       # the ramp / DC never take OUT2 beyond +/- this [V] (the DAC clips at 1 V)

BLOCKS = [
    # plant (top): servo box, PZT driver, cavity, photodiode
    dict(id="photodiode", label="Photodiode", kind="plant", x=20, y=12, w=150, h=58,
         params=[P("in1_scale", None)], opts=[],
         note="IN1 jumper: LV (+/-1 V) or HV (+/-20 V), set by hand. IN1 scale multiplies the displayed photodiode "
              "traces (1 = LV, 20 = HV). Nothing is written to the Red Pitaya."),
    dict(id="cavity", label="Cavity", kind="plant", x=330, y=12, w=150, h=58, params=[], opts=[],
         note="The cavity: its transmission is the photodiode signal; the dither makes the error signal."),
    dict(id="driver", label="PZT driver", kind="plant", x=640, y=12, w=210, h=58, params=[], opts=[],
         note="External: driven by OUT2 (correction + DC offset + dither, or the alignment ramp)."),
    dict(id="servo", label="External servo box", kind="plant", x=1060, y=12, w=240, h=58,
         params=[P("in2_scale", None)], opts=[],
         note="External: error in <- OUT1, correction out -> IN2. With its +/-15 V output use the IN2 HV jumper "
              "(+/-20 V range, 30 V absolute maximum); IN2 scale = 20 then shows the correction in servo-box volts. "
              "On LV (+/-1 V range, 6 V absolute maximum) a limiter is required."),
    # IN1 -> error -> OUT1
    dict(id="bandpass", label="Band-pass (AC high-pass)", kind="dsp", x=20, y=125, w=140, h=110,
         params=[P("ac_hz", "iq2.acbandwidth")], opts=[],
         note="iq2's input high-pass: 1 stage, 1st order, corner on a factor-2 ladder, in front of both mixers. "
              "With the low-pass after the mixers the chain is a band-pass around the dither frequency."),
    dict(id="mixer", label="×", kind="node", x=196, y=128, w=40, h=40, params=[], opts=[],
         note="iq2 X mixer: IN1 (high-passed) times the LO (the dither oscillator delayed by the phase block). "
              "X = 'iq2'."),
    dict(id="mixerq", label="×", kind="node", x=196, y=192, w=40, h=40, params=[], opts=[],
         note="iq2 Y mixer: IN1 (high-passed) times the LO shifted by 90 deg. Y = 'iq2_2' (the second quadrature, "
              "wired out only on iq2 in this FPGA)."),
    dict(id="lowpass", label="Low-pass (X and Y)", kind="dsp", x=268, y=125, w=150, h=122,
         params=[P("lpf_hz", "iq2.bandwidth"), P("lpf_order", "iq2.bandwidth", widget="radio")], opts=[],
         note="iq2 low-pass after the mixers, one register for both quadratures: order 1 to 4 (1st-order stages; iq2 "
              "is built with 4 stages, iq0 only 2), factor-2 ladder. Keep it below dither/3. It also limits the "
              "bandwidth of the error signal the servo box sees."),
    dict(id="amp", label="Amplifier", kind="dsp", x=446, y=125, w=110, h=110,
         params=[P("err_gain", "iq2.quadrature_factor", widget="int")], opts=[],
         note="iq2.quadrature_factor: integer gain, the same for X ('iq2') and Y ('iq2_2')."),
    dict(id="errout", label="Error output (pid1)", kind="dsp", x=600, y=125, w=200, h=136,
         params=[P("eo_source", "pid1.input", widget="radio"), P("eo_gain", "pid1.p"),
                 P("eo_offset_mv", "pid1.ival"), P("eo_limit_mv", "pid1.max_voltage", inline=False)], opts=[],
         note="OUT1 = gain x (X or Y) + offset, clamped to +/- the limit. X is the error once the phase is set "
              "(Auto rotates the signal into X). pid1 is only an amplifier (P = gain, I = 0, integrator value = the "
              "DC offset). A negative gain flips the error sign for the servo box."),
    dict(id="out1", label="OUT1", kind="node", x=930, y=140, w=100, h=66, params=[], opts=[],
         note="OUT1 -> external servo box (error input)."),
    # IN2 correction -> summer
    dict(id="corr", label="Correction in (IN2 → pid0)", kind="dsp", x=1000, y=230, w=230, h=90,
         params=[P("in2_gain", "pid0.p")], opts=[],
         note="IN2 (the servo box's correction) x gain into the OUT2 sum. pid0 is only an amplifier (P = gain, "
              "I = 0, clamped to +/-1 V). On in servo mode, off in ramp (alignment) mode. On the HV jumper the FPGA "
              "sees IN2/20, so gain 1 maps +/-15 V of the servo box to +/-0.75 V on OUT2."),
    # generators -> OUT2
    dict(id="phase", label="Phase delay", kind="dsp", x=141, y=250, w=150, h=74,
         params=[P("phase_deg", "iq2.phase")], opts=[],
         note="iq2.phase: rotates the LO, i.e. the (X, Y) plane. The app reads theta = the angle of the error "
              "signal in the X-Y plane from one sweep (principal axis of X vs Y across the target peak) and Auto "
              "rotates it into X (Y = 0) with a negative slope. In servo mode theta is estimated live from the "
              "residual error fluctuations."),
    dict(id="dither", label="Dither generator", kind="dsp", x=20, y=345, w=250, h=110,
         params=[P("dither_hz", "iq2.frequency"), P("dither_mvpp", "iq2.amplitude")], opts=[],
         note="iq2's NCO: the mixer LO (through the phase delay) and, through the Dither switch, summed into OUT2. "
              "Amplitude is pp on OUT2, calibrated by measuring it on OUT2 while ramping."),
    dict(id="ramp", label="Ramp / DC offset (asg0)", kind="dsp", x=320, y=345, w=250, h=142,
         params=[P("ramp_hz", "asg0.frequency", live="acquire"), P("ramp_mvpp", "asg0.amplitude", live="acquire"),
                 P("ramp_center_mv", "asg0.offset"), P("ramp_shape", "asg0.waveform", live="acquire",
                                                       inline=False)], opts=[],
         note="asg0 into OUT2. Ramp switch on (alignment): triangle or sawtooth around the setpoint; it triggers the "
              "scope. Ramp off (servo mode): the setpoint is a DC offset under the IN2 correction (asg0 offset, "
              "amplitude 0). 'Park at peak' puts the offset on the chosen peak's error zero crossing and switches "
              "to servo mode, so the servo box only supplies small corrections."),
    dict(id="sum", label="Σ", kind="node", x=1120, y=395, w=40, h=40, params=[], opts=[],
         note="Every module with output_direct = out2 is summed into OUT2: asg0 (ramp or DC), iq2 (dither) and pid0 "
              "(IN2 correction), then the DAC clips at +/-1 V."),
    dict(id="out2", label="OUT2", kind="node", x=1222, y=385, w=106, h=60, params=[], opts=[],
         note="OUT2 -> PZT driver."),
]
BLOCK = {b["id"]: b for b in BLOCKS}

HOMES = {"scope": ["averages", "prominence", "live_window_ms", "ui_hz", "probe_every"],
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
        for k in [p["key"] for p in b["params"]] + b["opts"]:
            seen.setdefault(k, []).append(b["id"])
    for h, ks in HOMES.items():
        for k in ks:
            seen.setdefault(k, []).append(h)
    out = [f"{k}: no home" for k in keys if k not in seen]
    out += [f"{k}: in {v}" for k, v in seen.items() if len(v) > 1]
    return out


WIRES = [
    dict(id="pd_in1", kind="plant", pts=[(95, 70), (95, 125)]),
    dict(id="bp_mix", kind="sig", pts=[(160, 148), (196, 148)]),
    dict(id="bp_mixq", kind="sig", pts=[(160, 212), (196, 212)]),
    dict(id="mix_lpf", kind="sig", pts=[(236, 148), (268, 148)]),
    dict(id="mixq_lpf", kind="sig", pts=[(236, 212), (268, 212)]),
    dict(id="lpf_amp", kind="sig", pts=[(418, 148), (446, 148)]),
    dict(id="lpfq_amp", kind="sig", pts=[(418, 212), (446, 212)]),
    dict(id="amp_eo", kind="sig", pts=[(556, 148), (600, 148)]),
    dict(id="ampq_eo", kind="sig", pts=[(556, 212), (600, 212)]),
    dict(id="eo_out1", kind="sig", pts=[(800, 173), (930, 173)]),
    dict(id="out1_servo", kind="plant", pts=[(980, 140), (980, 100), (1120, 100), (1120, 70)]),
    dict(id="servo_in2", kind="plant", pts=[(1270, 70), (1270, 275), (1230, 275)]),
    dict(id="corr_sum", kind="drv", pts=[(1152, 320), (1152, 397)]),
    dict(id="drv_cav", kind="plant", pts=[(640, 41), (480, 41)]),
    dict(id="cav_pd", kind="plant", pts=[(330, 41), (170, 41)]),
    dict(id="nco_phase", kind="lo", pts=[(216, 345), (216, 322)]),
    dict(id="lo_mix", kind="lo", pts=[(216, 250), (216, 232)]),
    dict(id="lo_90", kind="lo", pts=[(216, 192), (216, 168)]),
    dict(id="dith_sum", kind="drv", pts=[(270, 362), (290, 362), (290, 345), (1128, 345), (1128, 397)]),
    dict(id="ramp_sum", kind="drv", pts=[(570, 415), (1120, 415)]),
    dict(id="sum_out", kind="drv", pts=[(1160, 415), (1222, 415)]),
    dict(id="out2_drv", kind="plant", pts=[(1318, 385), (1318, 118), (745, 118), (745, 70)]),
]

SWITCHES = [
    dict(id="errout", label="Error out", x=865, y=173),
    dict(id="dither", label="Dither", x=700, y=345),
    dict(id="ramp", label="Ramp (align)", x=860, y=415),
    dict(id="output", label="Output", x=1191, y=415),
]
SWITCH_TITLES = {
    "errout": "Error out: pid1.output_direct (out1 / off). Off: OUT1 = 0 V",
    "dither": "Dither: iq2.output_direct (the dither on OUT2)",
    "ramp": "Ramp (alignment): on = OUT2 = ramp + dither (IN2 correction off); off = servo mode: OUT2 = IN2 x gain + "
            "DC offset + dither",
    "output": "Output: OUT2 enable (off: ramp/DC, dither and correction disconnected, OUT2 = 0 V)",
}

PROBES = [
    dict(id="in1", signal="in1", x=95, y=100, label="in1: photodiode (IN1)"),
    dict(id="hp", signal=None, x=178, y=148, why="after the high-pass: inside iq2, not routed to the scope"),
    dict(id="mix", signal=None, x=252, y=148, why="raw mixer output: inside iq2, not a scope signal"),
    dict(id="iq2", signal="iq2", x=578, y=148, label="iq2: X quadrature (in-phase: the error once phased)"),
    dict(id="iq2_2", signal="iq2_2", x=578, y=212, label="iq2_2: Y quadrature (90 deg)"),
    dict(id="out1", signal="out1", x=915, y=173, label="out1: error output to the servo box"),
    dict(id="in2", signal="in2", x=1270, y=200, label="in2: correction from the servo box (IN2)"),
    dict(id="pid0", signal="pid0", x=1152, y=355, label="pid0: correction into the OUT2 sum (gain x IN2)"),
    dict(id="asg0", signal="asg0", x=610, y=415, label="asg0: ramp / DC offset"),
    dict(id="out2", signal="out2", x=1318, y=362, label="out2: PZT drive (sum)"),
]
PROBE_SIGNALS = [p["signal"] for p in PROBES if p["signal"]]

TERMINALS = [dict(text="IN1", x=104, y=106), dict(text="X", x=242, y=128), dict(text="Y", x=242, y=192),
             dict(text="90°", x=222, y=172), dict(text="OUT1 → servo box", x=990, y=82),
             dict(text="IN2 ← servo box", x=1135, y=212),
             dict(text="Lock Assist: click a peak or crosshair, then Park at peak", x=300, y=262)]


def geometry():
    return dict(W=CANVAS_W, H=CANVAS_H, prefix=PREFIX,
                wires=[dict(id=w["id"], kind=w["kind"], pts=w["pts"]) for w in WIRES])


# ---------------------------------------------------------------------------------------------
# Writers (iq2, asg0, pid0, pid1 only)
# ---------------------------------------------------------------------------------------------
def w_mixer(c):
    iq = c.r.iq2
    iq.input = "in1"
    iq.gain = 0
    iq.output_signal = "quadrature"


def w_bandpass(c):
    c.r.iq2.acbandwidth = c.s["ac_hz"]


def w_lowpass(c):
    c.r.iq2.bandwidth = [c.s["lpf_hz"]] * max(1, min(int(c.s["lpf_order"]), 4))


def w_amp(c):
    c.r.iq2.quadrature_factor = c.s["err_gain"]


def w_phase(c):
    c.r.iq2.phase = c.s["phase_deg"]


def w_dither(c):
    s = c.s
    if (s["dither_hz"], s["dither_mvpp"]) != c._last_dither:
        c._last_dither = (s["dither_hz"], s["dither_mvpp"])
        c._cal_checks = 2
    iq = c.r.iq2
    iq.frequency = s["dither_hz"]
    iq.amplitude = s["dither_mvpp"] * 1e-3 / 2 / c.amp_cal
    iq.output_direct = "out2" if (c.dither_on and s["dither_mvpp"] > 0 and c.mode != "off") else "off"


def _passthrough(p, inp, gain, offset, lim, out):
    p.i = 0
    p.input = inp
    p.setpoint = 0
    p.p = gain
    p.min_voltage, p.max_voltage = -lim, lim
    p.ival = max(min(offset, lim), -lim)
    try:
        p.inputfilter = [0, 0, 0, 0]
    except Exception:
        pass
    p.output_direct = out


def w_errout(c):
    """pid1: OUT1 = p * (X or Y) + ival, clamped. I = 0 (no feedback)."""
    s = c.s
    lim = min(abs(s["eo_limit_mv"]), 1000.0) * 1e-3
    _passthrough(c.r.pid1, "iq2_2" if s["eo_source"] == "Y" else "iq2", s["eo_gain"], s["eo_offset_mv"] * 1e-3, lim, "out1" if c.errout_on else "off")


def w_corr(c):
    """pid0: IN2 x gain into the OUT2 sum in servo mode (off while ramping or with Output off). I = 0."""
    _passthrough(c.r.pid0, "in2", c.s["in2_gain"], 0.0, 1.0, "out2" if c.mode == "servo" else "off")


def w_ramp(c):
    """Ramp mode: the ramp around the setpoint. Servo mode: the setpoint as a DC offset (asg0, amplitude 0)."""
    if c.mode == "acquire":
        s = c.s
        lo, hi = c._ramp_limits(s["ramp_center_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3)
        c._ramp_on(lo, hi, s["ramp_hz"], s["ramp_shape"])
    elif c.mode == "servo":
        c._dc(c.s["ramp_center_mv"] * 1e-3)


WRITERS = dict(mixer=w_mixer, bandpass=w_bandpass, lowpass=w_lowpass, amp=w_amp, phase=w_phase, dither=w_dither,
               errout=w_errout, corr=w_corr, ramp=w_ramp)
STATIC = ["mixer", "dither", "phase", "bandpass", "lowpass", "amp", "errout", "corr"]
LIVE = ["ramp"]


def write_static(c):
    for b in STATIC:
        WRITERS[b](c)


def apply_changed(c, changed):
    owners = {OWNER.get(k) for k in changed}
    if "dither" in owners:
        owners.add("ramp")                  # the ramp limits leave room for the dither
    done = []
    for b in STATIC + LIVE:
        if b in owners:
            WRITERS[b](c)
            done.append(b)
    return done


READBACK = {
    "iq2": ["frequency", "amplitude", "phase", "bandwidth", "acbandwidth", "quadrature_factor", "output_direct"],
    "asg0": ["waveform", "frequency", "amplitude", "offset", "output_direct"],
    "pid1": ["input", "p", "i", "ival", "min_voltage", "max_voltage", "output_direct"],
    "pid0": ["input", "p", "i", "output_direct"],
}


def read_hw(r):
    return SCH.read_hw(r, READBACK)


# ---------------------------------------------------------------------------------------------
# Switch states and the diagram state
# ---------------------------------------------------------------------------------------------
BUSY = ("busy", "connecting")


def switch_states(mode, hw, s, errout_on):
    return dict(errout=bool(errout_on) and hw.get("pid1.output_direct") == "out1",
                dither=hw.get("iq2.output_direct") == "out2",
                ramp=mode in ("acquire", "busy"),
                output=mode != "off")


def _f(x, fmt="{:g}"):
    return "?" if x is None else fmt.format(x)


def diagram_state(snap):
    s, hw, mode = snap["settings"], snap["hw"], snap["mode"]
    sw = switch_states(mode, hw, s, snap["errout_on"])
    W = "warn"
    B = {}
    B["photodiode"] = [(f"IN1 scale ×{s['in1_scale']:g}", "")]
    B["cavity"] = []
    B["driver"] = [("← OUT2", "dim")]
    in2 = snap.get("in2_now")
    B["servo"] = [("error ← OUT1 · correction → IN2", "dim")]
    B["bandpass"] = [("HP " + SCH._req(snap, "ac_hz", hw.get("iq2.acbandwidth"), "{:.1f}", " Hz"), ""),
                     ("1 stage, 1st order", "dim")]
    B["mixer"] = []
    B["mixerq"] = []
    bw = [b for b in (hw.get("iq2.bandwidth") or []) if b and b > 0]
    lp = [("LP " + SCH._req(snap, "lpf_hz", bw[0] if bw else None, "{:.1f}", " Hz") + f" ×{len(bw)}", ""),
          (f"{len(bw)}×1st order, {6*len(bw)} dB/oct · X and Y", "dim")]
    if bw and bw[0] > s["dither_hz"] / 3:
        lp.append(("above dither/3: ripple", W))
    B["lowpass"] = lp
    B["amp"] = [("×" + SCH._req(snap, "err_gain", hw.get("iq2.quadrature_factor"), "{:g}") + " (integer)", "")]
    an = snap.get("analysis") or {}
    if an.get("err_max", 0) > 0.95 and mode == "acquire":
        B["amp"].append(("error clips: lower it", W))
    p, iv = hw.get("pid1.p"), hw.get("pid1.ival")
    src = {"iq2": "X", "iq2_2": "Y"}.get(hw.get("pid1.input"), hw.get("pid1.input") or "?")
    eo = [(f"OUT1 = {_f(p, '{:+.4g}')} × {src} {(iv or 0)*1e3:+.1f} mV", "" if sw["errout"] else "dim"),
          (f"clamp ±{(hw.get('pid1.max_voltage') or 0)*1e3:.0f} mV · " + ("ON" if sw["errout"] else "off"), "dim")]
    sl = snap.get("slope")
    if sl:
        eo.append((f"slope {sl*(p or 0):+.3g} V OUT1 / V OUT2", ""))
    B["errout"] = eo
    B["out1"] = [("→ servo box" if sw["errout"] else "0 V", "dim")]
    corr_on = hw.get("pid0.output_direct") == "out2"
    cl = [(f"OUT2 += {_f(hw.get('pid0.p'), '{:+.4g}')} × IN2 · " + ("ON" if corr_on else "off (ramp mode)"),
           "" if corr_on else "dim")]
    if in2 is not None:
        cl.append((f"IN2 {in2*1e3:+.1f} mV raw · {in2*s['in2_scale']:+.3g} V at box", "dim"))
    B["corr"] = cl
    B["phase"] = [(f"{_f(hw.get('iq2.phase'), '{:+.1f}')}°", "")]
    xy = snap.get("xy") or {}
    if xy.get("theta") is not None:
        good = xy.get("q", 0) >= 3
        B["phase"].append((f"θ {xy['theta']:+.1f}° ({xy.get('src', '')})" if good else "θ: no clear axis",
                           W if good and abs(xy["theta"]) > 10 else "dim"))
    amp_hw = hw.get("iq2.amplitude")
    B["dither"] = [(f"{_f(hw.get('iq2.frequency'), '{:.6g}')} Hz, {s['dither_mvpp']:g} mVpp"
                    + (f" (OUT2 {snap['dither_meas']:.1f})" if snap.get("dither_meas") else "")
                    + ("" if sw["dither"] else " (off)"), "")]
    if amp_hw is not None:
        B["dither"].append((f"register {amp_hw*2e3:.2f} mVpp (cal ×{snap.get('amp_cal', 1):.3f})", "dim"))
    if mode in ("acquire", "busy") and (hw.get("asg0.amplitude") or 0) > 0:
        a, o = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
        wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
        B["ramp"] = [(f"RAMP {wf} {_f(hw.get('asg0.frequency'), '{:.3g}')} Hz", ""),
                     (f"{(o-a)*1e3:+.0f}..{(o+a)*1e3:+.0f} mV", "dim")]
    elif mode == "servo":
        B["ramp"] = [(f"DC offset {(hw.get('asg0.offset') or 0)*1e3:+.2f} mV (servo mode)", "")]
    else:
        B["ramp"] = [("off", "dim")]
    B["sum"] = []
    parts = [n for n, on in (("ramp" if mode in ("acquire", "busy") else "DC", hw.get("asg0.output_direct") == "out2"),
                             ("correction", corr_on), ("dither", sw["dither"])) if on]
    B["out2"] = [("±1 V DAC", "") if mode != "off" else ("0 V (off)", W),
                 ("+".join({"ramp": "ramp", "DC": "DC", "correction": "corr", "dither": "dither"}[x] for x in parts)
                  if parts else "nothing", "dim")]
    xsel = hw.get("pid1.input") != "iq2_2"
    wires = dict(pd_in1=True, bp_mix=True, bp_mixq=True, mix_lpf=True, mixq_lpf=True, lpf_amp=True, lpfq_amp=True,
                 amp_eo=xsel, ampq_eo=not xsel, lo_90=True, eo_out1=sw["errout"],
                 out1_servo=sw["errout"], servo_in2=True, corr_sum=corr_on, drv_cav=True, cav_pd=True,
                 nco_phase=True, lo_mix=True, dith_sum=sw["dither"], ramp_sum=hw.get("asg0.output_direct") == "out2",
                 sum_out=mode != "off", out2_drv=mode != "off")
    busy = mode in BUSY
    swd = {k: dict(on=bool(on), busy=busy, label=next(x["label"] for x in SWITCHES if x["id"] == k))
           for k, on in sw.items()}
    pa, pb = s.get("probe_a", "off"), s.get("probe_b", "off")
    probes = {q["id"]: ("A" if q["signal"] and q["signal"] == pa else "B" if q["signal"] and q["signal"] == pb
                        else "") for q in PROBES}
    return dict(boxes=B, wires=wires, sw=swd, probes=probes, mode=mode)
