"""
errsig2ph_ui.py -- browser UI of errsig2ph_app.py (2-phase lock-in spin-off): the schematic (errsig2ph_schematic.py)
and an embedded scope that shows X, Y and R. Reuses the styles and clientside drawing code of the full app (ui.py):
DIAG_JS, POP_JS and SCOPE_JS (extended here with the Y and R traces).
"""
import json
import time

import numpy as np

import cavity_lock_app as CL
import errsig2ph_schematic as ES
import ui as U

SHORT = {"ac_hz": "Hz", "lpf_hz": "Hz", "lpf_order": "n", "err_gain": "gain ×", "phase_deg": "deg",
         "dither_hz": "Hz", "dither_mvpp": "mVpp", "ramp_hz": "Hz", "ramp_mvpp": "mVpp",
         "ramp_center_mv": "setpoint mV", "in1_scale": "×", "eo_gain": "gain ×", "eo_offset_mv": "offset mV",
         "in2_gain": "gain ×", "in2_scale": "IN2 ×"}

SCOPE2_JS = U.SCOPE_JS.replace(
    "error: 'Error signal (iq0) [V]'", "error: 'Lock-in X (iq2, error), Y (iq2_2), R [V]'").replace(
    """      shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: 0, y1: 0, line: {color: 'gray', width: 1}});
      var zc""",
    """      if (fr.erry) data.push({type: 'scatter', mode: 'lines', x: fr.ty || fr.t, y: fr.erry, name: 'Y', yaxis: ya,
                              line: {color: '#c0392b', width: 1}});
      if (fr.rr) data.push({type: 'scatter', mode: 'lines', x: fr.ty || fr.t, y: fr.rr, name: 'R', yaxis: ya,
                            line: {color: '#999999', width: 1, dash: 'dot'}});
      shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: 0, y1: 0, line: {color: 'gray', width: 1}});
      var zc""")
assert SCOPE2_JS.count("fr.erry") == 2 and "Lock-in X" in SCOPE2_JS
SCOPE2_JS = SCOPE2_JS.replace("name: 'error', yaxis: ya", "name: 'X (error)', yaxis: ya")

TYPED_JS = """
function(ns, nb) {
  var el = document.getElementById('s-ramp_center_mv');
  if (!el) { return window.dash_clientside.no_update; }
  var v = parseFloat(el.value);
  return isNaN(v) ? window.dash_clientside.no_update : {v: v, t: Date.now()};
}
"""


