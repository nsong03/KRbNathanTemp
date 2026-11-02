"""
schematic.py -- the block diagram of the cavity lock as data: one model, two consumers.

  * Hardware: every block has a writer that sets exactly the PyRPL registers drawn on that block.
    Controller.configure() runs the static writers; an apply runs the writers of the blocks that own the
    changed keys (see apply_changed).
  * Drawing:  the Dash UI places the boxes, switches and probe points from BLOCKS / SWITCHES / PROBES and the
    browser draws WIRES as SVG. diagram_state() turns the controller snapshot (register readback + mode) into
    the readback text on each box, the live/dead state of each wire and the switch positions.

Nothing here imports the app: writers get the Controller and use its settings (ctl.s), its PyRPL handle
(ctl.r) and a few of its helpers (_dither, _ramp_limits, _ramp_on, _set_gains, _clamp).
"""
import math

OUT_LIMIT = 0.95                      # never command OUT1 beyond +/- this [V] (the DAC saturates at +/- 1 V)
I_STEP = 1.0 / (2 ** 32 * 2 * math.pi * 8e-9)   # pid.i register resolution [Hz] (~0.0046 Hz)
CANVAS_W, CANVAS_H = 1340, 470


def P(key, attr, live="always", inline=True, widget=None):
    """A block parameter: FIELDS key -> PyRPL attribute it writes ('module.attr'; None = display only).
    live: when the register is written: always | acquire (scan on) | lock_ctx (dither in its lock context) |
    locked (loop closed)."""
    return dict(key=key, attr=attr, live=live, inline=inline, widget=widget)


