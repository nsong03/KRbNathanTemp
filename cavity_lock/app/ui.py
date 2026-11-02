"""
ui.py -- the browser UI (Dash): a Moku-style schematic of the real signal chain, and the embedded scope.

The schematic is built from schematic.py (the same model the hardware writers use):
  * boxes (absolutely positioned Dash components on a fixed canvas, so native inputs keep working), each with
    its label, the register readback ("hardware truth") and 1-2 inline knobs; clicking the title opens a
    popover with the rest of its parameters;
  * wires: an SVG layer drawn by a small clientside callback (DIAG_JS), live wires coloured, dead ones grey;
  * switches (Scan, Dither, Lock, Output, Auto-relock) and probe points (2 scope channels) on the wires.
The server sends compact data: `frame` (traces, every new capture) and `diag` (readback text, switch and wire
states, only when it changes). The browser draws both (Plotly.react with SVG scatter; plain DOM updates).
"""
import json
import time

import numpy as np

import schematic as SCH

PANEL_LABEL = {"drive": "probes A/B"}
SHORT = {"ac_hz": "Hz", "lpf_hz": "Hz", "lpf_order": "order", "err_gain": "gain ×", "setpoint_mv": "mV",
         "ugf_hz": "UGF Hz", "clamp_auto": "auto (mode spacing)", "phase_deg": "deg", "acq_dither_hz": "Hz",
         "acq_dither_mvpp": "mVpp", "ramp_hz": "Hz", "ramp_mvpp": "mVpp", "out2_signal": "", "in1_scale": "×",
         "kappa_mhz": "κ MHz", "driver_bw_hz": "Hz"}
COLORS = {"A": "#e67e22", "B": "#8e44ad"}

CSS = """
body { margin: 0; font-family: sans-serif; }
#canvas-wrap { overflow-x: auto; }
#canvas { position: relative; width: %(W)dpx; height: %(H)dpx; background: #fbfbfb; border: 1px solid #ddd;
          border-radius: 8px; margin: 4px 0 8px; }
#wires { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 1; }
.w { fill: none; stroke-width: 2; }
.w.on.sig { stroke: #1f5fa8; } .w.on.lo { stroke: #7d3c98; } .w.on.drv { stroke: #d35400; }
.w.on.plant { stroke: #9a9a9a; } .w.on.mon { stroke: #16a085; }
.w.off { stroke: #c8c8c8; stroke-dasharray: 5 4; }
.blk { position: absolute; box-sizing: border-box; background: white; border: 1.5px solid #555; border-radius: 6px;
       z-index: 2; font-size: 12px; padding: 0 6px 4px; overflow: hidden; }
.blk.plant { background: #eeeeee; border: 1.5px dashed #aaa; color: #777; }
.blk.plant .hdr, .blk.plant .rb, .b-testgen .hdr, .b-testgen .rb { display: inline-block; margin-right: 6px; }
.blk.node { border-radius: 50%%; padding: 0; display: flex; align-items: center; justify-content: center;
            font-size: 20px; cursor: pointer; }
.blk.node.term { border-radius: 5px; font-size: 13px; font-weight: bold; flex-direction: column; }
.blk.node .rb { font-size: 10px; text-align: center; }
.blk.node.term .rb { white-space: normal; padding: 0 3px; }
.hdr { font-weight: bold; cursor: pointer; padding: 3px 0 2px; line-height: 1.15; }
.hdr:hover { color: #1f5fa8; text-decoration: underline; }
.rb { font-family: Consolas, Menlo, monospace; font-size: 10.5px; line-height: 1.3; white-space: nowrap; }
.rb .dim { color: #888; } .rb .warn { color: #c0392b; font-weight: bold; }
.rb .badge-acq { color: #2471a3; font-weight: bold; } .rb .badge-lock { color: #1e8e3e; font-weight: bold; }
.inl { display: flex; gap: 4px; align-items: center; margin-top: 3px; flex-wrap: wrap; }
.ic { display: inline-flex; align-items: center; gap: 2px; font-size: 11px; color: #444; }
.ic .dash-input-container { width: 66px; min-width: 0; height: 22px; }
.ic .dash-input-element { width: 100%%; font-size: 12px; padding: 1px 3px; }
.ic .dash-input-stepper { display: none; }
.pop { position: absolute; z-index: 30; background: white; border: 1px solid #888; border-radius: 6px;
       box-shadow: 0 4px 14px rgba(0,0,0,.25); padding: 6px 10px 8px; width: 310px; font-size: 12px; }
.pop .ptitle { font-weight: bold; display: flex; justify-content: space-between; margin-bottom: 4px; }
.pop .px { cursor: pointer; color: #888; padding: 0 4px; } .pop .px:hover { color: #000; }
.pop .row { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.pop .row .l { flex: 1; }
.pop .dash-input-container { width: 110px; height: 26px; }
.pop .note { color: #555; margin-top: 6px; font-size: 11px; line-height: 1.35; }
.pop .sub { font-weight: bold; margin-top: 6px; color: #444; }
.sw { position: absolute; z-index: 5; cursor: pointer; text-align: center; width: 76px; height: 34px;
      margin: -26px 0 0 -38px; }
.sw .lab { font-size: 11px; font-weight: bold; white-space: nowrap; line-height: 14px; }
.pill { width: 38px; height: 18px; border-radius: 9px; background: #b5b5b5; position: relative; margin: 1px auto 0;
        border: 1px solid #777; box-sizing: border-box; }
.pill:after { content: ''; position: absolute; top: 1px; left: 1px; width: 14px; height: 14px; border-radius: 50%%;
              background: white; transition: left .15s; }
.pill.on { background: #1e8e3e; border-color: #146c2e; } .pill.on:after { left: 21px; }
.pill.busy { opacity: .5; }
.sw:hover .pill { box-shadow: 0 0 0 2px #9cc3f0; }
.probe { position: absolute; z-index: 6; width: 18px; height: 18px; margin: -9px 0 0 -9px; cursor: pointer; }
.probe.grey { cursor: not-allowed; }
.pdot { display: block; width: 18px; height: 18px; border-radius: 50%%; border: 2px solid #333; background: white;
        box-sizing: border-box; font-size: 10px; font-weight: bold; line-height: 14px; text-align: center; }
.pdot.grey { border-color: #cdcdcd; background: #f1f1f1; }
.pdot.A { background: #e67e22; border-color: #b85f12; color: white; }
.pdot.B { background: #8e44ad; border-color: #6c3483; color: white; }
.probe:hover .pdot:not(.grey) { box-shadow: 0 0 0 3px #9cc3f0; }
.term { position: absolute; font-size: 11px; font-weight: bold; color: #666; z-index: 2; }
.plantframe { position: absolute; border: 1px dashed #c4c4c4; border-radius: 8px; z-index: 0; }
.plantframe span { position: absolute; right: 8px; bottom: 2px; font-size: 10px; color: #999; }
.bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 13px; position: relative; }
.btn { margin: 2px; padding: 5px 10px; font-size: 13px; cursor: pointer; }
.gear { cursor: pointer; padding: 4px 8px; border: 1px solid #bbb; border-radius: 4px; background: #f6f6f6;
        font-size: 13px; }
"""

