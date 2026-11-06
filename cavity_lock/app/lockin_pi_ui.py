"""
lockin_pi_ui.py -- browser UI of lockin_pi_app.py: a Lock section (IN1 -> lock-in -> PI -> OUT1, ramp) and an
Intensity section (PI -> OUT2), each with its toggles, settings and scope. Scopes are drawn clientside.
"""
import time

import numpy as np

import pi_ui

CSS = pi_ui.CSS + """
.sec { border: 2px solid #bbb; border-radius: 8px; padding: 6px 10px; margin-top: 8px; }
.sec > .bar h3 { margin: 2px 12px 2px 0; }
"""

SCOPE_JS = """
function(fr) {
  var nu = window.dash_clientside.no_update;
  if (!fr) { return nu; }
  var rows = fr.rows, n = rows.length, data = [], shapes = [], ann = [];
  var lay = {template: 'plotly_white', margin: {l: 60, r: 15, t: 30, b: 45}, uirevision: fr.uirev,
             plot_bgcolor: 'white', paper_bgcolor: 'white', hovermode: 'x',
             legend: {orientation: 'h', y: 1.02, x: 1, xanchor: 'right', yanchor: 'bottom'}};
  var gap = 0.07, h = (1 - gap * (n - 1)) / n;
  rows.forEach(function(r, i) {
    var ya = i === 0 ? 'y' : 'y' + (i + 1), key = i === 0 ? 'yaxis' : 'yaxis' + (i + 1);
    var top = 1 - i * (h + gap);
    lay[key] = {domain: [top - h, top], anchor: 'x', title: {text: r.unit, font: {size: 11}}};
    ann.push({text: r.title, x: 0.5, xref: 'paper', y: top, yref: 'paper', yanchor: 'bottom', showarrow: false,
              font: {size: 13}});
    r.traces.forEach(function(t) {
      data.push({type: 'scatter', mode: 'lines', x: t.x || fr.x, y: t.y, name: t.name, yaxis: ya,
                 line: {color: t.color, width: 1, dash: t.dash || 'solid'}});
    });
    if (r.hline !== undefined && r.hline !== null) {
      shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: r.hline, y1: r.hline,
                   line: {color: 'gray', dash: 'dot', width: 1}});
    }
  });
  lay.xaxis = {title: {text: fr.xtitle}, anchor: n === 1 ? 'y' : 'y' + n};
  lay.shapes = shapes; lay.annotations = ann; lay.height = 150 * n + 90;
  return {data: data, layout: lay};
}
"""