# ---------------------------------------------------------------------------------------------
# Blocks. kind: dsp (FPGA), node (mixer / sum / terminal), plant (external, display only)
# ---------------------------------------------------------------------------------------------
BLOCKS = [
    # plant: OUT1 -> PZT driver -> cavity -> photodiode -> IN1 (drawn right to left along the top)
    dict(id="photodiode", label="Photodiode", kind="plant", x=20, y=12, w=150, h=58,
         params=[P("in1_scale", None)], opts=[],
         note="IN1 jumper: LV (+/-1 V) or HV (+/-20 V), set by hand on the board. IN1 scale multiplies the "
              "displayed photodiode traces (1 = LV, 20 = HV). Nothing is written to the Red Pitaya."),
    dict(id="cavity", label="Cavity", kind="plant", x=330, y=12, w=150, h=58,
         params=[P("kappa_mhz", None)], opts=[],
         note="Linewidth kappa: used only to convert detuning from volts of drive to Hz."),
    dict(id="driver", label="PZT driver + PZT", kind="plant", x=640, y=12, w=170, h=58,
         params=[P("driver_bw_hz", None)], opts=[],
         note="Bandwidth of driver + PZT (one pole). Used by the loop model and the predicted phase margin "
              "only; measure it with Measure loop."),
    # signal chain
    dict(id="bandpass", label="Band-pass (AC high-pass)", kind="dsp", x=20, y=125, w=140, h=110,
         params=[P("ac_hz", "iq0.acbandwidth")], opts=[],
         note="iq0's input high-pass: 1 stage, 1st order, corner on a factor-2 ladder. Together with the "
              "low-pass after the mixer it makes the chain a band-pass around the dither frequency. "
              "(No true band-pass or notch filter is used.)"),
    dict(id="mixer", label="x", kind="node", x=196, y=153, w=40, h=40, params=[], opts=[],
         note="iq0 mixer: IN1 (high-passed) times the LO. Demodulation source = Modulation: the LO is the "
              "dither oscillator itself, delayed by the phase block. iq0.input = in1."),
    dict(id="lowpass", label="Low-pass", kind="dsp", x=268, y=125, w=150, h=110,
         params=[P("lpf_hz", "iq0.bandwidth"), P("lpf_order", "iq0.bandwidth", widget="radio")], opts=[],
         note="iq0 low-pass after the mixer: order 1 or 2, each stage 1st order (6 dB/oct per stage), corners "
              "on a factor-2 ladder. Keep it below dither/3 so the dither ripple is filtered out."),
    dict(id="amp", label="Amplifier", kind="dsp", x=446, y=125, w=110, h=110,
         params=[P("err_gain", "iq0.quadrature_factor", widget="int")], opts=[],
         note="iq0.quadrature_factor: an integer gain (1 = lowest useful). Its output is the error signal "
              "'iq0' that pid0 and the scope see."),
    dict(id="setpoint", label="Setpoint", kind="dsp", x=606, y=125, w=110, h=110,
         params=[P("setpoint_mv", "pid0.setpoint")], opts=[],
         note="pid0 subtracts this from the error. 0 = lock on the error zero crossing; a non-zero value moves "
              "the lock point by setpoint / slope."),
    dict(id="pid", label="PID (P + I)", kind="dsp", x=800, y=125, w=140, h=110,
         params=[P("ugf_hz", "pid0.i", live="locked"), P("p_gain", "pid0.p", live="locked", inline=False)],
         opts=["refine_phase", "gain_ramp"],
         note="pid0. I is given as the loop unity-gain frequency: i = -ugf/S with S the measured error slope "
              "per OUT1 volt, so |i*S| is the loop bandwidth whatever the sign of S. P is normalized the same "
              "way. Written only while locked (i = 0 otherwise). Lock stages: scan off, park at the zero "
              "crossing, I at ugf/10, ugf/3, ugf."),
    dict(id="limiter", label="Limiter", kind="dsp", x=968, y=125, w=118, h=110,
         params=[P("clamp_auto", "pid0.min_voltage", live="locked"),
                 P("clamp_mv", "pid0.max_voltage", live="locked", inline=False)],
         opts=["auto_relock", "unlock_frac", "unlock_s"],
         note="pid0 output clamp around the lock point: +/-0.4 x the distance to the nearest other mode "
              "(auto) or +/- the half-width. Outside a lock it is +/-0.95 V. The watchdog (Auto-relock "
              "switch) declares an unlock when the transmission stays low or the output hits the clamp."),
    dict(id="sum", label="Σ", kind="node", x=1120, y=153, w=40, h=40, params=[], opts=[],
         note="Every module with output_direct = out1 is summed digitally into OUT1 (ramp asg0, dither iq0, "
              "PID pid0), then the DAC saturates at +/-1 V."),
    dict(id="out1", label="OUT1", kind="node", x=1222, y=140, w=106, h=66, params=[], opts=[],
         note="OUT1 -> PZT driver MOD IN. The app never commands beyond +/-0.95 V (ramp limits include the "
              "dither)."),
    # generators
    dict(id="phase", label="Phase delay", kind="dsp", x=141, y=250, w=150, h=74,
         params=[P("phase_deg", "iq0.phase")], opts=[],
         note="iq0.phase: delay of the LO against the dither. Auto fits it from 4 phases (cos^2 fit of the "
              "error contrast) at the target peak; LOCK re-fits it at the lock dither."),
    dict(id="dither", label="Dither generator", kind="dsp", x=20, y=345, w=250, h=110,
         params=[P("acq_dither_hz", "iq0.frequency"), P("acq_dither_mvpp", "iq0.amplitude"),
                 P("lock_same_dither", "iq0.frequency", live="lock_ctx", inline=False),
                 P("lock_dither_hz", "iq0.frequency", live="lock_ctx", inline=False),
                 P("lock_dither_mvpp", "iq0.amplitude", live="lock_ctx", inline=False)],
         opts=[],
         note="iq0's NCO. It is the mixer LO (through the phase delay) and, through the Dither switch, it is "
              "summed into OUT1 (iq0.output_direct). Two contexts: ACQ (scan) and LOCK; the lock dither "
              "equals the acquisition dither unless unticked. Amplitude is pp on OUT1, calibrated by "
              "measuring it on OUT1."),
    dict(id="ramp", label="Ramp / scan", kind="dsp", x=320, y=345, w=230, h=110,
         params=[P("ramp_hz", "asg0.frequency", live="acquire"), P("ramp_mvpp", "asg0.amplitude", live="acquire"),
                 P("ramp_shape", "asg0.waveform", live="acquire", inline=False),
                 P("ramp_center_mv", "asg0.offset", live="acquire", inline=False)],
         opts=[],
         note="asg0: triangle ('ramp') or sawtooth ('halframp'), summed into OUT1 through the Scan switch. "
              "It triggers the scope. Clipped so ramp + dither stay inside +/-0.95 V. Also the swept-sine "
              "generator of 'Measure Bode plot': closed loop = a small sine into OUT1 while locked (the ramp is "
              "off then); circuit only = it drives spare copies iq1 -> pid1 with their outputs off."),
]
BLOCK = {b["id"]: b for b in BLOCKS}

