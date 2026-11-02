"""
intensity_schematic.py -- the AOM intensity servo (IN2 -> pid2 -> OUT2) as data: one model, two consumers,
like schematic.py for the cavity lock.

  * Hardware: block writers set exactly the registers drawn on their block. Only pid2 and asg1 are written here
    (the cavity owns iq0, asg0, pid0 and OUT1; see CLAUDE.md "FPGA module allocation").
  * Drawing: ui.py builds the boxes, switches and probes from BLOCKS / SWITCHES / PROBES (element ids prefixed
    with PREFIX) and the browser draws WIRES; diagram_state() gives the readback text, wires, switches, probes.

Writers get the Intensity object `it` (intensity.py): its settings (it.s), PyRPL handle (it.r), mode and helpers.
"""
import math

import schematic as SCH

P = SCH.P
PREFIX = "i-"
CANVAS_W, CANVAS_H = 1340, 470
I_STEP = SCH.I_STEP                                   # pid.i resolution [Hz]
I_MAX = (2 ** 23 - 1) * I_STEP                        # largest pid.i [Hz] (24-bit signed) ~ 38.9 kHz
P_STEP = 2.0 ** -12
P_MAX = (2 ** 23 - 1) * P_STEP                        # ~ 2048
RP_DELAY = 0.21e-6                                    # Red Pitaya analog-in to analog-out


def _ladder(lo_exp, hi_exp):
    return [round(m * 10.0 ** e, 10) for e in range(lo_exp, hi_exp + 1) for m in (1, 2, 5)]


P_LADDER = [0.0] + _ladder(-4, 2) + [1000.0, 2000.0]
I_LADDER = [0.0] + _ladder(0, 3) + [10000.0, 20000.0, 38000.0]

