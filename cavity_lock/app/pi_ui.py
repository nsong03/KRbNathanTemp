"""
pi_ui.py -- browser UI of pi_app.py: settings on the left (PI, input filter, ramp, scope), toggles in the top bar,
the scope (IN1 and OUT1) on the right. The scope is drawn clientside (Plotly.react, SVG scatter).
"""
import time

import numpy as np

CSS = """
body { margin: 0; }
.bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.btn { padding: 4px 10px; cursor: pointer; }
.tg { padding: 4px 12px; cursor: pointer; border-radius: 14px; border: 1px solid #999; font-weight: bold;
      background: #eee; color: #555; }
.tg.on { background: #1e8e3e; color: white; border-color: #1e8e3e; }
.grp { border: 1px solid #ccc; border-radius: 6px; padding: 6px 8px; margin-bottom: 8px; }
.grp h4 { margin: 0 0 4px; font-size: 14px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 6px; margin: 2px 0;
       font-size: 13px; }
.row > span:first-child { flex: 1; }
.row .dash-input-container { width: 170px; flex: none; }
.row input { width: 100%; }
.row label { display: inline-block; margin-left: 6px; }
.rb { font-family: monospace; font-size: 12px; color: #555; white-space: pre-wrap; margin-top: 3px; }
.note { font-size: 12px; color: #666; margin-top: 3px; }
.warn { color: #c0392b; font-weight: bold; }
"""

SCOPE_JS = """
function(fr) {
  var nu = window.dash_clientside.no_update;
  if (!fr) { return nu; }
  var data = [], lay = {template: 'plotly_white', margin: {l: 60, r: 15, t: 30, b: 45}, uirevision: fr.uirev,
                       plot_bgcolor: 'white', paper_bgcolor: 'white', hovermode: 'closest',
                       legend: {orientation: 'h', y: 1.04, x: 1, xanchor: 'right', yanchor: 'bottom'}};
  if (fr.xy) {
    data.push({type: 'scatter', mode: 'lines', x: fr.out1, y: fr.in1, name: 'IN1 vs OUT1',
               line: {color: '#1f5fa8', width: 1}});
    lay.xaxis = {title: {text: 'OUT1 [mV]'}};
    lay.yaxis = {title: {text: 'IN1 error [mV]'}, zeroline: true};
    lay.height = 520;
  } else {
    data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr.in1, name: 'IN1 (error)', yaxis: 'y',
               line: {color: '#1f5fa8', width: 1}});
    data.push({type: 'scatter', mode: 'lines', x: fr.t2, y: fr.out1, name: 'OUT1', yaxis: 'y2',
               line: {color: '#d35400', width: 1}});
    lay.yaxis = {domain: [0.53, 1], title: {text: 'IN1 [mV]'}, anchor: 'x'};
    lay.yaxis2 = {domain: [0, 0.45], title: {text: 'OUT1 [mV]'}, anchor: 'x'};
    lay.xaxis = {title: {text: fr.xtitle}, anchor: 'y2'};
    lay.shapes = [{type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: fr.sp, y1: fr.sp,
                   line: {color: 'gray', dash: 'dot', width: 1}}];
    lay.height = 560;
  }
  return {data: data, layout: lay};
}
"""