# keys that do not write a register: they live in the popover of the panel they configure
HOMES = {
    "scope": ["averages", "prominence", "lock_window_ms", "ui_hz", "drive_every"],
    "scope_bar": ["panels", "probe_a", "probe_b"],
    "measure": ["noise_avg", "na_start_hz", "na_stop_hz", "na_points", "na_amp_frac", "bode_start_hz",
                "bode_stop_hz", "bode_points", "bode_cycles", "bode_amp_mv"],
    "measure_bar": ["bode_mode"],
}

OWNER = {}                                         # FIELDS key -> block id (or home name)
for _b in BLOCKS:
    for _p in _b["params"]:
        OWNER[_p["key"]] = _b["id"]
    for _k in _b["opts"]:
        OWNER[_k] = _b["id"]
for _h, _ks in HOMES.items():
    for _k in _ks:
        OWNER[_k] = _h


def ownership_problems(keys):
    """Each FIELDS key must have exactly one home; returns a list of problems (empty = fine)."""
    seen = {}
    for b in BLOCKS:
        for k in [p["key"] for p in b["params"]] + b["opts"]:
            seen.setdefault(k, []).append(b["id"])
    for h, ks in HOMES.items():
        for k in ks:
            seen.setdefault(k, []).append(h)
    out = [f"{k}: no home" for k in keys if k not in seen]
    out += [f"{k}: in {v}" for k, v in seen.items() if len(v) > 1]
    out += [f"{k}: not a FIELDS key" for k in seen if k not in keys]
    return out


# ---------------------------------------------------------------------------------------------
# Wires, switches, probes (canvas pixels)
# ---------------------------------------------------------------------------------------------
# kind: sig (error path), lo (oscillator), drv (drive to OUT1), plant (external), mon (OUT2)
WIRES = [
    dict(id="pd_in1", kind="plant", pts=[(95, 70), (95, 125)]),
    dict(id="bp_mix", kind="sig", pts=[(160, 173), (196, 173)]),
    dict(id="mix_lpf", kind="sig", pts=[(236, 173), (268, 173)]),
    dict(id="lpf_amp", kind="sig", pts=[(418, 173), (446, 173)]),
    dict(id="amp_sp", kind="sig", pts=[(556, 173), (606, 173)]),
    dict(id="sp_pid", kind="sig", pts=[(716, 173), (800, 173)]),
    dict(id="pid_lim", kind="drv", pts=[(940, 173), (968, 173)]),
    dict(id="lim_sum", kind="drv", pts=[(1086, 173), (1120, 173)]),
    dict(id="sum_out", kind="drv", pts=[(1160, 173), (1222, 173)]),
    dict(id="out_plant", kind="plant", pts=[(1275, 140), (1275, 41), (810, 41)]),
    dict(id="drv_cav", kind="plant", pts=[(640, 41), (480, 41)]),
    dict(id="cav_pd", kind="plant", pts=[(330, 41), (170, 41)]),
    dict(id="nco_phase", kind="lo", pts=[(216, 345), (216, 322)]),
    dict(id="lo_mix", kind="lo", pts=[(216, 250), (216, 193)]),
    dict(id="dith_sum", kind="drv", pts=[(270, 362), (290, 362), (290, 333), (1132, 333), (1132, 193)]),
    dict(id="ramp_sum", kind="drv", pts=[(550, 410), (1148, 410), (1148, 193)]),
]