# Embedded scope: transmission, error (with Lock Assist crosshairs), probes A/B, plus the setpoint history.
SCOPE_JS = """
function(fr) {
  var nu = window.dash_clientside.no_update;
  if (!fr) { return [nu, nu]; }
  var C = {pd: '#222222', err: '#1f5fa8', A: '#e67e22', B: '#8e44ad', ref: '#d35400', tgt: '#1e8e3e'};
  var rows = fr.rows, n = rows.length, data = [], shapes = [], ann = [], lay = {};
  var gap = 0.06, h = (1 - gap * (n - 1)) / Math.max(n, 1);
  var titles = {transmission: 'Transmission (photodiode, IN1) [V]', error: 'Error signal (iq0) [V]',
                drive: 'Probes A / B [mV]'};
  rows.forEach(function(r, i) {
    var ya = i === 0 ? 'y' : 'y' + (i + 1), key = i === 0 ? 'yaxis' : 'yaxis' + (i + 1);
    var top = 1 - i * (h + gap);
    lay[key] = {domain: [top - h, top], anchor: 'x', zeroline: false};
    ann.push({text: titles[r], x: 0.5, xref: 'paper', y: top, yref: 'paper', yanchor: 'bottom', showarrow: false,
              font: {size: 13}});
    if (r === 'transmission' && fr.pd) {
      data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr.pd, name: 'photodiode', yaxis: ya,
                 line: {color: C.pd, width: 1}});
      (fr.peaks || []).forEach(function(p) {
        data.push({type: 'scatter', mode: 'markers+text', x: p.t, y: p.t.map(function() { return p.y; }),
                   text: p.t.map(function() { return p.label; }), textposition: 'top center', yaxis: ya,
                   marker: {symbol: p.tgt ? 'star' : 'triangle-down', size: p.tgt ? 13 : 9,
                            color: p.tgt ? C.tgt : 'black'}, showlegend: false, hoverinfo: 'skip'});
      });
      if (fr.lock_height !== null && fr.lock_height !== undefined) {
        shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: fr.lock_height, y1: fr.lock_height,
                     line: {color: C.tgt, dash: 'dot', width: 1}});
      }
    }
    if (r === 'error' && fr.err) {
      data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr.err, name: 'error', yaxis: ya,
                 line: {color: C.err, width: 1}});
      shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: 0, y1: 0, line: {color: 'gray', width: 1}});
      var zc = fr.zc || [], ok = zc.filter(function(z) { return z.ok; }), bad = zc.filter(function(z) { return !z.ok; });
      if (ok.length) {
        data.push({type: 'scatter', mode: 'markers', x: ok.map(function(z) { return z.t; }),
                   y: ok.map(function() { return 0; }), customdata: ok.map(function(z) { return z.v; }),
                   yaxis: ya, name: 'lock points', showlegend: false,
                   marker: {symbol: 'cross-thin', size: 22, line: {color: C.tgt, width: 2.5}},
                   hovertemplate: 'Lock Assist: click to target this peak (%{customdata:.4f} V)<extra></extra>'});
      }
      if (bad.length) {
        data.push({type: 'scatter', mode: 'markers', x: bad.map(function(z) { return z.t; }),
                   y: bad.map(function() { return 0; }), yaxis: ya, showlegend: false, hoverinfo: 'skip',
                   marker: {symbol: 'cross-thin', size: 14, line: {color: '#aaaaaa', width: 1.5}}});
      }
    }
    if (r === 'drive' && fr.dt) {
      if (fr.pa) data.push({type: 'scatter', mode: 'lines', x: fr.dt, y: fr.pa, name: 'A: ' + fr.pa_name, yaxis: ya,
                            line: {color: C.A, width: 1}});
      if (fr.pb) data.push({type: 'scatter', mode: 'lines', x: fr.dt, y: fr.pb, name: 'B: ' + fr.pb_name, yaxis: ya,
                            line: {color: C.B, width: 1.3}});
    }
  });
  (fr.v0_t || []).forEach(function(tv) {
    shapes.push({type: 'line', xref: 'x', x0: tv, x1: tv, yref: 'paper', y0: 0, y1: 1,
                 line: {color: C.tgt, dash: 'dash', width: 1}});
  });
  lay.xaxis = {title: {text: fr.xtitle}, anchor: n > 0 ? (n === 1 ? 'y' : 'y' + n) : 'y'};
  lay.shapes = shapes; lay.annotations = ann;
  lay.height = 190 * Math.max(n, 1) + 70; lay.margin = {l: 55, r: 15, t: 30, b: 45};
  lay.template = 'plotly_white'; lay.hovermode = 'x'; lay.uirevision = fr.uirev;
  lay.legend = {orientation: 'h', y: 1.04, x: 1, xanchor: 'right', yanchor: 'bottom'};
  lay.plot_bgcolor = 'white'; lay.paper_bgcolor = 'white';
  var scope = {data: data, layout: lay};
  var hist = nu;
  if (fr.hist) {
    var H = fr.hist, hd = [
      {type: 'scatter', mode: 'lines', x: H.t, y: H.sp, name: 'setpoint [mV]', line: {color: C.ref}},
      {type: 'scatter', mode: 'lines', x: H.t, y: H.T, name: 'transmission [V]', yaxis: 'y2',
       line: {color: C.pd, width: 1}}];
    var hs = (H.unlocks || []).map(function(u) {
      return {type: 'line', xref: 'x', x0: u, x1: u, yref: 'paper', y0: 0, y1: 1, line: {color: 'red', width: 1}}; });
    hist = {data: hd, layout: {title: {text: 'Setpoint (OUT1 DC from pid0) over time', font: {size: 14}}, height: 260,
            xaxis: {title: {text: 'time [s]'}}, yaxis: {title: {text: 'setpoint [mV]'}},
            yaxis2: {title: {text: 'transmission [V]'}, overlaying: 'y', side: 'right'}, shapes: hs,
            margin: {l: 55, r: 55, t: 35, b: 40}, template: 'plotly_white', uirevision: 'hist',
            plot_bgcolor: 'white', paper_bgcolor: 'white',
            legend: {orientation: 'h', y: 1.12, x: 1, xanchor: 'right'},
            annotations: H.t.length ? [] : [{text: 'fills in once locked (or held)', showarrow: false,
                                            xref: 'paper', yref: 'paper', x: 0.5, y: 0.5}]}};
  }
  return [scope, hist];
}
"""