def build_app(ctl, A):
    """A: the lockin_pi_app module."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update

    app = Dash(__name__, title="Lock-in PI + intensity PI", update_title=None)
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + CSS + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "select":
            return val
        if val is None:
            return ctl.s.get(key, default)
        return int(val) if isinstance(default, int) and not isinstance(default, bool) else float(val)

    def control(key):
        _, label, default, _, kind, opt = FIELD[key]
        val = ctl.s.get(key, default)
        if kind == "num":
            comp = dcc.Input(id=f"s-{key}", type="number", value=val, debounce=True,
                             step=1 if isinstance(default, int) else "any", **opt)
        else:
            comp = dcc.RadioItems(id=f"s-{key}", options=[{"label": f" {o}", "value": o} for o in opt["options"]],
                                  value=val, inline=True, inputStyle={"marginRight": "3px"},
                                  labelStyle={"display": "inline-block", "marginLeft": "10px"})
        return html.Div([html.Span(label), comp], className="row")

    def group(title, keys, rb_id=None, note=None):
        kids = [html.H4(title)] + [control(k) for k in keys]
        if rb_id:
            kids.append(html.Div(id=rb_id, className="rb"))
        if note:
            kids.append(html.Div(note, className="note"))
        return html.Div(kids, className="grp")

    def tg(sid, label, title):
        return html.Button(label, id=f"sw-{sid}", className="tg", title=title)

    lock_left = html.Div([
        group("Lock-in (iq2, dual phase: X and Y)", ["dither_hz", "dither_mvpp", "phase_deg", "lpf_hz", "lpf_order",
                                                     "ac_hz", "lia_gain"], "rb-iq",
              "X = in-phase ('iq2'), Y = quadrature ('iq2_2'): IN1 high-passed, times the LO and the LO + 90°, "
              "low-passed and multiplied by the gain. The phase rotates X and Y. The dither goes out on OUT1."),
        group("Lock PI (pid0 → OUT1)", ["pi_input", "p", "i_hz", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv"],
              "rb-pid", "e = (X or Y, filtered if the Filter toggle is on) − setpoint; out = p·e + ival, "
                        "d(ival)/dt = 2π·i·e, clamped. Raw values: you choose the signs."),
        group("Lock filter (Filter toggle)", A.FILT, "rb-filt",
              "> 0 low-pass corner, < 0 high-pass, 0 = stage off. Used only while the Filter toggle is on."),
        group("Ramp / offset (asg0 → OUT1)", ["offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"], "rb-asg",
              "Ramp on: OUT1 = ramp around the offset + dither, PI off. Ramp off: OUT1 = offset + dither (+ PI)."),
    ], style={"width": "400px", "flex": "none"})
    lock_sec = html.Div([
        html.Div([html.H3("Lock: IN1 → lock-in → PI → OUT1"), html.Div(id="status", style={"fontWeight": "bold",
                                                                                            "flex": "1"}),
                  tg("output", "Output", "OUT1 on / off (off: OUT1 = 0 V)"),
                  tg("ramp", "Ramp", "ramp on OUT1 for acquisition (PI off)"),
                  tg("dither", "Dither", "the lock-in dither on OUT1"),
                  tg("pi", "PI", "PI on: OUT1 = offset + PI + dither"),
                  tg("filt", "Filter", "the lock PI's input filter on / off"),
                  html.Button("Reset integrator", id="btn-reset", className="btn")], className="bar"),
        html.Div([lock_left, html.Div([html.Div(id="stats", style={"fontSize": "14px"}),
                                       dcc.Graph(id="scope", config={"displaylogo": False})],
                                      style={"flex": "1", "minWidth": "0"})],
                 style={"display": "flex", "gap": "12px", "marginTop": "6px"}),
    ], className="sec")

    int_left = html.Div([
        group("Intensity PI (pid2 → OUT2)", ["int_input", "int_p", "int_i_hz", "int_setpoint_mv", "int_clamp_lo_mv",
                                             "int_clamp_hi_mv"], "rb-ipid",
              "e = input (filtered if the Filter toggle is on) − setpoint; out = p·e + ival, d(ival)/dt = 2π·i·e, "
              "clamped. Raw values. Servo off: OUT2 = 0 V."),
        group("Intensity filter (Filter toggle)", A.IFILT, "rb-ifilt"),
    ], style={"width": "400px", "flex": "none"})
    int_sec = html.Div([
        html.Div([html.H3("Intensity: PI → OUT2"), html.Div(id="istatus", style={"fontWeight": "bold", "flex": "1"}),
                  tg("int_on", "Servo", "intensity PI on / off (off: OUT2 = 0 V)"),
                  tg("int_filt", "Filter", "the intensity PI's input filter on / off"),
                  html.Button("Reset integrator", id="btn-ireset", className="btn")], className="bar"),
        html.Div([int_left, html.Div([html.Div(id="istats", style={"fontSize": "14px"}),
                                      dcc.Graph(id="iscope", config={"displaylogo": False})],
                                     style={"flex": "1", "minWidth": "0"})],
                 style={"display": "flex", "gap": "12px", "marginTop": "6px"}),
    ], className="sec")

    app.layout = html.Div([
        html.Div([html.H2("Dual-phase lock-in PI (IN1 → OUT1) + intensity PI (OUT2)", style={"margin": "4px 12px 4px 0",
                                                                                           "flex": "1"}),
                  html.Span(id="fps", style={"fontSize": "13px"}),
                  html.Button("Save settings", id="btn-save", className="btn"),
                  html.Button("Load settings", id="btn-load", className="btn")], className="bar"),
        lock_sec, int_sec,
        html.Div([group("Scope", ["live_ms", "int_live_ms", "xaxis", "ui_hz"])],
                 style={"width": "400px", "marginTop": "8px"}),
        html.Pre(id="log", style={"fontSize": "12px", "height": "22vh", "overflowY": "auto", "whiteSpace": "pre-wrap",
                                  "background": "#f6f6f6", "padding": "6px", "margin": "6px 0 0"}),
        dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
        dcc.Store(id="synced", data=-1), dcc.Store(id="push-version"), dcc.Store(id="frame"),
        dcc.Store(id="iframe"), dcc.Store(id="frame-seq", data=-1), dcc.Store(id="ack"),
    ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(f"s-{k}", "value") for k in KEYS],
                  State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-reset": "reset_i", "btn-ireset": "int_reset_i", "btn-save": "save", "btn-load": "load"}
    SW = ["output", "ramp", "dither", "pi", "filt", "int_on", "int_filt"]

    @app.callback(Output("ack", "data", allow_duplicate=True),
                  [Input(b, "n_clicks") for b in btn_cmd] + [Input(f"sw-{n}", "n_clicks") for n in SW],
                  prevent_initial_call=True)
    def on_button(*_):
        t = ctx.triggered_id
        if t in btn_cmd:
            ctl.submit(btn_cmd[t])
        elif t and t.startswith("sw-"):
            name = t[3:]
            ctl.submit("switch", (name, not ctl.switches()[name]))
        return time.time()

    app.clientside_callback(SCOPE_JS, Output("scope", "figure"), Input("frame", "data"))
    app.clientside_callback(SCOPE_JS, Output("iscope", "figure"), Input("iframe", "data"))

    def r4(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 4).tolist()

    def lock_frame(snap):
        tr, s = snap["trace"], snap["settings"]
        if tr is None:
            return None
        R = np.hypot(tr["x"], tr["y"])
        sp = s["setpoint_mv"]
        if tr["kind"] == "ramp" and s["xaxis"] == "OUT1":
            k = max(1, len(tr["t"]) // 3000)
            x = r4(tr["out1"][::k], 1e3)

            def T(name, y, color, dash=None):
                return dict(name=name, y=r4(y[::k], 1e3), color=color, dash=dash)
            return dict(x=x, xtitle="OUT1 [mV] (one ramp period)", uirev="xy", rows=[
                dict(title="IN1 photodiode", unit="mV", traces=[T("IN1", tr["pd"], "#222222")]),
                dict(title="Lock-in X, Y, R", unit="mV", hline=sp if s["pi_input"] else None,
                     traces=[T("X", tr["x"], "#1f5fa8"), T("Y", tr["y"], "#c0392b"), T("R", R, "#999999", "dot")])])
        t_ms = tr["t"] * 1e3

        def E(name, y, color, dash=None):
            xx, yy = A.PA.envelope(t_ms, y * 1e3)
            return dict(name=name, x=r4(xx), y=r4(yy), color=color, dash=dash)
        return dict(xtitle="time [ms]" + ("  (one ramp period, triggered on the ramp)" if tr["kind"] == "ramp"
                                          else "  (live)"), uirev=tr["kind"], rows=[
            dict(title="IN1 photodiode", unit="mV", traces=[E("IN1", tr["pd"], "#222222")]),
            dict(title="Lock-in X, Y, R (dotted: setpoint)", unit="mV", hline=sp,
                 traces=[E("X", tr["x"], "#1f5fa8"), E("Y", tr["y"], "#c0392b"), E("R", R, "#999999", "dot")]),
            dict(title="OUT1", unit="mV", traces=[E("OUT1", tr["out1"], "#d35400")])])

    def int_frame(snap):
        it, s = snap["itrace"], snap["settings"]
        if it is None:
            return None
        t_ms = it["t"] * 1e3

        def E(name, y, color):
            xx, yy = A.PA.envelope(t_ms, y * 1e3)
            return dict(name=name, x=r4(xx), y=r4(yy), color=color)
        return dict(xtitle="time [ms]  (live)", uirev="int", rows=[
            dict(title=f"{s['int_input'].upper()} (dotted: setpoint)", unit="mV", hline=s["int_setpoint_mv"],
                 traces=[E(s["int_input"].upper(), it["inp"], "#1f5fa8")]),
            dict(title="OUT2", unit="mV", traces=[E("OUT2", it["out2"], "#d35400")])])

    def fmt(x, f="{:+.6g}"):
        return "?" if x is None else f.format(x)

    def pid_rb(hw, m):
        f = hw.get(f"{m}.inputfilter") or []
        on = [x for x in f if x]
        filt = "off" if not on else ", ".join((f"LP {x:.1f}" if x > 0 else f"HP {-x:.1f}") for x in on) + " Hz"
        return (f"hw: p {fmt(hw.get(f'{m}.p'))}, i {fmt(hw.get(f'{m}.i'))} Hz, setpoint "
                f"{fmt((hw.get(f'{m}.setpoint') or 0)*1e3, '{:+.2f}')} mV\n    clamp "
                f"{fmt((hw.get(f'{m}.min_voltage') or 0)*1e3, '{:+.0f}')}..{fmt((hw.get(f'{m}.max_voltage') or 0)*1e3, '{:+.0f}')}"
                f" mV, input {hw.get(f'{m}.input')}, output {hw.get(f'{m}.output_direct')}\n    filter {filt}")

    def clamp_warn(iv, hw, m):
        lo, hi = hw.get(f"{m}.min_voltage"), hw.get(f"{m}.max_voltage")
        return iv is not None and lo is not None and hi is not None and hi > lo and (
            iv <= lo + 0.01 * (hi - lo) or iv >= hi - 0.01 * (hi - lo))

    def tstats(y, name):
        return f"{name} mean {np.mean(y)*1e3:+.2f} mV, rms {np.std(y)*1e3:.3f} mV"

    OUTS = ([Output("frame", "data"), Output("iframe", "data"), Output("frame-seq", "data"), Output("status", "children"),
             Output("istatus", "children"), Output("stats", "children"), Output("istats", "children"),
             Output("fps", "children"), Output("rb-iq", "children"), Output("rb-pid", "children"),
             Output("rb-filt", "children"), Output("rb-asg", "children"), Output("rb-ipid", "children"),
             Output("rb-ifilt", "children")] + [Output(f"sw-{n}", "className") for n in SW]
            + [Output("log", "children"), Output("tick", "interval"), Output("push-version", "data")])

    @app.callback(OUTS, Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"),
                  State("push-version", "data"))
    def on_tick(_, synced, frame_seq, pushed):
        snap = ctl.snapshot()
        s, mode, hw, tr, it = snap["settings"], snap["mode"], snap["hw"], snap["trace"], snap["itrace"]
        version_new = snap["version"] != synced
        fresh = snap["seq"] != frame_seq or version_new
        color = {"ramp": "#2471a3", "dc": "#7f8c8d", "pi": "#1e8e3e", "off": "#c0392b"}.get(mode, "#555")
        label = {"ramp": "RAMP", "dc": "OFFSET ONLY (PI off)", "pi": "PI ON", "off": "OUTPUT OFF"}.get(mode,
                                                                                                      mode.upper())
        status = html.Span(label, style={"color": color})
        istatus = html.Span("SERVO ON" if snap["int_on"] else "SERVO OFF (OUT2 = 0 V)",
                            style={"color": "#1e8e3e" if snap["int_on"] else "#7f8c8d"})
        st = []
        if tr is not None:
            st.append(tstats(tr["x"], "X") + "   ·   " + tstats(tr["y"], "Y") + "   ·   "
                      + f"OUT1 mean {np.mean(tr['out1'])*1e3:+.2f} mV")
        if snap.get("ival") is not None:
            st.append(f"   ·   integrator {snap['ival']*1e3:+.2f} mV")
        th = snap.get("theta")
        if th and mode == "ramp":
            st.append(f"   ·   signal angle in X-Y {th['theta']:+.1f}° (axis quality {th['q']:.0f}; 0° = all in X)"
                      if th["q"] >= 3 else "   ·   signal angle in X-Y: no clear axis")
        warn = clamp_warn(snap.get("ival"), hw, "pid0") and mode == "pi"
        stats = html.Span(["".join(st)] + ([html.Span("   LOCK PI AT THE CLAMP", className="warn")] if warn else []))
        ist = []
        if it is not None:
            ist.append(tstats(it["inp"], s["int_input"].upper()) + f"   ·   OUT2 mean {np.mean(it['out2'])*1e3:+.2f} mV")
        if snap.get("int_ival") is not None:
            ist.append(f"   ·   integrator {snap['int_ival']*1e3:+.2f} mV")
        iwarn = clamp_warn(snap.get("int_ival"), hw, "pid2") and snap["int_on"]
        istats = html.Span(["".join(ist)] + ([html.Span("   INTENSITY PI AT THE CLAMP", className="warn")]
                                             if iwarn else []))
        bw = [b for b in (hw.get("iq2.bandwidth") or []) if b > 0]
        rb_iq = (f"hw: {fmt(hw.get('iq2.frequency'), '{:.6g}')} Hz, {(hw.get('iq2.amplitude') or 0)*2e3:.2f} mVpp, "
                 f"phase {fmt(hw.get('iq2.phase'), '{:+.1f}')}°, dither {hw.get('iq2.output_direct')}\n    LP "
                 f"{bw[0] if bw else 0:.1f} Hz ×{len(bw)}, HP {fmt(hw.get('iq2.acbandwidth'), '{:.1f}')} Hz, gain "
                 f"{fmt(hw.get('iq2.quadrature_factor'), '{:g}')}")
        a_amp, a_off = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
        if hw.get("asg0.output_direct") != "out1":
            rb_asg = "hw: asg0 off"
        elif a_amp > 0:
            wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
            rb_asg = (f"hw: {wf} {fmt(hw.get('asg0.frequency'), '{:.4g}')} Hz, "
                      f"{(a_off-a_amp)*1e3:+.0f}..{(a_off+a_amp)*1e3:+.0f} mV")
        else:
            rb_asg = f"hw: DC {a_off*1e3:+.2f} mV"
        rb_filt = "Filter toggle " + ("ON" if s["filt_on"] else "off")
        rb_ifilt = "Filter toggle " + ("ON" if s["int_filt_on"] else "off")
        sw = ctl.switches()
        cls = ["tg on" if sw[n] else "tg" for n in SW]
        return [lock_frame(snap) if fresh else no_update, int_frame(snap) if fresh else no_update, snap["seq"],
                status, istatus, stats, istats, f"{snap['fps']:.1f} frames/s", rb_iq, pid_rb(hw, "pid0"), rb_filt,
                rb_asg, pid_rb(hw, "pid2"), rb_ifilt] + cls + [
            "\n".join(reversed(snap["logs"][-80:])),
            int(1000 / max(float(s["ui_hz"]), 1)) if version_new else no_update,
            snap["version"] if version_new and pushed != snap["version"] else no_update]

    @app.callback([Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
                  Input("push-version", "data"), prevent_initial_call=True)
    def on_push(v):
        s = dict(ctl.s)
        return [s[k] for k in KEYS] + [v]

    return app
