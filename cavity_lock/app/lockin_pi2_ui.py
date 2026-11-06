"""
lockin_pi2_ui.py -- browser UI of lockin_pi2_app.py (v2): the layout of lockin_pi_ui.py (view icons "Lock" /
"Intensity" with status dots, one schematic per view, every parameter inline) with the earlier automatic scopes:
the Lock view shows IN1, X / Y / R and OUT1 (triggered on the ramp while ramping, `ramp_periods` periods) plus probes
A / B; the Intensity view shows the servo input and OUT2. Their settings are in visible bars under each view.
"""
import json
import math
import time

import numpy as np

import lockin_pi_schematic as LS
import ui as U

SHORT = {"ac_hz": "Hz", "lpf_hz": "Hz", "lpf_order": "", "lia_gain": "×", "phase_deg": "deg", "dither_hz": "Hz",
         "dither_mvpp": "mVpp", "filt1_hz": "1", "filt2_hz": "2", "filt3_hz": "3", "filt4_hz": "4", "pi_input": "",
         "p": "P", "i": "I", "setpoint_mv": "sp mV", "clamp_lo_mv": "min mV", "clamp_hi_mv": "max mV",
         "offset_mv": "offset mV", "ramp_hz": "Hz", "ramp_mvpp": "mVpp", "ramp_shape": "",
         "int_input": "", "int_filt1_hz": "1", "int_filt2_hz": "2", "int_filt3_hz": "3", "int_filt4_hz": "4",
         "int_p": "P", "int_i": "I", "int_setpoint_mv": "sp mV", "int_clamp_lo_mv": "min mV",
         "int_clamp_hi_mv": "max mV"}

EXTRA_CSS = """
.scopepanel { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; font-size: 13px;
              border: 1px solid #ccc; border-radius: 6px; padding: 6px 8px; margin: 6px 0 4px; background: #fafafa; }
.scopepanel .sc { display: flex; align-items: center; gap: 4px; }
.scopepanel .sc > span { color: #444; }
.scopepanel .dash-input-container { width: 92px; }
.runbtn { padding: 3px 12px; font-weight: bold; cursor: pointer; border-radius: 12px; border: 1px solid #999; }
.runbtn.on { background: #1e8e3e; color: white; border-color: #1e8e3e; }
"""

TYPED_JS = """
function(ns, nb) {
  var el = document.getElementById('s-offset_mv');
  if (!el) { return window.dash_clientside.no_update; }
  var v = parseFloat(el.value);
  return isNaN(v) ? window.dash_clientside.no_update : {v: v, t: Date.now()};
}
"""