# ---------------------------------------------------------------------------------------------
# Blocks. kind: dsp (FPGA), node, plant (external, display only)
# ---------------------------------------------------------------------------------------------
BLOCKS = [
    # plant (top, drawn right to left): 80 MHz source -> mixer -> RF amp -> AOM -> pickoff photodiode;
    # OUT2 -> bias driver -> mixer IF
    dict(id="i_pd", label="Pickoff photodiode", kind="plant", x=20, y=12, w=180, h=62,
         params=[P("int_mw_per_v", None, inline=False), P("int_pd_bw_mhz", None, inline=False)], opts=[],
         note="Amplified Si photodiode on the AOM's 1st-order beam -> IN2 (LV jumper, about 0.5-0.8 V at the "
              "operating point). mW per V converts the readings to optical power (0 = volts only). The bandwidth "
              "enters the loop model only."),
    dict(id="i_aom", label="AOM (1st order)", kind="plant", x=290, y=12, w=180, h=62,
         params=[P("int_aom_max_dbm", None, inline=False), P("int_aom_delay_us", None, inline=False)], opts=[],
         note="Diffraction efficiency rises with RF power, peaks, then falls: past the peak the plant gain K "
              "changes sign and the servo would latch at full drive, so the limiter stays below the peak. The "
              "acoustic transit (about 0.24 us per mm in TeO2) is a pure delay in the loop model. RF level shown "
              "is estimated from the drive; warn above the rating."),
    dict(id="i_rfamp", label="RF amp ZHL-1-2W+", kind="plant", x=545, y=12, w=180, h=62,
         params=[P("int_amp_gain_db", None, inline=False)], opts=[],
         note="32 dB, +24 V supply, needs a heat sink, never run it without a load. About +31 dBm out at most."),
    dict(id="i_mixer", label="Mixer ZX05-1+ (attenuator)", kind="plant", x=800, y=12, w=200, h=62,
         params=[P("int_if_full_ma", None, inline=False)], opts=[],
         note="Used as a current-controlled attenuator: the IF current sets the RF amplitude (about -1 dBm out at "
              "full IF current with +4 dBm LO). IF limit 40 mA; the driver limits the current to about 25 mA."),
    dict(id="i_src", label="80 MHz source", kind="plant", x=1080, y=12, w=180, h=62,
         params=[P("int_rf_src_dbm", None, inline=False)], opts=[],
         note="External 80 MHz, 1 Vpp (+4 dBm into 50 ohm) into the mixer's LO port."),
    dict(id="i_drv", label="Bias driver ×(−10) + |V_bias|", kind="plant", x=800, y=100, w=260, h=58,
         params=[P("int_bias_v", None, inline=False)], opts=[],
         note="Inverting op-amp summer: R1 = 1 k from OUT2, R2 = 10 k from the bias pot (0..-5 V), Rf = 10 k, so "
              "V_drv = |V_bias| - 10 V_OUT2 -> Schottky -> 100 ohm -> mixer IF. More OUT2 = less light (K < 0); "
              "OUT2 = 0 V leaves the AOM at the bias level. |V_bias| here is display only (set by hand)."),
    # signal chain
    dict(id="i_filter", label="Input filter (pid2)", kind="dsp", x=20, y=200, w=175, h=112,
         params=[P("int_filter_hz", "pid2.inputfilter"), P("int_filter_stages", "pid2.inputfilter")], opts=[],
         note="pid2.inputfilter: up to 4 first-order stages on a factor-2 ladder; positive corner = low-pass, "
              "negative = high-pass, 0 = off. Any filter adds phase lag inside a 100 kHz loop: usually off."),
    dict(id="i_setpoint", label="Setpoint adjuster", kind="dsp", x=230, y=200, w=250, h=112,
         params=[P("int_setpoint_mv", "pid2.setpoint")], opts=[],
         note="pid2.setpoint in photodiode volts. The error is IN2 - setpoint. Slider (0 .. the most light the "
              "power scan allows), +/- steps or type a value; written live, also while locked (the gains are "
              "re-derived from the scan's K at the new setpoint)."),
    dict(id="i_pid", label="PI (pid2)", kind="dsp", x=530, y=200, w=245, h=162,
         params=[P("int_gain_mode", "pid2.i", live="locked", widget="select"),
                 P("int_ugf_khz", "pid2.i", live="locked"),
                 P("int_p", "pid2.p", live="locked", widget="select"),
                 P("int_i_hz", "pid2.i", live="locked", widget="select"),
                 P("int_corner_khz", "pid2.p", live="locked", inline=False)],
         opts=["int_gain_ramp"],
         note="pid2: output = p e + 2 pi i ∫ e dt. Negative feedback needs i K < 0, |i K| = loop bandwidth. "
              "Bandwidth mode: i = -f_u/K, p = i/f_corner. Manual: P and I magnitudes from the dropdowns, sign "
              "-sign(K). i is limited to 38.9 kHz (24 bit), so the integrator bandwidth is at most 38.9 kHz x |K|; "
              "p to 2048. The D term is not in this FPGA build (0.9.9.1). Written only while locked."),
    dict(id="i_limiter", label="Limiter", kind="dsp", x=820, y=200, w=150, h=112,
         params=[P("int_clamp_auto", "pid2.min_voltage", live="locked"),
                 P("int_clamp_margin", "pid2.max_voltage", live="locked", inline=False),
                 P("int_clamp_lo_mv", "pid2.min_voltage", live="locked", inline=False),
                 P("int_clamp_hi_mv", "pid2.max_voltage", live="locked", inline=False),
                 P("int_out2_limit_mv", None, inline=False)],
         opts=["int_auto_reengage", "int_rail_s", "int_light_frac"],
         note="pid2.min_voltage / max_voltage. Auto: the bright side stops where the light is (1 - margin) x the "
              "efficiency peak (from the power scan), the dark side at the scan limit. Watchdog: output at a "
              "clamp longer than 'railed after' -> railed fault (hold); light below the threshold -> hold, and "
              "re-engage when it is back (Re-engage switch). The OUT2 limit bounds every scan and clamp."),
    dict(id="i_sum", label="Σ", kind="node", x=1090, y=233, w=40, h=40, params=[], opts=[],
         note="Every module with output_direct = out2 is summed digitally onto OUT2: pid2 and asg1."),
    dict(id="i_out2", label="OUT2", kind="node", x=1210, y=220, w=110, h=66, params=[], opts=[],
         note="OUT2 -> 50 ohm tee -> bias driver. Useful range only about +/-0.1-0.2 V (x10 driver gain)."),
    dict(id="i_scan", label="Power scan (asg1)", kind="dsp", x=230, y=372, w=330, h=88,
         params=[P("int_scan_lo_mv", "asg1.offset", live="scanning"), P("int_scan_hi_mv", "asg1.amplitude",
                                                                         live="scanning"),
                 P("int_scan_hz", "asg1.frequency", live="scanning", inline=False),
                 P("int_scan_avg", None, inline=False)],
         opts=[],
         note="asg1 triangle on OUT2 with the loop open (pid2 p = i = 0, ival = 0); the scope triggers on asg1 and "
              "plots the photodiode against OUT2. Finds the efficiency peak, the auto clamp, K = dV_IN2/dV_OUT2 at "
              "the setpoint and the most light available. Also the Bode test-sine generator while locked."),
]
BLOCK = {b["id"]: b for b in BLOCKS}