SWITCHES = [
    dict(id="lock", label="Lock", x=766, y=173),            # (x, y) = centre of the switch, on its wire
    dict(id="output", label="Output", x=1191, y=173),
    dict(id="dither", label="Dither", x=860, y=333),
    dict(id="scan", label="Scan", x=860, y=410),
    dict(id="relock", label="Auto-relock", x=1027, y=262),
]

# signal: a real DSP signal name the scope can show; None = not probe-able (why says so)
PROBES = [
    dict(id="in1", signal="in1", x=95, y=100, label="in1: photodiode (IN1)"),
    dict(id="hp", signal=None, x=178, y=173,
         why="after the high-pass: inside iq0, not routed to the scope"),
    dict(id="mix", signal=None, x=252, y=173,
         why="raw mixer output: inside iq0. output_signal 'quadrature_hf' might expose it, but that re-routes "
             "iq0's output (it would break the lock) and is unverified"),
    dict(id="iq0", signal="iq0", x=581, y=173, label="iq0: error signal"),
    dict(id="sp", signal=None, x=727, y=173,
         why="error minus setpoint exists only inside pid0: probe iq0 and subtract the setpoint"),
    dict(id="pid0", signal="pid0", x=1103, y=173, label="pid0: PID output (= setpoint when locked)"),
    dict(id="nco", signal=None, x=216, y=333,
         why="the bare dither oscillator is not a scope signal; it shows on out1 when the ramp and PID are off"),
    dict(id="asg0", signal="asg0", x=610, y=410, label="asg0: ramp"),
    dict(id="out1", signal="out1", x=1275, y=100, label="out1: total drive (ramp + dither + PID)"),
]
PROBE_SIGNALS = [p["signal"] for p in PROBES if p["signal"]]

TERMINALS = [dict(text="IN1", x=104, y=106), dict(text="Lock Assist: click a crosshair or peak in the scope",
                                                    x=300, y=262)]


def geometry():
    """Static drawing data for the browser (wires)."""
    return dict(W=CANVAS_W, H=CANVAS_H, wires=[dict(id=w["id"], kind=w["kind"], pts=w["pts"]) for w in WIRES])


# ---------------------------------------------------------------------------------------------
# Block writers: the only place the static registers are written from settings
# ---------------------------------------------------------------------------------------------
def w_mixer(ctl):
    iq = ctl.r.iq0
    iq.input = "in1"
    iq.gain = 0
    iq.output_signal = "quadrature"


def w_bandpass(ctl):
    ctl.r.iq0.acbandwidth = ctl.s["ac_hz"]


def w_lowpass(ctl):
    ctl.r.iq0.bandwidth = [ctl.s["lpf_hz"]] * int(ctl.s["lpf_order"])


def w_amp(ctl):
    ctl.r.iq0.quadrature_factor = ctl.s["err_gain"]


def w_phase(ctl):
    ctl.r.iq0.phase = ctl.s["phase_deg"]


def w_dither(ctl):
    f_d, mvpp = ctl._dither()
    if (f_d, mvpp) != ctl._last_dither:
        ctl._last_dither = (f_d, mvpp)
        ctl._cal_checks = 2                          # re-check the dither amplitude on OUT1
    iq = ctl.r.iq0
    iq.frequency = f_d
    iq.amplitude = mvpp * 1e-3 / 2 / ctl.amp_cal
    iq.output_direct = "out1" if (ctl.dither_on and mvpp > 0 and ctl.mode != "off") else "off"


def w_setpoint(ctl):
    ctl.r.pid0.setpoint = ctl.s["setpoint_mv"] * 1e-3


def w_ramp(ctl):
    """asg0 is live only while scanning (mode acquire); the lock sequence drives it itself otherwise."""
    if ctl.mode != "acquire":
        return
    s = ctl.s
    lo, hi = ctl._ramp_limits(s["ramp_center_mv"] * 1e-3, s["ramp_mvpp"] * 1e-3)
    ctl._ramp_on(lo, hi, s["ramp_hz"], s["ramp_shape"])