ROWS_JS = """
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

LOCK_DOT = {"pi": "ok", "ramp": "busy", "dc": "held", "off": "off", "connecting": "busy"}


def fmt_dur(d):
    return f"{d*1e6:.0f} µs" if d < 1e-3 else f"{d*1e3:.3g} ms" if d < 1 else f"{d:.3g} s"


def build_app(ctl, A):
    """A: the lockin_pi_app module (FIELDS, KEYS, PA)."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update, ALL

    app = Dash(__name__, title="Lock-in PI + intensity PI (v2)", update_title=None)
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + U.CSS % {} + EXTRA_CSS + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS
    WIDGET = {p["key"]: p["widget"] for b in LS.BLOCKS for p in b["params"] if p.get("widget")}
    NULLABLE = set()

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "select":
            return val
        if val is None or val == "":
            return None if key in NULLABLE else ctl.s.get(key, default)
        return int(val) if isinstance(default, int) and not isinstance(default, bool) else float(val)

    def control(key, short=None):
        _, label, default, _, kind, opt = FIELD[key]
        val = ctl.s.get(key, default)
        cid = f"s-{key}"
        if kind == "select" and (WIDGET.get(key) == "radio" or len(opt["options"]) <= 2):
            comp = dcc.RadioItems(id=cid, options=[{"label": f" {o}", "value": o} for o in opt["options"]],
                                  value=val, inline=True, inputStyle={"marginLeft": "4px"})
        elif kind == "num":
            comp = dcc.Input(id=cid, type="number", value=val, debounce=True,
                             step=1 if isinstance(default, int) and not isinstance(default, bool) else "any", **opt)
        else:
            labels = {}
            comp = dcc.Dropdown(id=cid, options=[{"label": labels.get(o, str(o)), "value": o} for o in opt["options"]],
                                value=val, clearable=False, searchable=False,
                                style={"width": "105px", "fontSize": "12px"})
        if short is not None:
            return html.Span([html.Span(short), comp] if short else [comp], className="ic", title=label)
        return html.Span([html.Span(label), comp], className="sc", title=label)

    def popover(pid, title, body, style):
        return html.Div([html.Div([html.Span(title), html.Span("×", id={"type": "popx", "b": pid}, className="px")],
                                  className="ptitle")] + body,
                        id={"type": "pop", "b": pid}, className="pop", style=dict(style, display="none"))

    def block(b, M):
        P = M.PREFIX
        pos = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
        hdr_id = {"type": "hdr", "b": b["id"]}
        if b["kind"] == "node":
            return html.Div([html.Span(b["label"]), html.Div(id=f"{P}rb-{b['id']}", className="rb")], id=hdr_id,
                            className="blk node" + (" term" if b["id"] in ("out1", "out2") else ""), style=pos)
        kids = [html.Div(b["label"] + "  ⓘ", id=hdr_id, className="hdr", title="click for the description"),
                html.Div(id=f"{P}rb-{b['id']}", className="rb")]
        knobs = [control(p["key"], SHORT.get(p["key"], "")) for p in b["params"]]
        if knobs:
            kids.append(html.Div(knobs, className="inl"))
        if b["id"] == "ramp":
            kids.append(html.Div([html.Button(lab, id={"type": "onudge", "d": d}, title=f"offset {d:+g} mV")
                                  for lab, d in (("−10", -10.0), ("−1", -1.0), ("+1", 1.0), ("+10", 10.0))],
                                 className="spadj"))
        if b["id"] in ("pid", "int_pid"):
            kids.append(html.Button("Reset integrator", id="btn-reset" if b["id"] == "pid" else "btn-ireset",
                                    style={"fontSize": "11px", "padding": "1px 6px", "cursor": "pointer"}))
        return html.Div(kids, className=f"blk {b['kind']} b-{b['id']}", style=pos)

    def block_pop(b, M):
        body = []
        regs = sorted({p["attr"] for p in b["params"] if p["attr"]})
        if regs:
            body.append(html.Div("Writes: " + ", ".join(regs), className="note"))
        body.append(html.Div(b["note"], className="note"))
        left = max(4, min(b["x"], M.CANVAS_W - 320))
        st = {"left": left, "top": b["y"] + b["h"] + 4} if b["y"] + b["h"] + 160 < M.CANVAS_H else \
            {"left": left, "bottom": M.CANVAS_H - b["y"] + 4}
        return popover(b["id"], b["label"], body, st)

    def switch(sw, M):
        P = M.PREFIX
        return html.Div([html.Div(sw["label"], id=f"{P}swl-{sw['id']}", className="lab"),
                         html.Div(id=f"{P}swp-{sw['id']}", className="pill")],
                        id={"type": "sw", "s": sw["id"]}, className="sw", style={"left": sw["x"], "top": sw["y"]},
                        title=LS.SWITCH_TITLES[sw["id"]])

    def probe(p, M):
        P = M.PREFIX
        pos = {"left": p["x"], "top": p["y"]}
        if p["signal"]:
            return html.Div(html.Span(id=f"{P}pb-{p['id']}", className="pdot"),
                            id={"type": "probe", "p": p["signal"]}, className="probe", style=pos,
                            title=f"probe {p['label']} (click: put it on scope CH1 / CH2)")
        return html.Div(html.Span(id=f"{P}pb-{p['id']}", className="pdot grey", **{"data-grey": "1"}),
                        className="probe grey", style=pos, title="not probe-able: " + p["why"])

    def canvas(M, frame_label, frame_h):
        P = M.PREFIX
        kids = [html.Div(id=f"{P}wires", className="wires"),
                html.Div(html.Span(frame_label), className="plantframe",
                         style={"left": 10, "top": 4, "width": 1300, "height": frame_h})]
        kids += [html.Div(t["text"], className="term", style={"left": t["x"], "top": t["y"]}) for t in M.TERMINALS]
        kids += [block(b, M) for b in M.BLOCKS] + [switch(s, M) for s in M.SWITCHES] + [probe(p, M) for p in M.PROBES]
        kids += [block_pop(b, M) for b in M.BLOCKS]
        return html.Div(html.Div(kids, id=f"{P}canvas", className="canvas",
                                 style={"width": M.CANVAS_W, "height": M.CANVAS_H}), className="canvas-wrap")

    def icon(vid, label, svg, dot_id):
        return html.Div([html.Div(id=dot_id, className="vdot"),
                         html.Div(className="vsvg", children=dcc.Markdown(svg, dangerously_allow_html=True,
                                                                         style={"lineHeight": 0})),
                         html.Span(label)], id=vid, className="vicon", n_clicks=0,
                        title=f"show the {label} view (the hardware is not touched)")

    def lock_scope():
        return html.Div([
            html.Div([html.B("Lock scope"),
                      html.Span("Probe A", style={"color": U.COLORS["A"], "fontWeight": "bold"}), control("probe_a", ""),
                      html.Span("Probe B", style={"color": U.COLORS["B"], "fontWeight": "bold"}), control("probe_b", ""),
                      control("ramp_periods"), control("live_ms"), control("xaxis"), control("probe_every"),
                      control("ui_hz")], className="scopepanel"),
            html.Div(id="stats", style={"fontSize": "13px", "margin": "2px 0"}),
            dcc.Graph(id="scope", config={"displaylogo": False})])

    def int_scope():
        return html.Div([
            html.Div([html.B("Intensity scope"), control("int_live_ms")], className="scopepanel"),
            html.Div(id="istats", style={"fontSize": "13px", "margin": "2px 0"}),
            dcc.Graph(id="iscope", config={"displaylogo": False})])

    def serve_layout():
        return html.Div([
            html.Div([
                html.Div([icon("vbtn-cav", "Lock", U.CAV_ICON, "dot-cav"),
                          icon("vbtn-int", "Intensity", U.INT_ICON, "dot-int")], className="views"),
                html.Div(id="status", style={"fontSize": "16px", "fontWeight": "bold", "flex": "1"}),
                html.Button("Save settings", id="btn-save", className="btn"),
                html.Button("Load settings", id="btn-load", className="btn"),
            ], className="bar", style={"marginBottom": "4px"}),
            html.Div([canvas(LS.LOCK, "external (display only)", 76), lock_scope()], id="view-cavity"),
            html.Div([canvas(LS.INT, "intensity servo: IN2 (or IN1) → pid2 → OUT2", 250), int_scope()],
                     id="view-intensity", style={"display": "none"}),
            html.Pre(id="log", style={"fontSize": "12px", "height": "22vh", "overflowY": "auto", "whiteSpace": "pre-wrap",
                                      "background": "#f6f6f6", "padding": "6px", "margin": "6px 0 0"}),
            dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
            dcc.Store(id="view", storage_type="local", data="cavity"),
            dcc.Store(id="synced", data=-1), dcc.Store(id="push-version"), dcc.Store(id="frame"),
            dcc.Store(id="frame-seq", data=-1), dcc.Store(id="iframe"), dcc.Store(id="iframe-seq", data=-1),
            dcc.Store(id="diag"), dcc.Store(id="diag-key", data=""), dcc.Store(id="diag-ack"),
            dcc.Store(id="geom", data=LS.geometry(LS.LOCK)),
            dcc.Store(id="idiag"), dcc.Store(id="idiag-key", data=""), dcc.Store(id="idiag-ack"),
            dcc.Store(id="igeom", data=LS.geometry(LS.INT)),
            dcc.Store(id="typed"), dcc.Store(id="ack"),
        ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    app.layout = serve_layout

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(f"s-{k}", "value") for k in KEYS],
                  State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-reset": "reset_i", "btn-ireset": "int_reset_i", "btn-save": "save", "btn-load": "load"}

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(b, "n_clicks") for b in btn_cmd],
                  prevent_initial_call=True)
    def on_button(*_):
        if ctx.triggered_id in btn_cmd:
            ctl.submit(btn_cmd[ctx.triggered_id])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "sw", "s": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_switch(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("switch", (t["s"], not ctl.switches()[t["s"]]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "probe", "p": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_probe(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("probe", t["p"])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "onudge", "d": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_nudge(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("offset_nudge", float(t["d"]))
        return time.time()

    app.clientside_callback(TYPED_JS, Output("typed", "data"), Input("s-offset_mv", "n_submit"),
                            Input("s-offset_mv", "n_blur"), prevent_initial_call=True)

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("typed", "data"), prevent_initial_call=True)
    def on_typed(d):
        if d and abs(float(d["v"]) - ctl.s["offset_mv"]) > 1e-9:
            ctl.submit("offset", float(d["v"]))
        return time.time()

    app.clientside_callback(U.VIEW_CLICK_JS, Output("view", "data"), Input("vbtn-cav", "n_clicks"),
                            Input("vbtn-int", "n_clicks"), prevent_initial_call=True)
    app.clientside_callback(U.VIEW_SHOW_JS, [Output("view-cavity", "style"), Output("view-intensity", "style"),
                                             Output("vbtn-cav", "className"), Output("vbtn-int", "className")],
                            Input("view", "data"))
    app.clientside_callback(U.POP_JS, Output({"type": "pop", "b": ALL}, "style"),
                            Input({"type": "hdr", "b": ALL}, "n_clicks"), Input({"type": "popx", "b": ALL}, "n_clicks"),
                            State({"type": "pop", "b": ALL}, "style"), prevent_initial_call=True)
    app.clientside_callback(ROWS_JS, Output("scope", "figure"), Input("frame", "data"))
    app.clientside_callback(ROWS_JS, Output("iscope", "figure"), Input("iframe", "data"))
    app.clientside_callback(U.DIAG_JS, Output("diag-ack", "data"), Input("diag", "data"), State("geom", "data"))
    app.clientside_callback(U.DIAG_JS, Output("idiag-ack", "data"), Input("idiag", "data"), State("igeom", "data"))

    def r4(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 4).tolist()

    def env(t_ms, y):
        return A.PA.envelope(t_ms, y * 1e3)

    def lock_frame(snap):
        tr, s, pr = snap["trace"], snap["settings"], snap.get("probe")
        if tr is None:
            return None
        R = np.hypot(tr["x"], tr["y"])
        sp = s["setpoint_mv"]
        xy = tr["kind"] == "ramp" and s["xaxis"] == "OUT1"
        if xy:
            k = max(1, len(tr["t"]) // 4000)
            xs = r4(tr["out1"][::k], 1e3)

            def T(name, y, color, dash=None):
                return dict(name=name, x=xs, y=r4(y[::k], 1e3), color=color, dash=dash)
            rows = [dict(title="IN1 photodiode", unit="mV", traces=[T("IN1", tr["pd"], "#222222")]),
                    dict(title="Lock-in X, Y, R (dotted: setpoint)", unit="mV", hline=sp,
                         traces=[T("X", tr["x"], "#1f5fa8"), T("Y", tr["y"], "#c0392b"), T("R", R, "#999999", "dot")])]
            fr = dict(xtitle=f"OUT1 [mV] ({len(tr['t'])} points, all ramp periods overlaid)", uirev="xy", rows=rows)
        else:
            t_ms = tr["t"] * 1e3

            def E(name, y, color, dash=None):
                xx, yy = env(t_ms, y)
                return dict(name=name, x=r4(xx), y=r4(yy), color=color, dash=dash)
            rows = [dict(title="IN1 photodiode", unit="mV", traces=[E("IN1", tr["pd"], "#222222")]),
                    dict(title="Lock-in X, Y, R (dotted: setpoint)", unit="mV", hline=sp,
                         traces=[E("X", tr["x"], "#1f5fa8"), E("Y", tr["y"], "#c0392b"), E("R", R, "#999999", "dot")]),
                    dict(title="OUT1", unit="mV", traces=[E("OUT1", tr["out1"], "#d35400")])]
            n_per = (tr["t"][-1] * s["ramp_hz"]) if tr["kind"] == "ramp" else 0
            fr = dict(xtitle="time [ms]" + (f"  ({n_per:.1f} ramp periods, triggered on the ramp)"
                                            if tr["kind"] == "ramp" else "  (live)"), uirev=tr["kind"], rows=rows)
        if pr is not None and not xy:
            tt = pr["t"] * 1e3
            tr_ = []
            for ch, col in (("a", U.COLORS["A"]), ("b", U.COLORS["B"])):
                if pr.get(ch) is not None:
                    xx, yy = env(tt, pr[ch])
                    tr_.append(dict(name=f"{ch.upper()}: {pr[ch + '_name']}", x=r4(xx), y=r4(yy), color=col))
            if tr_:
                fr["rows"].append(dict(title="Probes A / B", unit="mV", traces=tr_))
        return fr

    def int_frame(snap):
        it, s = snap["itrace"], snap["settings"]
        if it is None:
            return None
        t_ms = it["t"] * 1e3

        def E(name, y, color):
            xx, yy = env(t_ms, y)
            return dict(name=name, x=r4(xx), y=r4(yy), color=color)
        return dict(xtitle="time [ms]  (live)", uirev="int", rows=[
            dict(title=f"Intensity: {s['int_input'].upper()} (dotted: setpoint)", unit="mV",
                 hline=s["int_setpoint_mv"], traces=[E(s["int_input"].upper(), it["inp"], "#1f5fa8")]),
            dict(title="OUT2", unit="mV", traces=[E("OUT2", it["out2"], "#d35400")])])

    def tstats(y, name):
        return f"{name} mean {np.mean(y)*1e3:+.2f} mV, rms {np.std(y)*1e3:.3f} mV"

    @app.callback([Output("frame", "data"), Output("frame-seq", "data"), Output("iframe", "data"),
                   Output("iframe-seq", "data"), Output("status", "children"), Output("stats", "children"),
                   Output("istats", "children"), Output("dot-cav", "className"), Output("dot-int", "className"),
                   Output("log", "children"), Output("diag", "data"), Output("diag-key", "data"), Output("idiag", "data"),
                   Output("idiag-key", "data"), Output("tick", "interval"), Output("push-version", "data")],
                  Input("tick", "n_intervals"), State("view", "data"), State("synced", "data"),
                  State("frame-seq", "data"), State("iframe-seq", "data"), State("diag-key", "data"),
                  State("idiag-key", "data"), State("push-version", "data"))
    def on_tick(_, view, synced, frame_seq, iframe_seq, dkey, ikey, pushed):
        ctl.view = "intensity" if view == "intensity" else "cavity"
        snap = ctl.snapshot()
        s, mode, tr, it = snap["settings"], snap["mode"], snap["trace"], snap["itrace"]
        version_new = snap["version"] != synced
        color = {"ramp": "#2471a3", "dc": "#7f8c8d", "pi": "#1e8e3e", "off": "#c0392b"}.get(mode, "#555")
        label = {"ramp": "LOCK: RAMP", "dc": "LOCK: OFFSET ONLY (PI off)", "pi": "LOCK: PI ON",
                 "off": "LOCK: OUTPUT OFF"}.get(mode, mode.upper())
        ion = snap["int_on"]
        status = html.Span([html.Span(label, style={"color": color}), html.Span("   ·   "),
                            html.Span("INTENSITY: SERVO ON" if ion else "INTENSITY: SERVO OFF (OUT2 = 0 V)",
                                      style={"color": "#1e8e3e" if ion else "#7f8c8d"}),
                            html.Span(f"   {snap['fps']:.1f} traces/s", style={"fontSize": "13px"})])
        stats = (tstats(tr["x"], "X") + "  ·  " + tstats(tr["y"], "Y") + f"  ·  OUT1 mean "
                 f"{np.mean(tr['out1'])*1e3:+.2f} mV") if tr is not None else ""
        istats = (tstats(it["inp"], s["int_input"].upper()) + f"  ·  OUT2 mean {np.mean(it['out2'])*1e3:+.2f} mV") \
            if it is not None else ""
        lock_vis, int_vis = ctl.view != "intensity", ctl.view == "intensity"
        fr = lock_frame(snap) if lock_vis and (snap["seq"] != frame_seq or version_new) else no_update
        ifr = int_frame(snap) if int_vis and (snap["iseq"] != iframe_seq or version_new) else no_update
        dl, di = LS.diagram_state(snap, ctl.switches(), ("probe_a", "probe_b"), ("A", "B"))
        kl, ki = str(hash(json.dumps(dl, sort_keys=True))), str(hash(json.dumps(di, sort_keys=True)))
        return [fr, snap["seq"] if lock_vis else no_update, ifr, snap["iseq"] if int_vis else no_update,
                status, stats, istats, "vdot " + LOCK_DOT.get(mode, "off"), "vdot " + ("ok" if ion else "off"),
                "\n".join(reversed(snap["logs"][-80:])),
                dl if kl != dkey else no_update, kl if kl != dkey else no_update,
                di if ki != ikey else no_update, ki if ki != ikey else no_update,
                int(1000 / max(float(s["ui_hz"]), 1)) if version_new else no_update,
                snap["version"] if version_new and pushed != snap["version"] else no_update]

    @app.callback([Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
                  Input("push-version", "data"), prevent_initial_call=True)
    def on_push(v):
        s = dict(ctl.s)
        return [s[k] for k in KEYS] + [v]

    return app