HOMES = {
    "i_scope": ["int_window_us"],
    "i_scope_bar": ["int_panels", "int_probe_a", "int_probe_b"],
    "i_measure": ["int_bode_start_hz", "int_bode_stop_hz", "int_bode_points", "int_bode_cycles",
                  "int_bode_amp_frac"],
}

OWNER = {}
for _b in BLOCKS:
    for _p in _b["params"]:
        OWNER[_p["key"]] = _b["id"]
    for _k in _b["opts"]:
        OWNER[_k] = _b["id"]
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
    out += [f"{k}: not a FIELDS key" for k in seen if k not in keys]
    return out


# ---------------------------------------------------------------------------------------------
# Wires, switches, probes (canvas pixels)
# ---------------------------------------------------------------------------------------------
WIRES = [
    dict(id="pd_in2", kind="plant", pts=[(110, 74), (110, 200)]),
    dict(id="f_sp", kind="sig", pts=[(195, 250), (230, 250)]),
    dict(id="sp_pid", kind="sig", pts=[(480, 250), (530, 250)]),
    dict(id="pid_lim", kind="drv", pts=[(775, 250), (820, 250)]),
    dict(id="lim_sum", kind="drv", pts=[(970, 253), (1090, 253)]),
    dict(id="sum_out", kind="drv", pts=[(1130, 253), (1210, 253)]),
    dict(id="out_drv", kind="plant", pts=[(1265, 220), (1265, 129), (1060, 129)]),
    dict(id="drv_mix", kind="plant", pts=[(930, 100), (930, 74)]),
    dict(id="src_mix", kind="plant", pts=[(1080, 43), (1000, 43)]),
    dict(id="mix_amp", kind="plant", pts=[(800, 43), (725, 43)]),
    dict(id="amp_aom", kind="plant", pts=[(545, 43), (470, 43)]),
    dict(id="aom_pd", kind="plant", pts=[(290, 43), (200, 43)]),
    dict(id="scan_sum", kind="drv", pts=[(560, 416), (1110, 416), (1110, 273)]),
]

SWITCHES = [
    dict(id="lock", label="Lock", x=505, y=250),
    dict(id="output", label="Output", x=1035, y=253),
    dict(id="scan", label="Scan", x=900, y=416),
    dict(id="reengage", label="Re-engage", x=895, y=350),
]
SWITCH_TITLES = {
    "scan": "Scan: asg1 power scan on OUT2 with the loop open (on while locked asks to unlock first)",
    "lock": "Lock: asg1 off, set the clamp, park at the scan's OUT2 for the setpoint, ramp the gains up; "
            "off = hold the present drive",
    "output": "Output: pid2.output_direct (out2 / off). Off: OUT2 = 0 V = the bias level",
    "reengage": "Re-engage: after a 'no light' hold, lock again when the light is back",
}