# Diagram: wires (SVG, built once), readback text, switch positions and probe labels (plain DOM updates).
DIAG_JS = """
function(d, g) {
  var nu = window.dash_clientside.no_update;
  if (!d || !g) { return nu; }
  var host = document.getElementById('wires');
  if (!host) { return nu; }
  if (!host.firstChild) {
    var col = {sig: '#1f5fa8', lo: '#7d3c98', drv: '#d35400', plant: '#9a9a9a', mon: '#16a085', off: '#c8c8c8'};
    var s = '<svg xmlns="http://www.w3.org/2000/svg" width="' + g.W + '" height="' + g.H + '"><defs>';
    Object.keys(col).forEach(function(k) {
      s += '<marker id="ah-' + k + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" ' +
           'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="' + col[k] + '"/></marker>';
    });
    s += '</defs>';
    g.wires.forEach(function(w) {
      s += '<path id="wire-' + w.id + '" class="w ' + w.kind + ' off" d="M' +
           w.pts.map(function(p) { return p[0] + ',' + p[1]; }).join(' L') + '"/>';
    });
    host.innerHTML = s + '</svg>';
  }
  g.wires.forEach(function(w) {
    var el = document.getElementById('wire-' + w.id);
    if (!el) { return; }
    var on = !!d.wires[w.id];
    el.setAttribute('class', 'w ' + w.kind + (on ? ' on' : ' off'));
    el.setAttribute('marker-end', 'url(#ah-' + (on ? w.kind : 'off') + ')');
  });
  function esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  Object.keys(d.boxes).forEach(function(b) {
    var el = document.getElementById('rb-' + b);
    if (el) {
      el.innerHTML = d.boxes[b].map(function(l) {
        return '<div class="' + (l[1] || '') + '" title="' + esc(l[0]) + '">' + esc(l[0]) + '</div>'; }).join('');
    }
  });
  Object.keys(d.sw).forEach(function(k) {
    var st = d.sw[k], p = document.getElementById('swp-' + k), l = document.getElementById('swl-' + k);
    if (p) { p.className = 'pill' + (st.on ? ' on' : '') + (st.busy ? ' busy' : ''); }
    if (l) { l.textContent = st.label; }
  });
  Object.keys(d.probes).forEach(function(q) {
    var e = document.getElementById('pb-' + q);
    if (e) {
      var c = d.probes[q];
      e.className = 'pdot' + (c ? ' ' + c : '') + (e.getAttribute('data-grey') ? ' grey' : '');
      e.textContent = c;
    }
  });
  return Date.now();
}
"""

# Swept-sine Bode plot, filled in point by point: measured (solid, markers) over the model (dotted).
BODE_JS = """
function(B) {
  var nu = window.dash_clientside.no_update;
  if (!B) { return nu; }
  var data = [];
  B.curves.forEach(function(c) {
    if (c.mdb) {
      data.push({type: 'scatter', mode: 'lines', x: B.fp, y: c.mdb, name: c.name + ' (model)', legendgroup: c.name,
                 showlegend: false, line: {color: c.color, dash: 'dot', width: 1.5}, xaxis: 'x', yaxis: 'y'});
      data.push({type: 'scatter', mode: 'lines', x: B.fp, y: c.mph, legendgroup: c.name, showlegend: false,
                 line: {color: c.color, dash: 'dot', width: 1.5}, xaxis: 'x2', yaxis: 'y2', hoverinfo: 'skip'});
    }
    data.push({type: 'scatter', mode: 'lines+markers', x: c.f, y: c.db || [], name: c.name, legendgroup: c.name,
               line: {color: c.color, width: 2}, marker: {size: 5}, xaxis: 'x', yaxis: 'y'});
    data.push({type: 'scatter', mode: 'lines+markers', x: c.f, y: c.ph || [], legendgroup: c.name, showlegend: false,
               line: {color: c.color, width: 2}, marker: {size: 5}, xaxis: 'x2', yaxis: 'y2'});
  });
  var prog = B.done ? (B.n < B.n_total ? ' (stopped at ' + B.n + '/' + B.n_total + ')' : '')
                    : '  measuring ' + B.n + '/' + B.n_total + ' ...';
  var res = (B.ugf ? '   unity gain ' + B.ugf.toFixed(1) + ' Hz, phase margin ' + B.pm.toFixed(0) + ' deg' : '');
  var shapes = [{type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: 0, y1: 0, line: {color: 'gray', width: 1}},
                {type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y2', y0: -180, y1: -180,
                 line: {color: 'red', width: 1, dash: 'dot'}}];
  if (B.ugf) {
    shapes.push({type: 'line', xref: 'x', x0: B.ugf, x1: B.ugf, yref: 'paper', y0: 0, y1: 1,
                 line: {color: '#1e8e3e', width: 1, dash: 'dash'}});
  }
  var lay = {title: {text: B.title + prog + res, font: {size: 14}, x: 0.01, xanchor: 'left'}, height: 600,
             template: 'plotly_white', margin: {l: 60, r: 20, t: 40, b: 120}, uirevision: 'bode-' + B.mode,
             shapes: shapes,
             xaxis: {type: 'log', anchor: 'y', domain: [0, 1], showticklabels: false},
             xaxis2: {type: 'log', anchor: 'y2', matches: 'x', title: {text: 'frequency [Hz]'}},
             yaxis: {domain: [0.52, 1], title: {text: 'magnitude [dB]'}},
             yaxis2: {domain: [0, 0.45], title: {text: 'phase [deg]'}},
             legend: {orientation: 'h', y: -0.13, x: 0, xanchor: 'left', yanchor: 'top'},
             annotations: [{text: B.note, xref: 'paper', yref: 'paper', x: 0, y: -0.24, showarrow: false,
                            xanchor: 'left', font: {size: 11, color: '#555'}}],
             plot_bgcolor: 'white', paper_bgcolor: 'white'};
  return {data: data, layout: lay};
}
"""