def w_pid(ctl):
    if ctl.mode == "locked" and ctl.lock_info.get("slope"):
        ctl._set_gains(ctl.s["ugf_hz"])


def w_limiter(ctl):
    li = ctl.lock_info
    if ctl.mode in ("locked", "measuring") and li.get("v0") is not None:
        lo, hi = ctl._clamp(li["v0"], li.get("spacing"))
        ctl.r.pid0.min_voltage, ctl.r.pid0.max_voltage = lo, hi
        li["clamp"] = (lo, hi)


WRITERS = dict(mixer=w_mixer, bandpass=w_bandpass, lowpass=w_lowpass, amp=w_amp, phase=w_phase, dither=w_dither,
               setpoint=w_setpoint, ramp=w_ramp, pid=w_pid, limiter=w_limiter)
STATIC = ["mixer", "dither", "phase", "bandpass", "lowpass", "amp", "setpoint"]   # configure() order
LIVE = ["ramp", "pid", "limiter"]


def write_static(ctl):
    for b in STATIC:
        WRITERS[b](ctl)


def apply_changed(ctl, changed):
    """Run the writers of the blocks that own the changed keys. Returns the block ids written."""
    owners = {OWNER.get(k) for k in changed}
    if "dither" in owners:
        owners.add("ramp")                          # the ramp limits leave room for the dither
    done = []
    for b in STATIC + LIVE:
        if b in owners:
            WRITERS[b](ctl)
            done.append(b)
    return done


# ---------------------------------------------------------------------------------------------
# Register readback (taken after every command, never in the fast tick)
# ---------------------------------------------------------------------------------------------
READBACK = {
    "iq0": ["frequency", "amplitude", "phase", "bandwidth", "acbandwidth", "quadrature_factor", "output_direct",
            "input"],
    "pid0": ["i", "p", "setpoint", "min_voltage", "max_voltage", "output_direct", "input"],
    "asg0": ["waveform", "frequency", "amplitude", "offset", "output_direct"],
}


def read_hw(r, readback=None):
    hw = {}
    for mod, attrs in (readback or READBACK).items():
        m = getattr(r, mod)
        for a in attrs:
            try:
                v = getattr(m, a)
                if a in ("bandwidth", "inputfilter"):
                    v = [float(x) for x in (v if hasattr(v, "__len__") else [v])]
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = float(v)
                elif not isinstance(v, (str, list, bool)) and v is not None:
                    v = str(v)
                hw[f"{mod}.{a}"] = v
            except Exception as e:                   # a register that cannot be read shows as missing
                hw[f"{mod}.{a}"] = None
    return hw


# ---------------------------------------------------------------------------------------------
# Switches (derived, never stored) and the diagram state for the browser
# ---------------------------------------------------------------------------------------------
BUSY = ("locking", "busy", "measuring", "connecting")


def switch_states(mode, hw, s):
    """True/False for every switch, from the register readback and the mode."""
    return dict(scan=hw.get("asg0.output_direct") == "out1" and (hw.get("asg0.amplitude") or 0) > 0
                and hw.get("asg0.waveform") != "sin",
                dither=hw.get("iq0.output_direct") == "out1",
                lock=mode in ("locking", "locked", "measuring"),
                output=mode != "off",
                relock=bool(s.get("auto_relock")))


def _f(x, fmt="{:g}"):
    return "?" if x is None else fmt.format(x)


def _req(snap, key, hwval, fmt="{:g}", unit=""):
    """'250 -> 303.6 Hz' when the requested value differs from what the hardware uses, else '303.6 Hz'."""
    rq = snap["req"].get(key)
    if rq is not None and hwval is not None and fmt.format(rq) != fmt.format(hwval) \
            and abs(rq - hwval) > 1e-3 * max(abs(rq), 1):
        return f"{rq:g} → {fmt.format(hwval)}{unit}"
    return f"{_f(hwval, fmt)}{unit}"