PROBES = [
    dict(id="in2", signal="in2", x=110, y=178, label="in2: pickoff photodiode (IN2)"),
    dict(id="err", signal=None, x=212, y=250, why="after the input filter / error: inside pid2, not a scope "
                                                  "signal; probe in2 and subtract the setpoint"),
    dict(id="pid2", signal="pid2", x=990, y=253, label="pid2: servo output (after the limiter)"),
    dict(id="asg1", signal="asg1", x=610, y=416, label="asg1: power scan / Bode test sine"),
    dict(id="out2", signal="out2", x=1170, y=253, label="out2: total OUT2 (pid2 + asg1)"),
]
PROBE_SIGNALS = [p["signal"] for p in PROBES if p["signal"]]

TERMINALS = [dict(text="IN2", x=118, y=150), dict(text="OUT2 → bias driver", x=1150, y=170)]


def geometry():
    return dict(W=CANVAS_W, H=CANVAS_H, prefix=PREFIX,
                wires=[dict(id=w["id"], kind=w["kind"], pts=w["pts"]) for w in WIRES])


# ---------------------------------------------------------------------------------------------
# Block writers (pid2 and asg1 only)
# ---------------------------------------------------------------------------------------------
def w_filter(it):
    hz, n = float(it.s["int_filter_hz"]), int(it.s["int_filter_stages"])
    it.r.pid2.inputfilter = [hz] * n if hz else [0, 0, 0, 0]


def w_setpoint(it):
    it.r.pid2.setpoint = it.s["int_setpoint_mv"] * 1e-3


def w_pid(it):
    if it.mode == "locked" and it.lock_info.get("K"):
        it.set_gains(1.0)


def w_limiter(it):
    if it.mode in ("locked", "measuring") and it.lock_info.get("K"):
        it.apply_clamp()


def w_scan(it):
    if it.mode == "scanning":
        it.scan_on(quiet=True)


WRITERS = dict(i_filter=w_filter, i_setpoint=w_setpoint, i_pid=w_pid, i_limiter=w_limiter, i_scan=w_scan)
STATIC = ["i_filter", "i_setpoint"]
LIVE = ["i_scan", "i_limiter", "i_pid"]


def write_static(it):
    for b in STATIC:
        WRITERS[b](it)


def apply_changed(it, changed):
    owners = {OWNER.get(k) for k in changed}
    if "int_out2_limit_mv" in changed:
        owners |= {"i_scan"}
    done = []
    for b in STATIC + LIVE:
        if b in owners:
            WRITERS[b](it)
            done.append(b)
    return done


READBACK = {
    "pid2": ["input", "setpoint", "p", "i", "min_voltage", "max_voltage", "output_direct", "inputfilter"],
    "asg1": ["waveform", "frequency", "amplitude", "offset", "output_direct"],
}


def read_hw(r):
    return SCH.read_hw(r, READBACK)


# ---------------------------------------------------------------------------------------------
# Switch states (derived) and the diagram state
# ---------------------------------------------------------------------------------------------
BUSY = ("locking", "measuring", "busy")


def scan_live(hw):
    return hw.get("asg1.output_direct") in ("out2", "both") and (hw.get("asg1.amplitude") or 0) > 0 \
        and hw.get("asg1.waveform") != "sin"


def switch_states(mode, hw, s):
    return dict(scan=scan_live(hw), lock=mode in ("locking", "locked", "measuring"),
                output=hw.get("pid2.output_direct") in ("out2", "both"), reengage=bool(s.get("int_auto_reengage")))


def rf_levels(s, u):
    """Estimated IF current, mixer RF out and AOM RF power for OUT2 = u (display only)."""
    vd = s["int_bias_v"] - 10.0 * u
    i_ma = min(max((vd - 0.3) / 100.0 * 1e3, 0.0), s["int_if_full_ma"])
    if i_ma <= 1e-3:
        return vd, 0.0, None, None
    p_mix = -1.0 + 20 * math.log10(i_ma / s["int_if_full_ma"])
    return vd, i_ma, p_mix, p_mix + s["int_amp_gain_db"]