def build_app(ctl, A):
    """A: the errsig_app module (FIELDS, KEYS)."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update, ALL

    app = Dash(__name__, title="2-phase lock-in error signal + summer", update_title=None)
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + U.CSS % {} + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS
    WIDGET = {p["key"]: p["widget"] for b in ES.BLOCKS for p in b["params"] if p.get("widget")}

    def to_ui(key, val):
        return (["on"] if val else []) if FIELD[key][4] == "check" else val

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "check":
            return bool(val)
        if kind == "select":
            return val
        if val is None:
            return ctl.s.get(key, default)
        return int(val) if isinstance(default, int) and not isinstance(default, bool) else float(val)

    def control(key, short=None):
        _, label, default, _, kind, opt = FIELD[key]
        val = to_ui(key, ctl.s.get(key, default))
        widget = WIDGET.get(key)
        cid = f"s-{key}"
        if widget == "radio":
            opts = opt["options"] if kind == "select" else list(range(int(opt["min"]), int(opt["max"]) + 1))
            comp = dcc.RadioItems(id=cid, options=[{"label": f" {o}", "value": o} for o in opts], value=val,
                                  inline=True, inputStyle={"marginLeft": "4px"})
        elif kind == "num":
            comp = dcc.Input(id=cid, type="number", value=val, debounce=True, step=1 if widget == "int" else "any",
                             **opt)
        else:
            comp = dcc.Dropdown(id=cid, options=[{"label": str(o), "value": o} for o in opt["options"]], value=val,
                                clearable=False, searchable=False,
                                style={"width": "120px" if short is None else "112px", "fontSize": "12px"})
        if short is not None:
            return html.Span([html.Span(short), comp] if short else [comp], className="ic", title=label)
        return html.Div([html.Span(label, className="l"), comp], className="row")

    def popover(pid, title, body, style):
        return html.Div([html.Div([html.Span(title), html.Span("×", id={"type": "popx", "b": pid}, className="px")],
                                  className="ptitle")] + body,
                        id={"type": "pop", "b": pid}, className="pop", style=dict(style, display="none"))

    def block(b):
        pos = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
        hdr_id = {"type": "hdr", "b": b["id"]}
        if b["kind"] == "node":
            return html.Div([html.Span(b["label"]), html.Div(id=f"rb-{b['id']}", className="rb")], id=hdr_id,
                            className="blk node" + (" term" if b["id"] in ("out1", "out2") else ""), style=pos)
        kids = [html.Div(b["label"] + "  ⚙", id=hdr_id, className="hdr", title="click for all settings"),
                html.Div(id=f"rb-{b['id']}", className="rb")]
        knobs = [control(p["key"], SHORT.get(p["key"], "")) for p in b["params"] if p["inline"]]
        if b["id"] == "phase":
            knobs.append(html.Button("Auto", id="btn-phase", title="Auto phase: rotate the error signal into X by the "
                                     "measured theta (needs Ramp and Dither on)",
                                     style={"fontSize": "11px", "padding": "1px 6px", "cursor": "pointer"}))
        if knobs:
            kids.append(html.Div(knobs, className="inl"))
        if b["id"] == "ramp":
            kids.append(html.Div([html.Button(lab, id={"type": "rnudge", "d": d}, title=f"setpoint {d:+g} mV")
                                  for lab, d in (("−10", -10.0), ("−1", -1.0), ("+1", 1.0), ("+10", 10.0))],
                                 className="spadj"))
        return html.Div(kids, className=f"blk {b['kind']} b-{b['id']}", style=pos)

    def block_pop(b):
        body = [control(p["key"]) for p in b["params"] if not p["inline"]]
        regs = sorted({p["attr"] for p in b["params"] if p["attr"]})
        if regs:
            body.append(html.Div("Writes: " + ", ".join(regs), className="note"))
        body.append(html.Div(b["note"], className="note"))
        left = max(4, min(b["x"], ES.CANVAS_W - 320))
        st = {"left": left, "bottom": ES.CANVAS_H - b["y"] + 4} if b["y"] > 300 else {"left": left,
                                                                                     "top": b["y"] + b["h"] + 4}
        return popover(b["id"], b["label"], body, st)

    def switch(sw):
        return html.Div([html.Div(sw["label"], id=f"swl-{sw['id']}", className="lab"),
                         html.Div(id=f"swp-{sw['id']}", className="pill")],
                        id={"type": "sw", "s": sw["id"]}, className="sw", style={"left": sw["x"], "top": sw["y"]},
                        title=ES.SWITCH_TITLES[sw["id"]])

    def probe(p):
        pos = {"left": p["x"], "top": p["y"]}
        if p["signal"]:
            return html.Div(html.Span(id=f"pb-{p['id']}", className="pdot"), id={"type": "probe", "p": p["signal"]},
                            className="probe", style=pos, title=f"probe {p['label']} (click: scope channel A/B)")
        return html.Div(html.Span(id=f"pb-{p['id']}", className="pdot grey", **{"data-grey": "1"}),
                        className="probe grey", style=pos, title="not probe-able: " + p["why"])

    def canvas():
        kids = [html.Div(id="wires", className="wires"),
                html.Div(html.Span("external (display only)"), className="plantframe",
                         style={"left": 10, "top": 4, "width": 1300, "height": 76})]
        kids += [html.Div(t["text"], className="term", style={"left": t["x"], "top": t["y"]}) for t in ES.TERMINALS]
        kids += [block(b) for b in ES.BLOCKS] + [switch(s) for s in ES.SWITCHES] + [probe(p) for p in ES.PROBES]
        kids += [block_pop(b) for b in ES.BLOCKS]
        return html.Div(html.Div(kids, id="canvas", className="canvas",
                                 style={"width": ES.CANVAS_W, "height": ES.CANVAS_H}), className="canvas-wrap")

    def serve_layout():
        scope_pop = popover("scope", "Scope settings", [control(k) for k in ES.HOMES["scope"]], {"left": 0, "top": 34})
        return html.Div([
            html.Div([
                html.H3("2-phase lock-in error signal (OUT1) + PZT summer (OUT2)", style={"margin": "4px 12px 4px 0"}),
                html.Div(id="status", style={"fontSize": "16px", "fontWeight": "bold", "flex": "1"}),
                html.Button("Park at peak", id="btn-park", className="btn",
                            title="DC offset on the target peak's error zero crossing, then servo mode (ramp off, "
                                  "OUT2 = IN2 correction + DC + dither): hand-off to the servo box"),
                html.Button("Use tallest peak", id="btn-tallest", className="btn"),
                html.Button("Save settings", id="btn-save", className="btn"),
                html.Button("Load settings", id="btn-load", className="btn"),
            ], className="bar"),
            canvas(),
            html.Div([
                html.B("Embedded scope"),
                html.Span("Probe A", style={"color": U.COLORS["A"], "fontWeight": "bold"}), control("probe_a", ""),
                html.Span("Probe B", style={"color": U.COLORS["B"], "fontWeight": "bold"}), control("probe_b", ""),
                html.Span("⚙ scope", id={"type": "hdr", "b": "scope"}, className="gear"), scope_pop,
                html.Span(id="target-text", style={"fontSize": "13px"}),
            ], className="bar", style={"marginBottom": "4px"}),
            dcc.Graph(id="scope", config={"displaylogo": False}),
            dcc.Graph(id="history", style={"display": "none"}),
            html.Div(id="analysis", style={"fontSize": "13px", "whiteSpace": "pre-wrap"}),
            html.Pre(id="log", style={"fontSize": "12px", "height": "24vh", "overflowY": "auto", "whiteSpace": "pre-wrap",
                                      "background": "#f6f6f6", "padding": "6px", "margin": "6px 0 0"}),
            dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
            dcc.Store(id="synced", data=-1), dcc.Store(id="push-version"), dcc.Store(id="frame"),
            dcc.Store(id="frame-seq", data=-1), dcc.Store(id="diag"), dcc.Store(id="diag-key", data=""),
            dcc.Store(id="diag-ack"), dcc.Store(id="geom", data=ES.geometry()), dcc.Store(id="typed"),
            dcc.Store(id="ack"),
        ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    app.layout = serve_layout

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(f"s-{k}", "value") for k in KEYS],
                  State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-phase": "autophase", "btn-save": "save", "btn-load": "load", "btn-tallest": "tallest",
               "btn-park": "park"}

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

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "rnudge", "d": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_nudge(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("ramp_nudge", float(t["d"]))
        return time.time()

    app.clientside_callback(TYPED_JS, Output("typed", "data"), Input("s-ramp_center_mv", "n_submit"),
                            Input("s-ramp_center_mv", "n_blur"), prevent_initial_call=True)

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("typed", "data"), prevent_initial_call=True)
    def on_typed(d):
        if d and abs(float(d["v"]) - ctl.s["ramp_center_mv"]) > 1e-9:
            ctl.submit("ramp_center", float(d["v"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("scope", "clickData"), prevent_initial_call=True)
    def on_click(cd):
        pts = (cd or {}).get("points") or []
        cross = [p for p in pts if p.get("customdata") is not None]
        if cross:
            ctl.submit("target_v", float(cross[0]["customdata"]))
        elif pts:
            ctl.submit("target", float(pts[0]["x"]))
        return time.time()

    app.clientside_callback(U.POP_JS, Output({"type": "pop", "b": ALL}, "style"),
                            Input({"type": "hdr", "b": ALL}, "n_clicks"), Input({"type": "popx", "b": ALL}, "n_clicks"),
                            State({"type": "pop", "b": ALL}, "style"), prevent_initial_call=True)
    app.clientside_callback(SCOPE2_JS, [Output("scope", "figure"), Output("history", "figure")], Input("frame", "data"))
    app.clientside_callback(U.DIAG_JS, Output("diag-ack", "data"), Input("diag", "data"), State("geom", "data"))

    def r5(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 5).tolist()

    def frame_data(snap):
        tr, dr, an = snap["traces"], snap["drive"], snap["analysis"] or {}
        rows = ["transmission", "error"] + (["drive"] if dr is not None else [])
        sweep = tr is not None and tr["kind"] == "sweep"
        fr = dict(rows=rows, uirev=f"{'sweep' if sweep else 'live'}-{len(rows)}",
                  xtitle="time [ms]" + ("  (one ramp period; click a peak or a crosshair to choose the target)"
                                        if sweep else "  (live, servo mode)"))
        if tr is not None:
            t_ms = tr["t"] * 1e3
            x, y = U_env(t_ms, tr["pd"])
            fr.update(t=r5(x), pd=r5(y))
            x, y = U_env(t_ms, tr["err"])
            fr["err"] = r5(y)
            if tr.get("y") is not None:
                tb = tr["t"][:len(tr["y"])] * 1e3
                x, y = U_env(tb, tr["y"])
                fr["ty"], fr["erry"] = r5(x), r5(y)
                x, y = U_env(tb, np.hypot(tr["xb"], tr["y"]))
                fr["rr"] = r5(y)
        if dr is not None:
            for ch in ("a", "b"):
                if dr.get(ch) is not None:
                    x, y = U_env(dr["t"] * 1e3, dr[ch] * 1e3)
                    fr["dt"], fr["p" + ch] = r5(x), r5(y)
            fr.update(pa_name=dr["a_name"], pb_name=dr["b_name"])
        if sweep and an.get("peaks"):
            fr["peaks"] = [dict(t=p["t_ms"], y=p["base"] + p["height"], label=f"{p['v']*1e3:+.0f} mV",
                                tgt=i == an.get("target")) for i, p in enumerate(an["peaks"])]
            fr["v0_t"] = an.get("v0_t_ms", [])
        if sweep and an.get("zc"):
            fr["zc"] = [dict(t=round(z["t_ms"], 4), ok=z["on_peak"], v=round(z["v"], 6)) for z in an["zc"]]
        return fr

    def U_env(x, y):
        return CL.envelope(x, y)

    @app.callback([Output("frame", "data"), Output("frame-seq", "data"), Output("status", "children"),
                   Output("target-text", "children"), Output("analysis", "children"), Output("log", "children"),
                   Output("diag", "data"), Output("diag-key", "data"), Output("tick", "interval"),
                   Output("push-version", "data")],
                  Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"),
                  State("diag-key", "data"), State("push-version", "data"))
    def on_tick(_, synced, frame_seq, diag_key, pushed):
        snap = ctl.snapshot()
        s, mode, an = snap["settings"], snap["mode"], snap["analysis"] or {}
        version_new = snap["version"] != synced
        frame = frame_data(snap) if (snap["seq"] != frame_seq or version_new) else no_update
        color = {"acquire": "#2471a3", "servo": "#1e8e3e", "busy": "#7d3c98", "off": "#c0392b"}.get(mode, "#555")
        label = {"acquire": "RAMP (alignment)", "servo": "SERVO MODE (IN2 → OUT2)",
                 "off": "OUTPUT OFF"}.get(mode, mode.upper())
        extra = f"   OUT1 error output {'ON' if snap['errout_on'] else 'off'}"
        if mode == "servo" and snap.get("in2_now") is not None:
            extra += f"   IN2 {snap['in2_now']*s['in2_scale']:+.3g} V at the box"
        xy = snap.get("xy") or {}
        if xy.get("theta") is not None:
            extra += (f"   θ {xy['theta']:+.1f}° ({xy['src']})" if xy.get("q", 0) >= 3 else
                      f"   θ: no clear axis ({xy['src']})")
        extra += f"   {snap['fps']:.1f} traces/s"
        status = html.Span([html.Span(label, style={"color": color}), html.Span(extra, style={"fontSize": "13px"})])
        if an.get("target") is not None and mode in ("acquire", "busy"):
            p = an["peaks"][an["target"]]
            th = an.get("theta")
            ttxt = (f"Target: {'chosen' if s.get('target_mv') is not None else 'tallest'} peak at {p['v']*1e3:+.1f} mV"
                    + (f", error zero crossing {an['v0']*1e3:+.2f} mV (slope {an['slope']:+.1f} /V)"
                       if an.get("v0") is not None else "")
                    + (f", θ {th:+.1f}° (axis quality {an.get('theta_q', 0):.0f})" if th is not None else ""))
        elif mode == "servo":
            ttxt = f"DC offset {s['ramp_center_mv']:+.2f} mV" + (f", error slope {snap['slope']:+.1f} /V"
                                                                 if snap.get("slope") else "")
        else:
            ttxt = ""
        lines = []
        if mode in ("acquire", "busy") and an.get("peaks"):
            for i, p in enumerate(an["peaks"]):
                lines.append(f"{p['v']*1e3:+8.1f} mV   height {p['height']:.3f} V   FWHM {p['fwhm']*1e3:.2f} mV"
                             + ("   <- target" if i == an.get("target") else ""))
            lines += an.get("notes", [])
        diag = ES.diagram_state(snap)
        key = str(hash(json.dumps(diag, sort_keys=True)))
        return [frame, snap["seq"], status, ttxt, "\n".join(lines), "\n".join(reversed(snap["logs"][-80:])),
                diag if key != diag_key else no_update, key if key != diag_key else no_update,
                int(1000 / max(float(s["ui_hz"]), 1)) if version_new else no_update,
                snap["version"] if version_new and pushed != snap["version"] else no_update]

    @app.callback([Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
                  Input("push-version", "data"), prevent_initial_call=True)
    def on_push(v):
        s = dict(ctl.s)
        return [to_ui(k, s[k]) for k in KEYS] + [v]

    return app