def build_app(ctl, A):
    """A: the pi_app module (FIELDS, KEYS, envelope)."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update

    app = Dash(__name__, title="PI servo (IN1 -> OUT1)", update_title=None)
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + CSS + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "select":
            return val
        if val is None:
            return ctl.s.get(key, default)
        return float(val)

    def control(key):
        _, label, default, _, kind, opt = FIELD[key]
        val = ctl.s.get(key, default)
        if kind == "num":
            comp = dcc.Input(id=f"s-{key}", type="number", value=val, debounce=True, step="any", **opt)
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

    left = html.Div([
        group("PI (pid0: IN1 → OUT1)", ["p", "i_hz", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv"], "rb-pid",
              "e = IN1 (filtered) − setpoint;  out = p·e + ival,  d(ival)/dt = 2π·i·e,  clamped to [min, max]. "
              "Choose the signs of p and i for negative feedback."),
        group("Input filter (in front of the PI)", A.FILT_KEYS, "rb-filt",
              "0 = off (default). > 0: 1st-order low-pass at that corner; < 0: high-pass. The FPGA rounds the "
              "corners to a factor-2 ladder."),
        group("Ramp / offset (asg0 → OUT1)", ["offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"], "rb-asg",
              "Ramp on: OUT1 = ramp around the offset, PI off. Ramp off: OUT1 = offset (+ PI when PI is on)."),
        group("Scope", ["live_ms", "xaxis", "ui_hz"]),
    ], style={"width": "380px", "flex": "none"})

    right = html.Div([
        html.Div(id="stats", style={"fontSize": "14px", "margin": "2px 0 4px"}),
        dcc.Graph(id="scope", config={"displaylogo": False}),
        html.Pre(id="log", style={"fontSize": "12px", "height": "22vh", "overflowY": "auto", "whiteSpace": "pre-wrap",
                                  "background": "#f6f6f6", "padding": "6px", "margin": "6px 0 0"}),
    ], style={"flex": "1", "minWidth": "0"})

    app.layout = html.Div([
        html.Div([
            html.H3("PI servo: IN1 error → OUT1", style={"margin": "4px 12px 4px 0"}),
            html.Div(id="status", style={"fontSize": "16px", "fontWeight": "bold", "flex": "1"}),
            html.Button("Output", id="sw-output", className="tg", title="OUT1 on / off (off: OUT1 = 0 V)"),
            html.Button("Ramp", id="sw-ramp", className="tg", title="Ramp on OUT1 for acquisition (PI off)"),
            html.Button("PI", id="sw-pi", className="tg", title="PI on: OUT1 = offset + PI(IN1)"),
            html.Button("Reset integrator", id="btn-reset", className="btn"),
            html.Button("Save settings", id="btn-save", className="btn"),
            html.Button("Load settings", id="btn-load", className="btn"),
        ], className="bar"),
        html.Div([left, right], style={"display": "flex", "gap": "12px", "marginTop": "6px"}),
        dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
        dcc.Store(id="synced", data=-1), dcc.Store(id="push-version"), dcc.Store(id="frame"),
        dcc.Store(id="frame-seq", data=-1), dcc.Store(id="ack"),
    ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(f"s-{k}", "value") for k in KEYS],
                  State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-reset": "reset_i", "btn-save": "save", "btn-load": "load"}
    sw_ids = {"sw-output": "output", "sw-ramp": "ramp", "sw-pi": "pi"}

    @app.callback(Output("ack", "data", allow_duplicate=True),
                  [Input(b, "n_clicks") for b in list(btn_cmd) + list(sw_ids)], prevent_initial_call=True)
    def on_button(*_):
        t = ctx.triggered_id
        if t in btn_cmd:
            ctl.submit(btn_cmd[t])
        elif t in sw_ids:
            name = sw_ids[t]
            ctl.submit("switch", (name, not ctl.switches()[name]))
        return time.time()

    app.clientside_callback(SCOPE_JS, Output("scope", "figure"), Input("frame", "data"))

    def r4(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 4).tolist()

    def frame_data(snap):
        tr, s = snap["trace"], snap["settings"]
        if tr is None:
            return None
        if tr["kind"] == "ramp" and s["xaxis"] == "OUT1":
            k = max(1, len(tr["t"]) // 3000)
            return dict(xy=True, in1=r4(tr["in1"][::k], 1e3), out1=r4(tr["out1"][::k], 1e3), uirev="xy")
        t_ms = tr["t"] * 1e3
        x, y = A.envelope(t_ms, tr["in1"] * 1e3)
        x2, y2 = A.envelope(t_ms, tr["out1"] * 1e3)
        return dict(t=r4(x), in1=r4(y), t2=r4(x2), out1=r4(y2), sp=s["setpoint_mv"], uirev=tr["kind"],
                    xtitle="time [ms]" + ("  (one ramp period, triggered on the ramp)" if tr["kind"] == "ramp"
                                          else "  (live)"))

    def fmt(x, f="{:+.4g}"):
        return "?" if x is None else f.format(x)

    @app.callback([Output("frame", "data"), Output("frame-seq", "data"), Output("status", "children"),
                   Output("stats", "children"), Output("rb-pid", "children"), Output("rb-filt", "children"),
                   Output("rb-asg", "children"), Output("sw-output", "className"), Output("sw-ramp", "className"),
                   Output("sw-pi", "className"), Output("log", "children"), Output("tick", "interval"),
                   Output("push-version", "data")],
                  Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"),
                  State("push-version", "data"))
    def on_tick(_, synced, frame_seq, pushed):
        snap = ctl.snapshot()
        s, mode, hw, tr = snap["settings"], snap["mode"], snap["hw"], snap["trace"]
        version_new = snap["version"] != synced
        frame = frame_data(snap) if (snap["seq"] != frame_seq or version_new) else no_update
        color = {"ramp": "#2471a3", "dc": "#7f8c8d", "pi": "#1e8e3e", "off": "#c0392b"}.get(mode, "#555")
        label = {"ramp": "RAMP", "dc": "OFFSET ONLY (PI off)", "pi": "PI ON", "off": "OUTPUT OFF"}.get(mode, mode.upper())
        status = html.Span([html.Span(label, style={"color": color}),
                            html.Span(f"   {snap['fps']:.1f} traces/s", style={"fontSize": "13px"})])
        stats = []
        if tr is not None:
            e, o = tr["in1"], tr["out1"]
            stats.append(f"IN1 mean {np.mean(e)*1e3:+.2f} mV, rms {np.std(e)*1e3:.3f} mV   ·   "
                         f"OUT1 mean {np.mean(o)*1e3:+.2f} mV")
        iv = snap.get("ival")
        warn = None
        if mode == "pi" and iv is not None:
            stats.append(f"   ·   integrator {iv*1e3:+.2f} mV")
            lo, hi = hw.get("pid0.min_voltage"), hw.get("pid0.max_voltage")
            if lo is not None and hi is not None and hi > lo and (iv <= lo + 0.01 * (hi - lo) or
                                                                  iv >= hi - 0.01 * (hi - lo)):
                warn = "   PI OUTPUT AT THE CLAMP (check the signs of p and i, or the setpoint)"
        stats_div = html.Span(["".join(stats)] + ([html.Span(warn, className="warn")] if warn else []))
        rb_pid = (f"hw: p {fmt(hw.get('pid0.p'), '{:+.6g}')}, i {fmt(hw.get('pid0.i'), '{:+.6g}')} Hz, "
                  f"setpoint {fmt((hw.get('pid0.setpoint') or 0)*1e3, '{:+.2f}')} mV\n"
                  f"    clamp {fmt((hw.get('pid0.min_voltage') or 0)*1e3, '{:+.0f}')}.."
                  f"{fmt((hw.get('pid0.max_voltage') or 0)*1e3, '{:+.0f}')} mV, input {hw.get('pid0.input')}, "
                  f"output {hw.get('pid0.output_direct')}")
        filt = hw.get("pid0.inputfilter") or []
        on = [f for f in filt if f]
        rb_filt = "hw: off" if not on else "hw: " + ", ".join(
            (f"LP {f:.1f} Hz" if f > 0 else f"HP {-f:.1f} Hz") for f in on)
        a_amp, a_off = hw.get("asg0.amplitude") or 0, hw.get("asg0.offset") or 0
        if hw.get("asg0.output_direct") != "out1":
            rb_asg = "hw: asg0 off"
        elif a_amp > 0:
            wf = {"ramp": "triangle", "halframp": "sawtooth"}.get(hw.get("asg0.waveform"), hw.get("asg0.waveform"))
            rb_asg = (f"hw: {wf} {fmt(hw.get('asg0.frequency'), '{:.4g}')} Hz, "
                      f"{(a_off-a_amp)*1e3:+.0f}..{(a_off+a_amp)*1e3:+.0f} mV")
        else:
            rb_asg = f"hw: DC {a_off*1e3:+.2f} mV"
        sw = ctl.switches()
        cls = ["tg on" if sw[n] else "tg" for n in ("output", "ramp", "pi")]
        return [frame, snap["seq"], status, stats_div, rb_pid, rb_filt, rb_asg] + cls + [
            "\n".join(reversed(snap["logs"][-80:])),
            int(1000 / max(float(s["ui_hz"]), 1)) if version_new else no_update,
            snap["version"] if version_new and pushed != snap["version"] else no_update]

    @app.callback([Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
                  Input("push-version", "data"), prevent_initial_call=True)
    def on_push(v):
        s = dict(ctl.s)
        return [s[k] for k in KEYS] + [v]

    return app