def _mw(s, v):
    return f" = {v * s['int_mw_per_v']:.3g} mW" if s.get("int_mw_per_v") and v is not None else ""


def diagram_state(snap):
    s, hw, mode, li, sc = snap["settings"], snap["hw"], snap["mode"], snap["lock"], snap.get("scan") or {}
    sw = switch_states(mode, hw, s)
    W = "warn"
    B = {}
    pd, drive = snap.get("pd"), snap.get("drive")
    u = (drive or 0.0) + ((hw.get("asg1.offset") or 0.0) if hw.get("asg1.output_direct") == "out2" else 0.0)
    vd, i_ma, p_mix, p_aom = rf_levels(s, u)
    B["i_pd"] = [((f"{pd:.3f} V" if pd is not None else "? V") + _mw(s, pd), ""),
                 (f"pole {s['int_pd_bw_mhz']:g} MHz", "dim")]
    B["i_aom"] = [((f"RF ≈ {p_aom:+.1f} dBm" if p_aom is not None else "RF off"), W if p_aom is not None and
                   p_aom > s["int_aom_max_dbm"] else ""),
                  (f"rating {s['int_aom_max_dbm']:g} dBm · delay {s['int_aom_delay_us']:g} µs", "dim")]
    B["i_rfamp"] = [(f"+{s['int_amp_gain_db']:g} dB" + (f" → {p_aom:+.1f} dBm" if p_aom is not None else ""), ""),
                    ("+24 V, heat sink, keep loaded", "dim")]
    B["i_mixer"] = [(f"IF {i_ma:.1f} mA (lim {s['int_if_full_ma']:g})", ""),
                    ((f"RF out {p_mix:+.1f} dBm" if p_mix is not None else "RF out: off") + " · LO ←", "dim")]
    B["i_src"] = [(f"80 MHz {s['int_rf_src_dbm']:+g} dBm", ""), ("→ mixer LO", "dim")]
    B["i_drv"] = [(f"V_drv = {s['int_bias_v']:g} − 10·({u*1e3:+.1f} mV) = {vd:.2f} V", ""),
                  ("display only; bias set by hand", "dim")]
    filt = [b for b in (hw.get("pid2.inputfilter") or []) if b]
    if filt:
        kind = "LP" if filt[0] > 0 else "HP"
        rq = snap["req"].get("int_filter_hz")
        txt = f"{kind} {abs(filt[0]):.1f} Hz × {len(filt)}"
        if rq is not None and abs(abs(rq) - abs(filt[0])) > 1e-3 * abs(rq):
            txt = f"{kind} {abs(rq):g} → {abs(filt[0]):.1f} Hz × {len(filt)}"
        B["i_filter"] = [(txt, ""), ("adds lag inside the loop", "dim")]
    else:
        B["i_filter"] = [("off (pass-through)", ""), ("4 stages max, factor-2 ladder", "dim")]
    sp = hw.get("pid2.setpoint")
    spl = [(("? mV" if sp is None else f"{sp*1e3:.1f} mV") + _mw(s, sp), "")]
    if sc.get("max_lock") is not None and sp is not None and sp > sc["max_lock"]:
        spl.append((f"above max lockable {sc['max_lock']*1e3:.0f} mV!", W))
    elif sc.get("max_lock") is not None:
        spl.append((f"max lockable {sc['max_lock']*1e3:.0f} mV (scan)", "dim"))
    B["i_setpoint"] = spl
    p_hw, i_hw, K = hw.get("pid2.p"), hw.get("pid2.i"), li.get("K") or sc.get("K")
    pidl = [(f"p {'?' if p_hw is None else f'{p_hw:+.4g}'}   i {'?' if i_hw is None else f'{i_hw:+.4g}'} Hz", "")]
    if K:
        pidl.append((f"K {K:+.2f} V/V · |i·K| {abs((i_hw or 0) * K)/1e3:.1f} kHz", ""))
        cap = I_MAX * abs(K)
        want = s["int_ugf_khz"] * 1e3
        if s["int_gain_mode"] == "bandwidth" and want > cap:
            pidl.append((f"cap {cap/1e3:.0f} kHz < {want/1e3:g} kHz!", W))
        else:
            pidl.append((f"integrator cap {cap/1e3:.0f} kHz", "dim"))
    else:
        pidl.append(("K unknown: run a power scan", "dim"))
    if mode in ("locked", "measuring") and i_hw == 0 and (p_hw or 0) == 0:
        pidl.append(("gains round to 0!", W))
    pm = snap.get("pm")
    if pm and pm[0]:
        pidl.append((f"model PM {pm[1]:.0f}° @ {pm[0]/1e3:.0f} kHz", W if pm[1] < 45 else "dim"))
    B["i_pid"] = pidl
    lo, hi = hw.get("pid2.min_voltage"), hw.get("pid2.max_voltage")
    if lo is not None and hi is not None and max(abs(lo), abs(hi)) >= 0.5:
        lim = [(f"{lo:+.3f}..{hi:+.3f} V (open)", "")]
    else:
        lim = [(f"{'?' if lo is None else f'{lo*1e3:+.1f}'}..{'?' if hi is None else f'{hi*1e3:+.1f}'} mV", "")]
    if drive is not None and hw.get("pid2.output_direct") == "out2":
        lim.append((f"out {drive*1e3:+.2f} mV", "dim"))
    if sc.get("u_peak") is not None:
        lim.append((f"peak at {sc['u_peak']*1e3:+.0f} mV", "dim"))
    elif sc:
        lim.append(("no peak in the scan range", "dim"))
    B["i_limiter"] = lim
    B["i_sum"] = []
    parts = [n for n, on in (("pid2", sw["output"]),
                             ("asg1", hw.get("asg1.output_direct") == "out2" and (hw.get("asg1.amplitude") or 0) > 0))
             if on]
    B["i_out2"] = [(f"limit ±{s['int_out2_limit_mv']:g} mV", ""),
                   ("= " + " + ".join(parts) if parts else "0 V = bias level", "dim")]
    a1 = hw.get("asg1.amplitude") or 0
    if sw["scan"]:
        o = hw.get("asg1.offset") or 0
        B["i_scan"] = [(f"triangle {hw.get('asg1.frequency') or 0:.3g} Hz on OUT2", ""),
                       (f"{(o-a1)*1e3:+.0f}..{(o+a1)*1e3:+.0f} mV", "dim")]
    elif a1 > 0 and hw.get("asg1.waveform") == "sin" and hw.get("asg1.output_direct") == "out2":
        B["i_scan"] = [(f"Bode test sine {hw.get('asg1.frequency') or 0:.4g} Hz, {a1*1e3:.3g} mV", "")]
    else:
        B["i_scan"] = [("off", "dim")]
    asg_on = hw.get("asg1.output_direct") in ("out2", "both") and a1 > 0
    closed = hw.get("pid2.input") == "in2" and bool(hw.get("pid2.i") or hw.get("pid2.p"))
    wires = dict(pd_in2=True, f_sp=True, sp_pid=closed, pid_lim=sw["output"], lim_sum=sw["output"],
                 sum_out=sw["output"] or asg_on, out_drv=True, drv_mix=True, src_mix=True, mix_amp=True,
                 amp_aom=True, aom_pd=True, scan_sum=asg_on)
    busy = mode in BUSY
    swd = {}
    for k, on in sw.items():
        lab = next(x["label"] for x in SWITCHES if x["id"] == k)
        if k == "lock" and mode == "locking":
            lab = "Lock: locking…"
        swd[k] = dict(on=bool(on), busy=busy and k != "reengage", label=lab)
    pa, pb = s.get("int_probe_a", "off"), s.get("int_probe_b", "off")
    probes = {p["id"]: ("A" if p["signal"] and p["signal"] == pa else "B" if p["signal"] and p["signal"] == pb
                        else "") for p in PROBES}
    return dict(boxes=B, wires=wires, sw=swd, probes=probes, mode=mode)