# Popovers: clicking a block title opens its popover (and closes the others); x or a second click closes it.
POP_JS = """
function(hdr, px, styles) {
  var cc = window.dash_clientside.callback_context;
  var trig = cc.triggered.length ? cc.triggered[0].prop_id : '';
  var ids = cc.states_list[0].map(function(s) { return s.id.b; });
  var which = null, isHdr = trig.indexOf('"hdr"') >= 0;
  if (trig.charAt(0) === '{' && cc.triggered[0].value) {
    which = JSON.parse(trig.slice(0, trig.lastIndexOf('.'))).b;
  }
  return styles.map(function(st, i) {
    var s = Object.assign({}, st), open = s.display !== 'none';
    s.display = (isHdr && ids[i] === which && !open) ? 'block' : 'none';
    return s;
  });
}
"""


def build_app(ctl, A):
    """A: the cavity_lock_app module (FIELDS, analysis helpers)."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update, ALL

    app = Dash(__name__, title="Cavity lock", update_title=None)
    app.index_string = app.index_string.replace(
        "{%css%}", "{%css%}<style>" + CSS % dict(W=SCH.CANVAS_W, H=SCH.CANVAS_H) + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS
    WIDGET = {p["key"]: p["widget"] for b in SCH.BLOCKS for p in b["params"] if p.get("widget")}

    def to_ui(key, val):
        kind = FIELD[key][4]
        return (["on"] if val else []) if kind == "check" else val

    def from_ui(key, val):
        kind, default = FIELD[key][4], FIELD[key][2]
        if kind == "check":
            return bool(val)
        if kind == "multi":
            return [p for p in A.PANELS if p in (val or [])]
        if kind == "select":
            return val
        if val is None:
            return ctl.s.get(key, default)
        return int(val) if isinstance(default, int) and not isinstance(default, bool) else float(val)

    def control(key, short=None):
        """The one input for a settings key: inline in a box (short label) or a popover row (full label)."""
        _, label, default, _, kind, opt = FIELD[key]
        val = to_ui(key, ctl.s.get(key, default))
        widget = WIDGET.get(key)
        cid = f"s-{key}"
        if kind == "num" and widget == "radio":
            comp = dcc.RadioItems(id=cid, options=[{"label": f" {o}", "value": o} for o in (1, 2)], value=val,
                                  inline=True, inputStyle={"marginLeft": "4px"})
        elif kind == "num":
            comp = dcc.Input(id=cid, type="number", value=val, debounce=True,
                             step=1 if widget == "int" else "any", **opt)
        elif kind == "check":
            comp = dcc.Checklist(id=cid, options=[{"label": "", "value": "on"}], value=val,
                                 style={"display": "inline-block"})
        elif kind == "multi":
            comp = dcc.Checklist(id=cid, options=[{"label": " " + PANEL_LABEL.get(o, o), "value": o}
                                                  for o in opt["options"]],
                                 value=val, inline=True, inputStyle={"marginLeft": "8px"})
            return html.Span([html.Span(label + ":"), comp], className="ic", style={"fontSize": "13px"})
        else:
            comp = dcc.Dropdown(id=cid, options=[{"label": o, "value": o} for o in opt["options"]], value=val,
                                clearable=False, searchable=False,
                                style={"width": "120px" if short is None else "112px", "fontSize": "12px"})
        if short is not None:
            return html.Span([html.Span(short), comp] if short else [comp], className="ic", title=label)
        return html.Div([html.Span(label, className="l"), comp], className="row")

    def popover(pid, title, body, style):
        return html.Div([html.Div([html.Span(title), html.Span("×", id={"type": "popx", "b": pid}, className="px")],
                                  className="ptitle")] + body,
                        id={"type": "pop", "b": pid}, className="pop", style=dict(style, display="none"))

    # ---- the schematic ----
    def block(b):
        pos = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
        hdr_id = {"type": "hdr", "b": b["id"]}
        if b["kind"] == "node":
            term = b["id"] in ("out1", "out2term")
            return html.Div([html.Span(b["label"]), html.Div(id=f"rb-{b['id']}", className="rb")],
                            id=hdr_id, className="blk node" + (" term" if term else ""), style=pos,
                            title="click for details")
        inline = [p["key"] for p in b["params"] if p["inline"]]
        kids = [html.Div(b["label"] + "  ⚙", id=hdr_id, className="hdr", title="click for all settings"),
                html.Div(id=f"rb-{b['id']}", className="rb")]
        knobs = [control(k, SHORT.get(k, "")) for k in inline]
        if b["id"] == "phase":
            knobs.append(html.Button("Auto", id="btn-phase", title="Auto-optimize phase (4-phase fit at the target "
                                                                   "peak; needs Scan and Dither on)",
                                     style={"fontSize": "11px", "padding": "1px 6px", "cursor": "pointer"}))
        if knobs:
            kids.append(html.Div(knobs, className="inl"))
        return html.Div(kids, className=f"blk {b['kind']} b-{b['id']}", style=pos)

    def block_pop(b):
        rest = [p["key"] for p in b["params"] if not p["inline"]]
        body = [control(k) for k in rest]
        if b["opts"]:
            body.append(html.Div({"pid": "Lock stages (sequence, no register)",
                                  "limiter": "Watchdog (Auto-relock switch)"}.get(b["id"], "Options"), className="sub"))
            body += [control(k) for k in b["opts"]]
        regs = sorted({p["attr"] for p in b["params"] if p["attr"]})
        if regs:
            body.append(html.Div("Writes: " + ", ".join(regs), className="note"))
        body.append(html.Div(b["note"], className="note"))
        W, H = SCH.CANVAS_W, SCH.CANVAS_H
        left = max(4, min(b["x"], W - 320))
        st = {"left": left, "bottom": H - b["y"] + 4} if b["y"] > 300 else {"left": left, "top": b["y"] + b["h"] + 4}
        return popover(b["id"], b["label"], body, st)

    def switch(sw):
        return html.Div([html.Div(sw["label"], id=f"swl-{sw['id']}", className="lab"),
                         html.Div(id=f"swp-{sw['id']}", className="pill")],
                        id={"type": "sw", "s": sw["id"]}, className="sw", style={"left": sw["x"], "top": sw["y"]},
                        title={"scan": "Scan: asg0 ramp into OUT1 (on while locked asks to unlock first)",
                               "dither": "Dither: iq0.output_direct (off while locked unlocks and holds first)",
                               "lock": "Lock: run the lock sequence (scan off, phase re-fit, zero crossing, park, "
                                       "gain ramp); off = unlock and hold",
                               "output": "Output: OUT1 enable (off: ramp, dither and PID disconnected, OUT1 = 0 V)",
                               "relock": "Auto-relock: the watchdog re-sweeps and relocks after an unlock"}[sw["id"]])

    def probe(p):
        pos = {"left": p["x"], "top": p["y"]}
        if p["signal"]:
            return html.Div(html.Span(id=f"pb-{p['id']}", className="pdot"), id={"type": "probe", "p": p["signal"]},
                            className="probe", style=pos,
                            title=f"probe {p['label']} (click to place on scope channel A/B, again to remove)")
        return html.Div(html.Span(id=f"pb-{p['id']}", className="pdot grey", **{"data-grey": "1"}),
                        className="probe grey", style=pos, title="not probe-able: " + p["why"])

    def canvas():
        kids = [html.Div(id="wires"),
                html.Div(html.Span("external (display only)"), className="plantframe",
                         style={"left": 10, "top": 4, "width": 812, "height": 76})]
        kids += [html.Div(t["text"], className="term", style={"left": t["x"], "top": t["y"]}) for t in SCH.TERMINALS]
        kids += [block(b) for b in SCH.BLOCKS]
        kids += [switch(s) for s in SCH.SWITCHES]
        kids += [probe(p) for p in SCH.PROBES]
        kids += [block_pop(b) for b in SCH.BLOCKS]
        return html.Div(html.Div(kids, id="canvas"), id="canvas-wrap")

    # ---- page ----
    def serve_layout():
        measure_pop = popover("measure", "Measurement settings", [control(k) for k in SCH.HOMES["measure"]]
                              + [html.Div("Measure noise and Measure loop work while locked.", className="note")],
                              {"right": 0, "top": 38})
        scope_pop = popover("scope", "Scope settings", [control(k) for k in SCH.HOMES["scope"]]
                            + [html.Div("Probes A/B other than in1/iq0 take a second capture every N traces.",
                                        className="note")], {"left": 0, "top": 34})
        return html.Div([
            html.Div([
                html.H3("Cavity PZT dither lock", style={"margin": "4px 12px 4px 0"}),
                html.Div(id="status", style={"fontSize": "16px", "fontWeight": "bold", "flex": "1"}),
                html.Button("Measure noise", id="btn-noise", className="btn"),
                html.Button("Measure loop", id="btn-loop", className="btn"),
                html.Button("Measure Bode plot", id="btn-bode", className="btn",
                            title="swept sine from the internal generator (asg1), recorded with the scope, point by "
                                  "point. closed loop: while locked, a small sine at OUT1. circuit only: spare copies "
                                  "of the lock-in and PID, OUT1 untouched"),
                control("bode_mode", ""),
                html.Button("Stop", id="btn-stop", className="btn", title="stop the running Bode sweep"),
                html.Span("⚙", id={"type": "hdr", "b": "measure"}, className="gear", title="measurement settings"),
                html.Button("Save settings", id="btn-save", className="btn"),
                html.Button("Load settings", id="btn-load", className="btn"),
                measure_pop,
            ], className="bar"),
            canvas(),
            html.Div([
                html.B("Embedded scope"),
                html.Span("Probe A", style={"color": COLORS["A"], "fontWeight": "bold"}), control("probe_a", ""),
                html.Span("Probe B", style={"color": COLORS["B"], "fontWeight": "bold"}), control("probe_b", ""),
                control("panels"),
                html.Span("⚙ scope", id={"type": "hdr", "b": "scope"}, className="gear"),
                scope_pop,
            ], className="bar", style={"marginBottom": "4px"}),
            html.Div([
                html.B("Lock target:"), html.Span(id="target-text"),
                html.Span("click a peak or a green crosshair (error zero crossing) in the scope",
                          style={"color": "#666", "fontSize": "12px"}),
                html.Button("Use tallest peak", id="btn-tallest", className="btn"),
            ], className="bar"),
            dcc.Graph(id="scope", config={"displaylogo": False}),
            dcc.Graph(id="history", config={"displaylogo": False}),
            dcc.Graph(id="noise", config={"displaylogo": False}, style={"display": "none"}),
            dcc.Graph(id="loopfig", config={"displaylogo": False}, style={"display": "none"}),
            dcc.Graph(id="bodefig", config={"displaylogo": False}, style={"display": "none"}),
            html.Div([
                html.Div(id="analysis", style={"flex": "1", "fontSize": "13px", "whiteSpace": "pre-wrap"}),
                html.Pre(id="log", style={"flex": "1", "fontSize": "12px", "height": "26vh", "overflowY": "auto",
                                          "whiteSpace": "pre-wrap", "background": "#f6f6f6", "padding": "6px",
                                          "margin": 0}),
            ], style={"display": "flex", "gap": "10px"}),
            dcc.ConfirmDialog(id="confirm-scan", message="The cavity is locked. Turning Scan on unlocks it "
                                                         "(the setpoint is dropped and the ramp starts). Continue?"),
            dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
            dcc.Store(id="synced", data=-1),                  # -1: the first tick pushes all present values
            dcc.Store(id="frame"),
            dcc.Store(id="frame-seq", data=-1),
            dcc.Store(id="meas-seq", data=-1),
            dcc.Store(id="bode"),
            dcc.Store(id="bode-seq", data=-1),
            dcc.Store(id="diag"),
            dcc.Store(id="diag-key", data=""),
            dcc.Store(id="diag-ack"),
            dcc.Store(id="geom", data=SCH.geometry()),
            dcc.Store(id="ack"),
        ], style={"fontFamily": "sans-serif", "padding": "6px 12px"})

    app.layout = serve_layout

    # ---- callbacks: settings, buttons, switches, probes, clicks ----
    @app.callback(Output("ack", "data", allow_duplicate=True),
                  [Input(f"s-{k}", "value") for k in KEYS], State("synced", "data"), prevent_initial_call=True)
    def on_settings(*vals):
        vals, synced = vals[:-1], vals[-1]
        trig = (ctx.triggered_id or "").replace("s-", "")
        if trig in FIELD and FIELD[trig][4] == "num" and vals[KEYS.index(trig)] is None:
            lim = FIELD[trig][5]
            ctl.log(f"'{FIELD[trig][1]}': not a number or outside "
                    f"[{lim.get('min', '-inf')}, {lim.get('max', 'inf')}]; keeping {ctl.s[trig]}")
        ctl.submit("apply", ({k: from_ui(k, v) for k, v in zip(KEYS, vals)}, synced))
        return time.time()

    btn_cmd = {"btn-phase": "autophase", "btn-save": "save", "btn-load": "load", "btn-tallest": "tallest",
               "btn-noise": "noise", "btn-loop": "loop", "btn-bode": "bode"}

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("btn-stop", "n_clicks"), prevent_initial_call=True)
    def on_stop(_):
        ctl.stop_measurement()                        # a flag, not a queued command: the sweep is running
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), [Input(b, "n_clicks") for b in btn_cmd],
                  prevent_initial_call=True)
    def on_button(*_):
        if ctx.triggered_id in btn_cmd:
            ctl.submit(btn_cmd[ctx.triggered_id])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Output("confirm-scan", "displayed"),
                  Input({"type": "sw", "s": ALL}, "n_clicks"), prevent_initial_call=True)
    def on_switch(_):
        t = ctx.triggered_id
        if not t or not ctx.triggered or not ctx.triggered[0].get("value"):
            return no_update, no_update
        name = t["s"]
        on = not ctl.switches()[name]                 # the click flips what the diagram shows
        if name == "scan" and on and ctl.mode in ("locked", "measuring"):
            return no_update, True                    # ask first (dcc.ConfirmDialog)
        ctl.submit("switch", (name, on))
        return time.time(), False

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("confirm-scan", "submit_n_clicks"),
                  prevent_initial_call=True)
    def on_confirm_scan(n):
        if n:
            ctl.submit("switch", ("scan", True))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "probe", "p": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_probe(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("probe", t["p"])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("scope", "clickData"), prevent_initial_call=True)
    def on_click(cd):
        pts = (cd or {}).get("points") or []
        cross = [p for p in pts if p.get("customdata") is not None]
        if cross:                                     # a Lock Assist crosshair: target the peak at that crossing
            ctl.submit("target_v", float(cross[0]["customdata"]))
        elif pts:
            ctl.submit("target", float(pts[0]["x"]))
        return time.time()

    app.clientside_callback(POP_JS, Output({"type": "pop", "b": ALL}, "style"),
                            Input({"type": "hdr", "b": ALL}, "n_clicks"), Input({"type": "popx", "b": ALL}, "n_clicks"),
                            State({"type": "pop", "b": ALL}, "style"), prevent_initial_call=True)
    app.clientside_callback(SCOPE_JS, [Output("scope", "figure"), Output("history", "figure")], Input("frame", "data"))
    app.clientside_callback(DIAG_JS, Output("diag-ack", "data"), Input("diag", "data"), State("geom", "data"))
    app.clientside_callback(BODE_JS, Output("bodefig", "figure"), Input("bode", "data"))

    # ---- the tick: compact data only ----
    def r5(a, scale=1.0):
        return np.round(np.asarray(a, dtype=float) * scale, 5).tolist()

    def frame_data(snap):
        s, tr, dr, an = snap["settings"], snap["traces"], snap["drive"], snap["analysis"] or {}
        rows = [p for p in ("transmission", "error", "drive") if p in s["panels"]]
        sweep = tr is not None and tr["kind"] == "sweep"
        fr = dict(rows=rows, uirev=f"{'sweep' if sweep else 'live'}-{len(rows)}",
                  xtitle="time [ms]" + ("  (one ramp period; click a peak or a crosshair to pick the lock target)"
                                        if sweep else "  (live)"))
        if tr is not None:
            t_ms = tr["t"] * 1e3
            if "transmission" in rows:
                x, y = A.envelope(t_ms, tr["pd"])
                fr.update(t=r5(x), pd=r5(y))
            if "error" in rows:
                x, y = A.envelope(t_ms, tr["err"])
                fr.update(t=r5(x), err=r5(y))
        if dr is not None and "drive" in rows and "a_name" in dr:
            t_ms = dr["t"] * 1e3
            for ch in ("a", "b"):
                if dr.get(ch) is not None:
                    x, y = A.envelope(t_ms, dr[ch] * 1e3)
                    fr["dt"], fr["p" + ch] = r5(x), r5(y)
            fr.update(pa_name=dr["a_name"], pb_name=dr["b_name"])
        if sweep and an.get("peaks"):
            fr["peaks"] = [dict(t=p["t_ms"], y=p["base"] + p["height"], label=f"{p['v']*1e3:+.0f} mV",
                                tgt=i == an.get("target")) for i, p in enumerate(an["peaks"])]
            fr["v0_t"] = an.get("v0_t_ms", [])
        if sweep and an.get("zc"):
            fr["zc"] = [dict(t=round(z["t_ms"], 4), ok=z["on_peak"], v=round(z["v"], 6)) for z in an["zc"]]
        if not sweep and snap["lock"].get("height"):
            fr["lock_height"] = snap["lock"]["height"]
        if "setpoint history" in s["panels"]:
            h = snap["history"][-6000:]
            t, sp, T, un = [], [], [], []
            if h:
                t0 = h[0][0]
                step = max(1, len(h) // 1500)
                last = None
                for j in range(0, len(h), step):
                    x = h[j]
                    tt = x[0] - t0
                    if last is not None and tt - last > 1.5:          # gap (e.g. while acquiring): break the lines
                        t.append(None); sp.append(None); T.append(None)
                    t.append(round(tt, 3)); sp.append(round(x[4] * 1e3, 4)); T.append(round(x[1], 5))
                    last = tt
                un = [round(x[0] - t0, 3) for x in h if x[5] == "unlock"]
            fr["hist"] = dict(t=t, sp=sp, T=T, unlocks=un)
        return fr

    @app.callback(
        [Output("frame", "data"), Output("frame-seq", "data"), Output("status", "children"),
         Output("target-text", "children"), Output("analysis", "children"), Output("log", "children"),
         Output("noise", "style"), Output("loopfig", "style"),
         Output("history", "style"), Output("meas-seq", "data"), Output("tick", "interval"),
         Output("diag", "data"), Output("diag-key", "data"), Output("bode", "data"), Output("bode-seq", "data"),
         Output("bodefig", "style")]
        + [Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
        Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"), State("meas-seq", "data"),
        State("diag-key", "data"), State("bode-seq", "data"))
    def on_tick(_, synced, frame_seq, meas_seq, diag_key, bode_seq):
        snap = ctl.snapshot()
        s, mode, an, li = snap["settings"], snap["mode"], snap["analysis"] or {}, snap["lock"]
        new_frame = snap["seq"] != frame_seq or snap["version"] != synced
        frame = frame_data(snap) if new_frame else no_update
        f_d, d_mvpp = snap["dither"]
        dm = f" (measured {snap['dither_meas']:.1f})" if snap["dither_meas"] else ""
        color = {"locked": "#1e8e3e", "acquire": "#2471a3", "held": "#b9770e", "busy": "#7d3c98",
                 "locking": "#7d3c98", "measuring": "#7d3c98", "off": "#c0392b"}.get(mode, "#555")
        label = {"acquire": "SCANNING", "off": "OUTPUT OFF"}.get(mode, mode.upper())
        dith = f"dither {d_mvpp:g} mVpp{dm} @ {f_d:g} Hz" if snap["dither_on"] else "dither OFF"
        if mode in ("locked", "measuring") and li.get("setpoint") is not None:
            extra = (f"   setpoint {li['setpoint']*1e3:+.3f} mV   transmission {li['frac']*100:.0f}% of peak   "
                     f"bandwidth {li.get('ugf_actual', 0):.1f} Hz   {dith}")
        elif mode == "acquire" and snap["ramp"]:
            rp = snap["ramp"]
            extra = f"   {rp['shape']} ramp {(rp['hi']-rp['lo'])*1e3:.0f} mVpp @ {rp['hz']:g} Hz   {dith}"
        elif mode == "held":
            extra = f"   setpoint held at {snap['history'][-1][4]*1e3:+.3f} mV" if snap["history"] else ""
        else:
            extra = ""
        if mode == "measuring" and snap["meas_progress"]:
            extra = "   " + snap["meas_progress"]
        extra += f"   {snap['fps']:.1f} traces/s" if snap["fps"] and mode != "measuring" else ""
        status = html.Span([html.Span(label, style={"color": color}), html.Span(extra, style={"fontSize": "13px"})])
        if an.get("target") is not None and mode in ("acquire", "busy"):
            p = an["peaks"][an["target"]]
            ttxt = (f"{'Chosen' if s.get('target_mv') is not None else 'Tallest'} peak at {p['v']*1e3:+.1f} mV, "
                    f"height {p['height']:.3f} V" + (f", lock point {an['v0']*1e3:+.2f} mV" if an.get("v0") is not None else ""))
        elif li.get("v0") is not None:
            ttxt = f"Locked peak: lock point {li['v0']*1e3:+.2f} mV, height {li['height']:.3f} V"
        else:
            ttxt = "no target yet"
        lines = []
        if mode in ("acquire", "busy", "locking") and an.get("peaks"):
            for i, p in enumerate(an["peaks"]):
                lines.append(f"{p['v']*1e3:+8.1f} mV   height {p['height']:.3f} V   FWHM {p['fwhm']*1e3:.2f} mV"
                             + ("   <- target" if i == an.get("target") else ""))
            if an.get("v0") is not None:
                lines.append(f"lock point {an['v0']*1e3:+.3f} mV, slope {an['slope']:+.1f} /V"
                             + (f", nearest other peak {an['mode_spacing']*1e3:.1f} mV away" if an.get("mode_spacing") else ""))
            lines += an.get("notes", [])
        elif li.get("v0") is not None:
            lines.append(f"lock point {li['v0']*1e3:+.3f} mV, slope {li['slope']:+.1f} /V, clamp "
                         f"{li['clamp'][0]*1e3:+.1f}..{li['clamp'][1]*1e3:+.1f} mV, relocks {li.get('relocks', 0)}")
            if li.get("err_rms") is not None:
                lines.append(f"error rms {li['err_rms']*1e3:.2f} mV  ->  about {abs(li['err_rms']/li['slope'])*1e6:.1f} uV "
                             f"of drive ({abs(li['err_rms']/li['slope'])/li['fwhm']*100:.2f}% of the swept FWHM)")
        lp_list = [s["lpf_hz"]] * int(s["lpf_order"])
        pm = A.predicted_margin(s, lp_list)
        fu, pmd = pm
        if fu:
            lines.append(f"model: unity gain {fu:.0f} Hz, phase margin {pmd:.0f} deg (integrator + error low-pass "
                         f"{' + '.join(f'{x:.0f} Hz' for x in lp_list)} + driver {s['driver_bw_hz']:g} Hz + 0.2 us)")
            if pmd < 45:
                lines.append("WARN phase margin under 45 deg: lower the loop bandwidth or raise the low-pass/dither frequency")
        nz = snap["noise"]
        if nz:
            lines.append("noise (linewidths rms | intensity rms) by band: " + ", ".join(
                f"{r['band']}: {r['det']*100 if r['det'] is not None else float('nan'):.2f}% | {r['rin']*100:.2f}%"
                for r in nz["rows"]))
        meas_new = snap["meas_seq"] != meas_seq
        show_noise = "noise spectrum" in s["panels"]
        show_loop = "loop (Bode)" in s["panels"]
        hist_style = {} if "setpoint history" in s["panels"] else {"display": "none"}
        diag = SCH.diagram_state(snap, pm)
        key = str(hash(json.dumps(diag, sort_keys=True)))
        diag_out, key_out = (diag, key) if key != diag_key else (no_update, no_update)
        if snap["version"] != synced:
            vals = [to_ui(k, s[k]) for k in KEYS] + [snap["version"]]
            interval = int(1000 / max(float(s["ui_hz"]), 1))
        else:
            vals = [no_update] * len(KEYS) + [no_update]
            interval = no_update
        return [frame, snap["seq"], status, ttxt, "\n".join(lines), "\n".join(reversed(snap["logs"][-80:])),
                {} if show_noise else {"display": "none"}, {} if show_loop else {"display": "none"},
                hist_style, snap["meas_seq"] if meas_new else no_update, interval, diag_out, key_out,
                A.bode_json(snap["bode"]) if snap["bode_seq"] != bode_seq else no_update,
                snap["bode_seq"] if snap["bode_seq"] != bode_seq else no_update,
                {} if "Bode (swept)" in s["panels"] else {"display": "none"}] + vals

    # The measurement plots get their own callbacks: sending both figures inside the 10 Hz tick response made
    # the browser drop the whole update (seen with Dash 4.5), freezing the page.
    @app.callback(Output("noise", "figure"), Input("meas-seq", "data"))
    def on_noise(_):
        return noise_fig(ctl.noise)

    @app.callback(Output("loopfig", "figure"), Input("meas-seq", "data"))
    def on_loop(_):
        return loop_fig(ctl.loop)

    return app


def noise_fig(nz):
    import plotly.graph_objects as go
    f = go.Figure()
    if nz:
        x = nz["f"]
        if nz["det_lw"] is not None:
            f.add_scatter(x=x, y=[v * nz["kappa_mhz"] * 1e6 for v in nz["det_lw"]], mode="lines",
                          name="residual detuning [Hz/rtHz]", line=dict(color="#1f5fa8", width=1))
        f.add_scatter(x=x, y=nz["rin"], mode="lines", name="relative intensity noise [1/rtHz]", yaxis="y2",
                      line=dict(color="#222222", width=1))
        for k in (1, 2, 3, 4):
            f.add_vline(x=k * nz["f_d"], line=dict(color="#d35400", width=0.8, dash="dot"))
    f.update_layout(title=dict(text="Noise while locked (dotted: dither harmonics)", font=dict(size=14)),
                    height=330, template="plotly_white", margin=dict(l=60, r=60, t=35, b=40),
                    xaxis=dict(type="log", title="frequency [Hz]"),
                    yaxis=dict(type="log", title="detuning [Hz/√Hz]", exponentformat="power"),
                    yaxis2=dict(type="log", title="RIN [1/√Hz]", overlaying="y", side="right",
                                exponentformat="power"),
                    legend=dict(orientation="h", y=1.12, x=1, xanchor="right"))
    return f


def loop_fig(lp):
    from plotly.subplots import make_subplots
    f = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
    if lp:
        x = lp["f"]
        f.add_scatter(x=x, y=lp["G_mag"], mode="lines+markers", name="|G| measured", row=1, col=1,
                      line=dict(color="#1f5fa8"))
        f.add_scatter(x=x, y=lp["M_mag"], mode="lines", name="|G| model", row=1, col=1,
                      line=dict(color="#1f5fa8", dash="dot"))
        if lp["P_mag"] is not None:
            pm = np.asarray(lp["P_mag"])
            f.add_scatter(x=x, y=(pm / pm[0]).tolist(), mode="lines", name="plant (normalized)", row=1, col=1,
                          line=dict(color="#d35400"))
        f.add_scatter(x=x, y=lp["G_ph"], mode="lines+markers", name="phase G", row=2, col=1,
                      line=dict(color="#1f5fa8"), showlegend=False)
        f.add_scatter(x=x, y=lp["M_ph"], mode="lines", name="phase model", row=2, col=1,
                      line=dict(color="#1f5fa8", dash="dot"), showlegend=False)
        if lp["P_ph"] is not None:
            f.add_scatter(x=x, y=lp["P_ph"], mode="lines", name="phase plant", row=2, col=1,
                          line=dict(color="#d35400"), showlegend=False)
        f.add_hline(y=1, line=dict(color="gray", width=1), row=1, col=1)
        f.add_hline(y=-180, line=dict(color="red", width=1, dash="dot"), row=2, col=1)
        if lp["ugf"]:
            f.add_vline(x=lp["ugf"], line=dict(color="#1e8e3e", dash="dash"))
    ttl = "Open-loop gain G" + (f": unity gain {lp['ugf']:.1f} Hz, phase margin {lp['pm']:.0f} deg"
                                 if lp and lp["ugf"] else "")
    f.update_layout(title=dict(text=ttl, font=dict(size=14)), height=420, template="plotly_white",
                    margin=dict(l=60, r=20, t=35, b=40), legend=dict(orientation="h", y=1.08, x=1, xanchor="right"))
    f.update_xaxes(type="log")
    f.update_xaxes(title_text="frequency [Hz]", row=2, col=1)
    f.update_yaxes(type="log", title_text="magnitude", row=1, col=1)
    f.update_yaxes(title_text="phase [deg]", row=2, col=1)
    return f