def diagram_state(snap, pm=None):
    """Readback lines per box [(text, css)], wire live flags, switch states and probe labels."""
    s, hw, mode, li = snap["settings"], snap["hw"], snap["mode"], snap["lock"]
    sw = switch_states(mode, hw, s)
    bw = [b for b in (hw.get("iq0.bandwidth") or []) if b and b > 0]
    f_d, d_mvpp = snap["dither"]
    W = "warn"
    B = {}
    # plant
    B["photodiode"] = [(f"IN1 scale ×{s['in1_scale']:g} " + ("(LV)" if s["in1_scale"] == 1 else
                                                           "(HV)" if s["in1_scale"] == 20 else ""), "")]
    kap = snap.get("kappa")
    B["cavity"] = [(f"κ {s['kappa_mhz']:g} MHz" + (f" = {kap*1e3:.2f} mV" if kap else ""), "")]
    B["driver"] = [(f"pole {s['driver_bw_hz']:g} Hz (assumed)", "")]
    # chain
    B["bandpass"] = [("HP " + _req(snap, "ac_hz", hw.get("iq0.acbandwidth"), "{:.1f}", " Hz"), ""),
                     ("1 stage, 1st order", "dim")]
    B["mixer"] = []
    lp = [("LP " + _req(snap, "lpf_hz", bw[0] if bw else None, "{:.1f}", " Hz") + f" ×{len(bw)}", ""),
          (f"{len(bw)}×1st order, {6*len(bw)} dB/oct", "dim")]
    if bw and bw[0] > f_d / 3:
        lp.append(("above dither/3: ripple", W))
    B["lowpass"] = lp
    qf = hw.get("iq0.quadrature_factor")
    am = [("×" + _req(snap, "err_gain", qf, "{:g}") + " (integer)", "")]
    an = snap.get("analysis") or {}
    if an.get("err_max", 0) > 0.95 and mode == "acquire":
        am.append(("error clips: lower it", W))
    B["amp"] = am
    sp = hw.get("pid0.setpoint")
    spl = [(f"{_f(None if sp is None else sp*1e3, '{:+.1f}')} mV", "")]
    if li.get("slope") and sp:
        spl.append((f"shifts lock {sp/li['slope']*1e3:+.3f} mV", "dim"))
    B["setpoint"] = spl
    # PID: I as the loop unity-gain frequency, with the i-resolution warning
    i_hw, S = hw.get("pid0.i"), li.get("slope")
    pidl = []
    if mode in ("locked", "measuring") and S:
        pidl.append((f"i {_f(i_hw, '{:+.4g}')} Hz", ""))
        pidl.append((f"|i·S| = UGF {abs((i_hw or 0)*S):.2f} Hz", ""))
        i_want = s["ugf_hz"] / abs(S)
        if not i_hw:
            pidl.append((f"I < i step {I_STEP:.4f} Hz!", W))
        elif abs(abs(i_hw) - i_want) > 0.1 * i_want:
            pidl.append(("I quantized > 10 %", W))
    else:
        pidl.append(("open: i = 0, p = 0" if not i_hw else f"i {i_hw:+.4g} Hz", ""))
        pidl.append((f"i step {I_STEP:.4f} Hz", "dim"))
    if pm and pm[0]:
        pidl.append((f"model PM {pm[1]:.0f}° @{pm[0]:.0f} Hz", W if pm[1] < 45 else "dim"))
    B["pid"] = pidl
    lo, hi = hw.get("pid0.min_voltage"), hw.get("pid0.max_voltage")
    if lo is not None and hi is not None and max(abs(lo), abs(hi)) >= 0.1:
        lim = [(f"{lo:+.3f}..{hi:+.3f} V", "")]
    else:
        lim = [(f"{_f(None if lo is None else lo*1e3, '{:+.1f}')}..{_f(None if hi is None else hi*1e3, '{:+.1f}')} mV",
                "")]
    if hw.get("pid0.output_direct") == "out1":
        iv = snap.get("ival")
        if iv is not None:
            lim.append((f"out {iv*1e3:+.2f} mV", "dim"))
    B["limiter"] = lim
    sine = hw.get("asg0.waveform") == "sin" and (hw.get("asg0.amplitude") or 0) > 0
    parts = [n for n, on in (("ramp", sw["scan"]),
                             ("test sine", sine and hw.get("asg0.output_direct") == "out1"),
                             ("dither", hw.get("iq0.output_direct") == "out1"),
                             ("PID", hw.get("pid0.output_direct") == "out1")) if on]
    B["sum"] = []
    B["out1"] = [("limit ±0.95 V", "") if mode != "off" else ("0 V (Output off)", W),
                 ("= " + " + ".join(parts) if parts else "nothing", "dim")]
    B["phase"] = [(f"{_f(hw.get('iq0.phase'), '{:+.1f}')}°", "")]
    ctx = snap["ctx"]
    amp_hw = hw.get("iq0.amplitude")
    dl = [(f"{'LOCK' if ctx == 'lock' else 'ACQ'} context live" + ("" if sw["dither"] else " (off)"),
           "badge-lock" if ctx == "lock" else "badge-acq"),
          (f"{_f(hw.get('iq0.frequency'), '{:.6g}')} Hz, {d_mvpp:g} mVpp"
           + (f" (OUT1 {snap['dither_meas']:.1f})" if snap.get("dither_meas") else ""), "")]
    if amp_hw is not None:
        dl.append((f"register {amp_hw*2e3:.2f} mVpp (cal ×{snap.get('amp_cal', 1):.3f})", "dim"))
    if s["lock_same_dither"]:
        dl.append(("lock dither = acquisition", "dim"))
    else:
        dl.append((f"lock: {s['lock_dither_hz']:g} Hz, {s['lock_dither_mvpp']:g} mVpp", "dim"))
    B["dither"] = dl
    if sw["scan"]:
        a, o = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
        wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
        tag = "lock sweep: " if mode in ("locking", "busy") and snap.get("ctx") == "lock" else ""
        B["ramp"] = [(f"{tag}{wf} {_f(hw.get('asg0.frequency'), '{:.3g}')} Hz", ""),
                     (f"{(o-a)*1e3:+.0f}..{(o+a)*1e3:+.0f} mV", "dim")]
    elif sine:
        a0 = hw["asg0.amplitude"]
        B["ramp"] = [(f"Bode test sine {_f(hw.get('asg0.frequency'), '{:.3g}')} Hz", ""),
                     (f"{a0*1e3:.3g} mV → " + ("OUT1" if hw.get("asg0.output_direct") == "out1"
                                               else "iq1/pid1 copies (outputs off)"), "dim")]
    else:
        B["ramp"] = [("off", "dim")]
    wires = dict(
        pd_in1=True, bp_mix=True, mix_lpf=True, lpf_amp=True, amp_sp=True, nco_phase=True, lo_mix=True,
        sp_pid=hw.get("pid0.input") == "iq0" and bool(hw.get("pid0.i")),
        pid_lim=hw.get("pid0.output_direct") == "out1", lim_sum=hw.get("pid0.output_direct") == "out1",
        sum_out=mode != "off", out_plant=mode != "off", drv_cav=True, cav_pd=True,
        dith_sum=sw["dither"], ramp_sum=sw["scan"] or (sine and hw.get("asg0.output_direct") == "out1"))
    busy = mode in BUSY
    swd = {}
    for k, on in sw.items():
        lab = next(x["label"] for x in SWITCHES if x["id"] == k)
        if k == "scan" and on and mode in ("locking", "busy") and snap.get("ctx") == "lock":
            lab = "Scan (lock sweep)"
        if k == "lock" and mode == "locking":
            lab = "Lock: locking…"
        swd[k] = dict(on=bool(on), busy=busy and k != "relock", label=lab)
    pa, pb = s.get("probe_a", "off"), s.get("probe_b", "off")
    probes = {p["id"]: ("A" if p["signal"] and p["signal"] == pa else "B" if p["signal"] and p["signal"] == pb
                        else "") for p in PROBES}
    return dict(boxes=B, wires=wires, sw=swd, probes=probes, mode=mode)
