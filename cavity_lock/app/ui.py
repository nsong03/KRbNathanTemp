"""
ui.py -- the browser UI (Dash): two views, "Cavity lock" and "Intensity", each a Moku-style schematic of its real
signal chain with its own settings, embedded scope and measurements. Two icons at the top left switch the view
(clientside only: no hardware access; the last view is remembered per browser in local storage). Each icon has a
status dot for its loop, so the other loop's state is visible from either view. The tick sends figures only for
the visible view.

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
import math
import time

import numpy as np

import schematic as SCH
import intensity_schematic as ISCH

PANEL_LABEL = {"drive": "probes A/B"}
SHORT = {"ac_hz": "Hz", "lpf_hz": "Hz", "lpf_order": "order", "err_gain": "gain ×", "setpoint_mv": "mV",
         "ugf_hz": "UGF Hz", "clamp_auto": "auto (mode spacing)", "phase_deg": "deg", "acq_dither_hz": "Hz",
         "acq_dither_mvpp": "mVpp", "ramp_hz": "Hz", "ramp_mvpp": "mVpp", "in1_scale": "×",
         "kappa_mhz": "κ MHz", "driver_bw_hz": "Hz",
         "ramp_center_mv": "setpoint mV", "int_filter_hz": "Hz", "int_filter_stages": "stages", "int_setpoint_mv": "mV", "int_gain_mode": "",
         "int_ugf_khz": "kHz", "int_p": "P", "int_i_hz": "I Hz", "int_clamp_lo_mv": "low mV", "int_dc_mv": "level mV",
         "int_clamp_hi_mv": "high mV", "int_mw_per_v": "mW/V", "int_pd_bw_mhz": "MHz",
         "int_aom_max_dbm": "max dBm", "int_aom_delay_us": "µs", "int_amp_gain_db": "dB", "int_if_full_ma": "mA",
         "int_rf_src_dbm": "dBm", "int_bias_v": "|V_bias| V"}
COLORS = {"A": "#e67e22", "B": "#8e44ad"}

CSS = """
body { margin: 0; font-family: sans-serif; }
.canvas-wrap { position: relative; z-index: 3; }   /* no overflow clip: popovers may extend over the scope */
.canvas { min-width: max-content; }
.canvas { position: relative; background: #fbfbfb; border: 1px solid #ddd; border-radius: 8px; margin: 4px 0 8px; }
.wires { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 1; }
.views { display: flex; gap: 6px; margin-right: 10px; }
.vicon { width: 74px; height: 56px; border: 1.5px solid #aaa; border-radius: 8px; background: #f4f4f4;
         cursor: pointer; position: relative; display: flex; flex-direction: column; align-items: center;
         justify-content: center; font-size: 11px; font-weight: bold; color: #555; user-select: none; }
.vicon svg { width: 40px; height: 26px; }
.vsvg p { margin: 0; } .vsvg { height: 28px; }
.vicon.active { border-color: #1f5fa8; background: #e8f0fb; color: #1f5fa8; box-shadow: 0 0 0 2px #9cc3f0; }
.vicon:hover { border-color: #1f5fa8; }
.vdot { position: absolute; top: 4px; right: 5px; width: 11px; height: 11px; border-radius: 50%%;
        border: 1px solid rgba(0,0,0,.35); background: #9e9e9e; }
.vdot.ok { background: #1e8e3e; } .vdot.busy { background: #2471a3; } .vdot.held { background: #e0a020; }
.vdot.fault { background: #d0302b; } .vdot.off { background: #9e9e9e; }
.spadj { display: flex; align-items: center; gap: 3px; margin-top: 2px; }
.spadj button { font-size: 10.5px; padding: 0 4px; cursor: pointer; }
.spslider { width: 236px; height: 22px; margin: 2px 0 0 0; position: relative; overflow: hidden; }
.ic .Select, .ic .dash-dropdown { font-size: 11px; }
.ddis { font-size: 11px; color: #999; border: 1px solid #ddd; border-radius: 3px; padding: 1px 4px; background: #f3f3f3;
        cursor: not-allowed; }
.w { fill: none; stroke-width: 2; }
.w.on.sig { stroke: #1f5fa8; } .w.on.lo { stroke: #7d3c98; } .w.on.drv { stroke: #d35400; }
.w.on.plant { stroke: #9a9a9a; } .w.on.mon { stroke: #16a085; }
.w.off { stroke: #c8c8c8; stroke-dasharray: 5 4; }
.blk { position: absolute; box-sizing: border-box; background: white; border: 1.5px solid #555; border-radius: 6px;
       z-index: 2; font-size: 12px; padding: 0 6px 4px; overflow: hidden; }
.blk.plant { background: #eeeeee; border: 1.5px dashed #aaa; color: #777; }
.blk.plant .hdr, .blk.plant .rb { display: inline-block; margin-right: 6px; }
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
  var P = g.prefix || '';
  var host = document.getElementById(P + 'wires');
  if (!host) { return nu; }
  if (!host.firstChild) {
    var col = {sig: '#1f5fa8', lo: '#7d3c98', drv: '#d35400', plant: '#9a9a9a', mon: '#16a085', off: '#c8c8c8'};
    var s = '<svg xmlns="http://www.w3.org/2000/svg" width="' + g.W + '" height="' + g.H + '"><defs>';
    Object.keys(col).forEach(function(k) {
      s += '<marker id="' + P + 'ah-' + k + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" ' +
           'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="' + col[k] + '"/></marker>';
    });
    s += '</defs>';
    g.wires.forEach(function(w) {
      s += '<path id="' + P + 'wire-' + w.id + '" class="w ' + w.kind + ' off" d="M' +
           w.pts.map(function(p) { return p[0] + ',' + p[1]; }).join(' L') + '"/>';
    });
    host.innerHTML = s + '</svg>';
  }
  g.wires.forEach(function(w) {
    var el = document.getElementById(P + 'wire-' + w.id);
    if (!el) { return; }
    var on = !!d.wires[w.id];
    el.setAttribute('class', 'w ' + w.kind + (on ? ' on' : ' off'));
    el.setAttribute('marker-end', 'url(#' + P + 'ah-' + (on ? w.kind : 'off') + ')');
  });
  function esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  Object.keys(d.boxes).forEach(function(b) {
    var el = document.getElementById(P + 'rb-' + b);
    if (el) {
      el.innerHTML = d.boxes[b].map(function(l) {
        return '<div class="' + (l[1] || '') + '" title="' + esc(l[0]) + '">' + esc(l[0]) + '</div>'; }).join('');
    }
  });
  Object.keys(d.sw).forEach(function(k) {
    var st = d.sw[k], p = document.getElementById(P + 'swp-' + k), l = document.getElementById(P + 'swl-' + k);
    if (p) { p.className = 'pill' + (st.on ? ' on' : '') + (st.busy ? ' busy' : ''); }
    if (l) { l.textContent = st.label; }
  });
  Object.keys(d.probes).forEach(function(q) {
    var e = document.getElementById(P + 'pb-' + q);
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

# Intensity embedded scope: live traces (probes A/B; setpoint line on in2), and the
# history of the mean photodiode level and the drive.
ISCOPE_JS = """
function(fr) {
  var nu = window.dash_clientside.no_update;
  if (!fr) { return [nu, nu]; }
  var C = {A: '#e67e22', B: '#8e44ad', pd: '#222222', sp: '#1e8e3e', cl: '#c0392b', pk: '#d35400'};
  var data = [], shapes = [], ann = [], lay = {};
  if (fr.kind === 'time') {
    var rows = [['a', fr.a_name, C.A], ['b', fr.b_name, C.B]].filter(function(r) { return fr[r[0]]; });
    var n = rows.length, gap = 0.1, h = (1 - gap * (n - 1)) / Math.max(n, 1);
    rows.forEach(function(r, i) {
      var ya = i === 0 ? 'y' : 'y2', key = i === 0 ? 'yaxis' : 'yaxis2', top = 1 - i * (h + gap);
      lay[key] = {domain: [top - h, top], anchor: 'x', zeroline: false, title: {text: r[1] + (r[1] === 'in2' ? ' [V]' : ' [mV]')}};
      data.push({type: 'scatter', mode: 'lines', x: fr.t, y: fr[r[0]], name: (i ? 'B: ' : 'A: ') + r[1], yaxis: ya,
                 line: {color: r[2], width: 1}});
      if (r[1] === 'in2' && fr.sp !== null && fr.sp !== undefined) {
        shapes.push({type: 'line', xref: 'paper', x0: 0, x1: 1, yref: ya, y0: fr.sp, y1: fr.sp,
                     line: {color: C.sp, dash: 'dot', width: 1.2}});
      }
    });
    lay.xaxis = {title: {text: 'time [µs]  (live)'}, anchor: n > 1 ? 'y2' : 'y'};
    lay.height = 180 * Math.max(n, 1) + 70;
  }
  lay.shapes = shapes; lay.annotations = ann; lay.margin = {l: 60, r: 15, t: 30, b: 45};
  lay.template = 'plotly_white'; lay.uirevision = fr.uirev; lay.hovermode = 'x';
  lay.legend = {orientation: 'h', y: 1.04, x: 1, xanchor: 'right', yanchor: 'bottom'};
  lay.plot_bgcolor = 'white'; lay.paper_bgcolor = 'white';
  var scope = fr.kind ? {data: data, layout: lay} : nu;
  var hist = nu;
  if (fr.hist) {
    var H = fr.hist;
    var hs = (H.faults || []).map(function(u) {
      return {type: 'line', xref: 'x', x0: u, x1: u, yref: 'paper', y0: 0, y1: 1, line: {color: 'red', width: 1}}; });
    hist = {data: [
      {type: 'scatter', mode: 'lines', x: H.t, y: H.pd, name: 'photodiode mean [' + H.unit + ']', line: {color: C.pd, width: 1}},
      {type: 'scatter', mode: 'lines', x: H.t, y: H.drv, name: 'drive (pid2) [mV]', yaxis: 'y2', line: {color: C.pk, width: 1}}],
      layout: {title: {text: 'Intensity over time (red: watchdog holds)', font: {size: 14}}, height: 250,
               xaxis: {title: {text: 'time [s]'}}, yaxis: {title: {text: 'photodiode [' + H.unit + ']'}},
               yaxis2: {title: {text: 'drive [mV]'}, overlaying: 'y', side: 'right'}, shapes: hs,
               margin: {l: 60, r: 60, t: 35, b: 40}, template: 'plotly_white', uirevision: 'ihist',
               plot_bgcolor: 'white', paper_bgcolor: 'white', legend: {orientation: 'h', y: 1.14, x: 1, xanchor: 'right'}}};
  }
  return [scope, hist];
}
"""

# View switching: the icons set the view (stored in the browser); the view shows/hides the two containers.
VIEW_CLICK_JS = """
function(a, b) {
  var cc = window.dash_clientside.callback_context;
  if (!cc.triggered.length) { return window.dash_clientside.no_update; }
  return cc.triggered[0].prop_id.indexOf('vbtn-int') === 0 ? 'intensity' : 'cavity';
}
"""
VIEW_SHOW_JS = """
function(v) {
  var cav = v !== 'intensity';
  return [cav ? {} : {display: 'none'}, cav ? {display: 'none'} : {}, 'vicon' + (cav ? ' active' : ''),
          'vicon' + (cav ? '' : ' active')];
}
"""

# Setpoint box: Dash 4's debounced number input does not re-send a value it sent itself before, even after a callback
# changed the box (type 500, nudge to 510, type 500 again: nothing). On Enter / blur, read the typed number from the
# DOM and send it as a direct setpoint command.
DC_TYPED_JS = """
function(ns, nb) {
  var el = document.getElementById('s-int_dc_mv');
  if (!el) { return window.dash_clientside.no_update; }
  var v = parseFloat(el.value);
  return isNaN(v) ? window.dash_clientside.no_update : {v: v, t: Date.now()};
}
"""
RAMP_TYPED_JS = """
function(ns, nb) {
  var el = document.getElementById('s-ramp_center_mv');
  if (!el) { return window.dash_clientside.no_update; }
  var v = parseFloat(el.value);
  return isNaN(v) ? window.dash_clientside.no_update : {v: v, t: Date.now()};
}
"""
SP_TYPED_JS = """
function(ns, nb) {
  var el = document.getElementById('s-int_setpoint_mv');
  if (!el) { return window.dash_clientside.no_update; }
  var v = parseFloat(el.value);
  return isNaN(v) ? window.dash_clientside.no_update : {v: v, t: Date.now()};
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


CAV_ICON = ('<svg viewBox="0 0 40 26"><path d="M8 3 Q3 13 8 23" fill="none" stroke="currentColor" stroke-width="2.5"/>'
            '<path d="M32 3 Q37 13 32 23" fill="none" stroke="currentColor" stroke-width="2.5"/>'
            '<path d="M8 13 H32" stroke="#c0392b" stroke-width="1.6" stroke-dasharray="3 2"/></svg>')
INT_ICON = ('<svg viewBox="0 0 40 26"><rect x="13" y="6" width="12" height="14" rx="1.5" fill="none" '
            'stroke="currentColor" stroke-width="2"/><path d="M1 13 H13" stroke="#c0392b" stroke-width="2"/>'
            '<path d="M25 13 H39" stroke="#c0392b" stroke-width="1"/><path d="M25 12 L38 4" stroke="#c0392b" '
            'stroke-width="2.4"/><path d="M16 9 V17 M19 9 V17 M22 9 V17" stroke="currentColor" stroke-width="1"/></svg>')
CAV_DOT = {"locked": "ok", "acquire": "busy", "locking": "busy", "busy": "busy", "measuring": "busy",
           "held": "held", "off": "off", "connecting": "busy"}


def build_app(ctl, A):
    """A: the cavity_lock_app module (FIELDS, analysis helpers)."""
    from dash import Dash, dcc, html, Input, Output, State, ctx, no_update, ALL

    app = Dash(__name__, title="Red Pitaya locks", update_title=None)
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + CSS % {} + "</style>")
    FIELD = {f[0]: f for f in A.FIELDS}
    KEYS = A.KEYS
    WIDGET = {p["key"]: p["widget"] for M in (SCH, ISCH) for b in M.BLOCKS for p in b["params"] if p.get("widget")}
    it = ctl.ints

    def to_ui(key, val):
        kind = FIELD[key][4]
        return (["on"] if val else []) if kind == "check" else val

    def from_ui(key, val):
        kind, default, opt = FIELD[key][4], FIELD[key][2], FIELD[key][5]
        if kind == "check":
            return bool(val)
        if kind == "multi":
            return [p for p in opt["options"] if p in (val or [])]
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
            w = {"int_p": "86px", "int_i_hz": "86px", "int_filter_stages": "60px", "int_gain_mode": "104px"}.get(key)
            comp = dcc.Dropdown(id=cid, options=[{"label": f"{o:g}" if isinstance(o, float) else str(o), "value": o}
                                                 for o in opt["options"]], value=val,
                                clearable=False, searchable=False,
                                style={"width": w or ("120px" if short is None else "112px"), "fontSize": "12px"})
        if short is not None:
            return html.Span([html.Span(short), comp] if short else [comp], className="ic", title=label)
        return html.Div([html.Span(label, className="l"), comp], className="row")

    def popover(pid, title, body, style):
        return html.Div([html.Div([html.Span(title), html.Span("×", id={"type": "popx", "b": pid}, className="px")],
                                  className="ptitle")] + body,
                        id={"type": "pop", "b": pid}, className="pop", style=dict(style, display="none"))

    # ---- the schematics (one builder for both models; ids of the intensity view carry its prefix) ----
    def pre(M):
        return getattr(M, "PREFIX", "")

    def block(b, M):
        P = pre(M)
        pos = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
        hdr_id = {"type": "hdr", "b": b["id"]}
        if b["kind"] == "node":
            term = b["id"] in ("out1", "i_out2")
            return html.Div([html.Span(b["label"]), html.Div(id=f"{P}rb-{b['id']}", className="rb")],
                            id=hdr_id, className="blk node" + (" term" if term else ""), style=pos,
                            title="click for details")
        inline = [p["key"] for p in b["params"] if p["inline"]]
        kids = [html.Div(b["label"] + "  ⚙", id=hdr_id, className="hdr", title="click for all settings"),
                html.Div(id=f"{P}rb-{b['id']}", className="rb")]
        knobs = [control(k, SHORT.get(k, "")) for k in inline]
        if b["id"] == "phase":
            knobs.append(html.Button("Auto", id="btn-phase", title="Auto-optimize phase (4-phase fit at the target "
                                                                   "peak; needs Scan and Dither on)",
                                     style={"fontSize": "11px", "padding": "1px 6px", "cursor": "pointer"}))
        if b["id"] == "i_pid":
            knobs.append(html.Span([html.Span("D"), html.Span("off ▾", className="ddis", id="i-d-dropdown",
                                                              title="D: not in this FPGA build (PyRPL 0.9.9.1 has "
                                                                    "the derivative register commented out)")],
                                   className="ic"))
        if knobs:
            kids.append(html.Div(knobs, className="inl"))
        if b["id"] == "ramp":
            kids.append(html.Div([html.Button(lab, id={"type": "rnudge", "d": d}, title=f"ramp setpoint {d:+g} mV")
                                  for lab, d in (("−10", -10.0), ("−1", -1.0), ("+1", 1.0), ("+10", 10.0))],
                                 className="spadj", title="ramp setpoint (centre) steps"))
        if b["id"] == "i_dc":
            kids.append(html.Div([html.Button(lab, id={"type": "dcnudge", "d": d}, title=f"DC level {d:+g} mV")
                                  for lab, d in (("−10", -10.0), ("−1", -1.0), ("−0.1", -0.1), ("+0.1", 0.1),
                                                 ("+1", 1.0), ("+10", 10.0))],
                                 className="spadj", title="DC output level steps"))
        if b["id"] == "i_setpoint":
            kids.append(html.Div(dcc.Slider(id="int-sp-slider", min=0, max=1000, step=0.5,
                                            value=ctl.s["int_setpoint_mv"], updatemode="mouseup", marks=None,
                                            tooltip={"placement": "bottom", "always_visible": False}),
                                 className="spslider", title="setpoint slider [mV of IN2]"))
            kids.append(html.Div([html.Button(lab, id={"type": "nudge", "d": d}, title=f"setpoint {d:+g} mV")
                                  for lab, d in (("−10", -10.0), ("−1", -1.0), ("−0.1", -0.1), ("+0.1", 0.1),
                                                 ("+1", 1.0), ("+10", 10.0))], className="spadj"))
        return html.Div(kids, className=f"blk {b['kind']} b-{b['id']}", style=pos)

    def block_pop(b, M):
        rest = [p["key"] for p in b["params"] if not p["inline"]]
        body = [control(k) for k in rest]
        if b["opts"]:
            body.append(html.Div({"pid": "Lock stages (sequence, no register)",
                                  "limiter": "Watchdog (Auto-relock switch)",
                                  "i_pid": "Lock stages (no register)",
                                  "i_limiter": "Watchdog (Re-engage switch)"}.get(b["id"], "Options"), className="sub"))
            body += [control(k) for k in b["opts"]]
        regs = sorted({p["attr"] for p in b["params"] if p["attr"]})
        if regs:
            body.append(html.Div("Writes: " + ", ".join(regs), className="note"))
        body.append(html.Div(b["note"], className="note"))
        W, H = M.CANVAS_W, M.CANVAS_H
        left = max(4, min(b["x"], W - 320))
        st = {"left": left, "bottom": H - b["y"] + 4} if b["y"] > 300 else {"left": left, "top": b["y"] + b["h"] + 4}
        return popover(b["id"], b["label"], body, st)

    CAV_SW_TITLES = {"scan": "Scan: asg0 ramp into OUT1 (on while locked asks to unlock first)",
                     "dither": "Dither: iq0.output_direct (off while locked unlocks and holds first)",
                     "lock": "Lock: run the lock sequence (scan off, phase re-fit, zero crossing, park, "
                             "gain ramp); off = unlock and hold",
                     "output": "Output: OUT1 enable (off: ramp, dither and PID disconnected, OUT1 = 0 V)",
                     "relock": "Auto-relock: the watchdog re-sweeps and relocks after an unlock"}

    def switch(sw, M):
        P = pre(M)
        typ = "isw" if M is ISCH else "sw"
        title = (ISCH.SWITCH_TITLES if M is ISCH else CAV_SW_TITLES)[sw["id"]]
        return html.Div([html.Div(sw["label"], id=f"{P}swl-{sw['id']}", className="lab"),
                         html.Div(id=f"{P}swp-{sw['id']}", className="pill")],
                        id={"type": typ, "s": sw["id"]}, className="sw", style={"left": sw["x"], "top": sw["y"]},
                        title=title)

    def probe(p, M):
        P = pre(M)
        typ = "iprobe" if M is ISCH else "probe"
        pos = {"left": p["x"], "top": p["y"]}
        if p["signal"]:
            return html.Div(html.Span(id=f"{P}pb-{p['id']}", className="pdot"), id={"type": typ, "p": p["signal"]},
                            className="probe", style=pos,
                            title=f"probe {p['label']} (click to place on scope channel A/B, again to remove)")
        return html.Div(html.Span(id=f"{P}pb-{p['id']}", className="pdot grey", **{"data-grey": "1"}),
                        className="probe grey", style=pos, title="not probe-able: " + p["why"])

    def canvas(M, frame):
        P = pre(M)
        kids = [html.Div(id=f"{P}wires", className="wires"),
                html.Div(html.Span("external (display only)"), className="plantframe",
                         style={"left": frame[0], "top": frame[1], "width": frame[2], "height": frame[3]})]
        kids += [html.Div(t["text"], className="term", style={"left": t["x"], "top": t["y"]}) for t in M.TERMINALS]
        kids += [block(b, M) for b in M.BLOCKS]
        kids += [switch(s, M) for s in M.SWITCHES]
        kids += [probe(p, M) for p in M.PROBES]
        kids += [block_pop(b, M) for b in M.BLOCKS]
        return html.Div(html.Div(kids, id=f"{P}canvas", className="canvas",
                                 style={"width": M.CANVAS_W, "height": M.CANVAS_H}), className="canvas-wrap")

    def icon(vid, label, svg, dot_id):
        return html.Div([html.Div(id=dot_id, className="vdot"),
                         html.Div(className="vsvg", children=dcc.Markdown(svg, dangerously_allow_html=True,
                                                                         style={"lineHeight": 0})),
                         html.Span(label)], id=vid, className="vicon", n_clicks=0,
                        title=f"show the {label} view (the hardware is not touched)")

    # ---- page ----
    def serve_layout():
        measure_pop = popover("measure", "Measurement settings", [control(k) for k in SCH.HOMES["measure"]]
                              + [html.Div("Measure noise and Measure loop work while locked.", className="note")],
                              {"right": 0, "top": 38})
        scope_pop = popover("scope", "Scope settings", [control(k) for k in SCH.HOMES["scope"]]
                            + [html.Div("Probes A/B other than in1/iq0 take a second capture every N traces.",
                                        className="note")], {"left": 0, "top": 34})
        imeasure_pop = popover("i_measure", "Intensity Bode settings", [control(k) for k in ISCH.HOMES["i_measure"]]
                               + [html.Div("Works while locked: a small sine from asg1 on OUT2 (summed with pid2), "
                                           "recorded with pid2 and in2.", className="note")],
                               {"left": 0, "top": 38})
        iscope_pop = popover("i_scope", "Intensity scope settings", [control(k) for k in ISCH.HOMES["i_scope"]]
                             + [html.Div("Probes A/B other than in2/pid2 take a second capture every N traces "
                                         "(cavity scope setting).", className="note")], {"left": 0, "top": 34})
        cavity = html.Div([
            html.Div([
                html.H3("Cavity PZT dither lock", style={"margin": "4px 12px 4px 0"}),
                html.Button("Measure noise", id="btn-noise", className="btn"),
                html.Button("Measure loop", id="btn-loop", className="btn"),
                html.Button("Measure Bode plot", id="btn-bode", className="btn",
                            title="swept sine from the internal generator (asg0), recorded with the scope, point by "
                                  "point. closed loop: while locked, a small sine at OUT1. circuit only: spare copies "
                                  "of the lock-in and PID (iq1, pid1, outputs off), OUT1 untouched"),
                control("bode_mode", ""),
                html.Button("Stop", id="btn-stop", className="btn", title="stop the running Bode sweep"),
                html.Span("⚙", id={"type": "hdr", "b": "measure"}, className="gear", title="measurement settings"),
                measure_pop,
            ], className="bar"),
            canvas(SCH, (10, 4, 812, 76)),
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
            html.Div(id="analysis", style={"fontSize": "13px", "whiteSpace": "pre-wrap"}),
        ], id="view-cavity")
        intensity = html.Div([
            html.Div([
                html.H3("AOM intensity servo", style={"margin": "4px 12px 4px 0"}),
                html.Button("Measure Bode plot", id="btn-ibode", className="btn",
                            title="while locked: swept sine from asg1 on OUT2, recorded with pid2 and in2, point by "
                                  "point (G, disturbance suppression, closed loop, plant)"),
                html.Button("Stop", id="btn-istop", className="btn", title="stop the running Bode sweep"),
                html.Span("⚙", id={"type": "hdr", "b": "i_measure"}, className="gear", title="Bode settings"),
                imeasure_pop,
            ], className="bar"),
            canvas(ISCH, (10, 4, 1262, 160)),
            html.Div([
                html.B("Embedded scope"),
                html.Span("Probe A", style={"color": COLORS["A"], "fontWeight": "bold"}), control("int_probe_a", ""),
                html.Span("Probe B", style={"color": COLORS["B"], "fontWeight": "bold"}), control("int_probe_b", ""),
                control("int_panels"),
                html.Span("⚙ scope", id={"type": "hdr", "b": "i_scope"}, className="gear"),
                iscope_pop,
            ], className="bar", style={"marginBottom": "4px"}),
            dcc.Graph(id="iscope", config={"displaylogo": False}),
            dcc.Graph(id="ihistory", config={"displaylogo": False}),
            dcc.Graph(id="ibodefig", config={"displaylogo": False}, style={"display": "none"}),
            html.Div(id="ianalysis", style={"fontSize": "13px", "whiteSpace": "pre-wrap"}),
        ], id="view-intensity", style={"display": "none"})
        return html.Div([
            html.Div([
                html.Div([icon("vbtn-cav", "Cavity lock", CAV_ICON, "dot-cav"),
                          icon("vbtn-int", "Intensity", INT_ICON, "dot-int")], className="views"),
                html.Div(id="status", style={"fontSize": "16px", "fontWeight": "bold", "flex": "1"}),
                html.Button("Save settings", id="btn-save", className="btn"),
                html.Button("Load settings", id="btn-load", className="btn"),
            ], className="bar", style={"marginBottom": "4px"}),
            cavity,
            intensity,
            html.Pre(id="log", style={"fontSize": "12px", "height": "26vh", "overflowY": "auto",
                                      "whiteSpace": "pre-wrap", "background": "#f6f6f6", "padding": "6px",
                                      "margin": "6px 0 0"}),
            dcc.ConfirmDialog(id="confirm-scan", message="The cavity is locked. Turning Scan on unlocks it "
                                                         "(the setpoint is dropped and the ramp starts). Continue?"),
            dcc.Interval(id="tick", interval=int(1000 / max(ctl.s.get("ui_hz", 10), 1))),
            dcc.Store(id="view", storage_type="local", data="cavity"),
            dcc.Store(id="view-ack"),
            dcc.Store(id="synced", data=-1),                  # -1: the first tick pushes all present values
            dcc.Store(id="push-version"),
            dcc.Store(id="sp-typed"),
            dcc.Store(id="ramp-typed"),
            dcc.Store(id="dc-typed"),
            dcc.Store(id="frame"),
            dcc.Store(id="frame-seq", data=-1),
            dcc.Store(id="meas-seq", data=-1),
            dcc.Store(id="bode"),
            dcc.Store(id="bode-seq", data=-1),
            dcc.Store(id="diag"),
            dcc.Store(id="diag-key", data=""),
            dcc.Store(id="diag-ack"),
            dcc.Store(id="geom", data=SCH.geometry()),
            dcc.Store(id="iframe"),
            dcc.Store(id="iframe-seq", data=-1),
            dcc.Store(id="idiag"),
            dcc.Store(id="idiag-key", data=""),
            dcc.Store(id="idiag-ack"),
            dcc.Store(id="igeom", data=ISCH.geometry()),
            dcc.Store(id="ibode"),
            dcc.Store(id="ibode-seq", data=-1),
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
               "btn-noise": "noise", "btn-loop": "loop", "btn-bode": "bode", "btn-ibode": "int_bode"}

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("btn-stop", "n_clicks"),
                  Input("btn-istop", "n_clicks"), prevent_initial_call=True)
    def on_stop(*_):
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

    @app.callback(Output("ack", "data", allow_duplicate=True),
                  Input({"type": "isw", "s": ALL}, "n_clicks"), prevent_initial_call=True)
    def on_iswitch(_):
        t = ctx.triggered_id
        if not t or not ctx.triggered or not ctx.triggered[0].get("value"):
            return no_update
        name = t["s"]
        on = not ISCH.switch_states(it.mode, it.hw, ctl.s, it.dc_on)[name]
        ctl.submit("int_switch", (name, on))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "probe", "p": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_probe(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("probe", t["p"])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "iprobe", "p": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_iprobe(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("int_probe", t["p"])
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "nudge", "d": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_nudge(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("int_nudge", float(t["d"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("int-sp-slider", "value"),
                  prevent_initial_call=True)
    def on_slider(v):
        if v is not None and abs(float(v) - ctl.s["int_setpoint_mv"]) > 1e-6:
            ctl.submit("int_setpoint", float(v))
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

    app.clientside_callback(SP_TYPED_JS, Output("sp-typed", "data"), Input("s-int_setpoint_mv", "n_submit"),
                            Input("s-int_setpoint_mv", "n_blur"), prevent_initial_call=True)

    app.clientside_callback(RAMP_TYPED_JS, Output("ramp-typed", "data"), Input("s-ramp_center_mv", "n_submit"),
                            Input("s-ramp_center_mv", "n_blur"), prevent_initial_call=True)

    app.clientside_callback(DC_TYPED_JS, Output("dc-typed", "data"), Input("s-int_dc_mv", "n_submit"),
                            Input("s-int_dc_mv", "n_blur"), prevent_initial_call=True)

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("dc-typed", "data"), prevent_initial_call=True)
    def on_dc_typed(d):
        if d and abs(float(d["v"]) - ctl.s["int_dc_mv"]) > 1e-9:
            ctl.submit("int_dc", float(d["v"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "dcnudge", "d": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_dcnudge(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("int_dc_nudge", float(t["d"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("ramp-typed", "data"), prevent_initial_call=True)
    def on_ramp_typed(d):
        if d and abs(float(d["v"]) - ctl.s["ramp_center_mv"]) > 1e-9:
            ctl.submit("ramp_center", float(d["v"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input({"type": "rnudge", "d": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def on_rnudge(_):
        t = ctx.triggered_id
        if t and ctx.triggered and ctx.triggered[0].get("value"):
            ctl.submit("ramp_nudge", float(t["d"]))
        return time.time()

    @app.callback(Output("ack", "data", allow_duplicate=True), Input("sp-typed", "data"), prevent_initial_call=True)
    def on_sp_typed(d):
        if d and abs(float(d["v"]) - ctl.s["int_setpoint_mv"]) > 1e-9:
            ctl.submit("int_setpoint", float(d["v"]))
        return time.time()

    app.clientside_callback(VIEW_CLICK_JS, Output("view", "data"), Input("vbtn-cav", "n_clicks"),
                            Input("vbtn-int", "n_clicks"), prevent_initial_call=True)
    app.clientside_callback(VIEW_SHOW_JS, [Output("view-cavity", "style"), Output("view-intensity", "style"),
                                           Output("vbtn-cav", "className"), Output("vbtn-int", "className")],
                            Input("view", "data"))
    app.clientside_callback(POP_JS, Output({"type": "pop", "b": ALL}, "style"),
                            Input({"type": "hdr", "b": ALL}, "n_clicks"), Input({"type": "popx", "b": ALL}, "n_clicks"),
                            State({"type": "pop", "b": ALL}, "style"), prevent_initial_call=True)
    app.clientside_callback(SCOPE_JS, [Output("scope", "figure"), Output("history", "figure")], Input("frame", "data"))
    app.clientside_callback(ISCOPE_JS, [Output("iscope", "figure"), Output("ihistory", "figure")],
                            Input("iframe", "data"))
    app.clientside_callback(DIAG_JS, Output("diag-ack", "data"), Input("diag", "data"), State("geom", "data"))
    app.clientside_callback(DIAG_JS, Output("idiag-ack", "data"), Input("idiag", "data"), State("igeom", "data"))
    app.clientside_callback(BODE_JS, Output("bodefig", "figure"), Input("bode", "data"))
    app.clientside_callback(BODE_JS, Output("ibodefig", "figure"), Input("ibode", "data"))

    # ---- the tick: compact data only, for the visible view ----
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

    def iframe_data(snap):
        s, tr, pr = snap["settings"], snap["traces"], snap["probe"]
        sp = s["int_setpoint_mv"] * 1e-3
        fr = dict(kind=None, sp=sp)
        if "traces" in s["int_panels"] and tr is not None:
            if pr is not None:
                fr.update(kind="time", uirev=f"live-{pr['a_name']}-{pr['b_name']}", a_name=pr["a_name"],
                          b_name=pr["b_name"])
                t_us = pr["t"] * 1e6
                for ch in ("a", "b"):
                    y = pr.get(ch)
                    nm = pr[ch + "_name"]
                    if y is not None and nm != "off":
                        x, yy = A.envelope(t_us, y if nm == "in2" else y * 1e3)
                        fr["t"], fr[ch] = r5(x), r5(yy)
        if "history" in s["int_panels"]:
            h = snap["history"][-6000:]
            k = s["int_mw_per_v"] if s.get("int_mw_per_v") else 1.0
            t, pdv, drv, faults = [], [], [], []
            if h:
                t0 = h[0][0]
                step = max(1, len(h) // 1500)
                last = None
                for j in range(0, len(h), step):
                    x = h[j]
                    tt = x[0] - t0
                    if last is not None and tt - last > 1.5:
                        t.append(None); pdv.append(None); drv.append(None)
                    t.append(round(tt, 3)); pdv.append(round(x[1] * k, 5)); drv.append(round(x[2] * 1e3, 4))
                    last = tt
                faults = [round(x[0] - t0, 3) for x in h if x[3] in ("railed", "nolight")]
            fr["hist"] = dict(t=t, pd=pdv, drv=drv, faults=faults, unit="mW" if s.get("int_mw_per_v") else "V")
        return fr

    def cav_status(snap):
        mode, li = snap["mode"], snap["lock"]
        f_d, d_mvpp = snap["dither"]
        dm = f" (measured {snap['dither_meas']:.1f})" if snap["dither_meas"] else ""
        color = {"locked": "#1e8e3e", "acquire": "#2471a3", "held": "#b9770e", "busy": "#7d3c98",
                 "locking": "#7d3c98", "measuring": "#7d3c98", "off": "#c0392b"}.get(mode, "#555")
        label = {"acquire": "SCANNING", "off": "OUTPUT OFF"}.get(mode, mode.upper())
        dith = f"dither {d_mvpp:g} mVpp{dm} @ {f_d:g} Hz" if snap["dither_on"] else "dither OFF"
        if mode in ("locked", "measuring") and li.get("setpoint") is not None:
            extra = (f"   setpoint {li['setpoint']*1e3:+.3f} mV   transmission {li.get('frac', 0)*100:.0f}% of peak   "
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
        return html.Span([html.Span("Cavity: " + label, style={"color": color}),
                          html.Span(extra, style={"fontSize": "13px"})])

    def int_status(snap):
        label, color, _ = it.status()
        s, li = snap["settings"], snap["lock"]
        mw = (lambda v: f" ({v*s['int_mw_per_v']:.3g} mW)") if s.get("int_mw_per_v") else (lambda v: "")
        extra = ""
        if snap["pd"] is not None:
            extra += f"   photodiode {snap['pd']*1e3:.1f} mV{mw(snap['pd'])}"
        extra += f"   setpoint {s['int_setpoint_mv']:g} mV"
        if snap["mode"] in ("locked", "measuring") and li.get("ugf_actual"):
            extra += f"   |i·K| {li['ugf_actual']/1e3:.1f} kHz"
        if snap["drive"] is not None and snap["mode"] != "scanning":
            extra += f"   drive {snap['drive']*1e3:+.2f} mV"
        if snap["mode"] == "measuring" and snap["meas_progress"]:
            extra = "   " + snap["meas_progress"]
        elif snap["fps"]:
            extra += f"   {snap['fps']:.0f} frames/s"
        return html.Span([html.Span("Intensity: " + label, style={"color": color}),
                          html.Span(extra, style={"fontSize": "13px"})])

    def int_lines(snap):
        s, li, pl = snap["settings"], snap["lock"], snap["plant"] or {}
        L = []
        if pl.get("K"):
            L.append(f"plant gain K = dV_IN2/dV_OUT2 = {pl['K']:+.3f} V/V, measured "
                     f"{time.strftime('%H:%M:%S', time.localtime(pl['t']))} by a {'closed' if pl.get('closed') else 'open'}"
                     f"-loop Bode plot at OUT2 {pl.get('u0', 0)*1e3:+.1f} mV (photodiode {(pl.get('pd0') or 0)*1e3:.0f} mV);"
                     f" integrator cap 38.9 kHz × |K| = {ISCH.I_MAX*abs(pl['K'])/1e3:.0f} kHz")
        else:
            L.append("K not measured yet: with Output on and the loop open, press Measure Bode plot (open-loop plant); "
                     "manual P / I gains also lock with the plant-sign setting")
        if li.get("K") and snap["mode"] in ("locked", "measuring", "railed", "nolight", "held"):
            if li.get("i_hw") is not None:
                L.append(f"gains ({s['int_gain_mode']}): asked p {li.get('p_req', 0):+.4g}, i {li.get('i_req', 0):+.4g} "
                         f"Hz -> hardware p {li['p_hw']:+.4g}, i {li['i_hw']:+.4g} Hz"
                         + (f"; |i·K| {abs(li['i_hw']*li['K'])/1e3:.2f} kHz" if li.get("K_measured") else ""))
            if li.get("adopted"):
                L.append("this servo was running at start-up and was adopted as is")
        if snap["pm"] and snap["pm"][0]:
            fu, pm = snap["pm"]
            L.append(f"model: unity gain {fu/1e3:.0f} kHz, phase margin {pm:.0f} deg (PI + delay "
                     f"{(ISCH.RP_DELAY*1e6 + s['int_aom_delay_us']):.2f} µs + photodiode {s['int_pd_bw_mhz']:g} MHz)"
                     + ("  WARN under 45 deg" if pm < 45 else ""))
        return "\n".join(L)

    tick_outputs = ([Output("frame", "data"), Output("frame-seq", "data"), Output("status", "children"),
                     Output("target-text", "children"), Output("analysis", "children"), Output("log", "children"),
                     Output("noise", "style"), Output("loopfig", "style"),
                     Output("history", "style"), Output("meas-seq", "data"), Output("tick", "interval"),
                     Output("diag", "data"), Output("diag-key", "data"), Output("bode", "data"),
                     Output("bode-seq", "data"), Output("bodefig", "style"),
                     Output("iframe", "data"), Output("iframe-seq", "data"), Output("idiag", "data"),
                     Output("idiag-key", "data"), Output("ibode", "data"), Output("ibode-seq", "data"),
                     Output("ianalysis", "children"),
                     Output("iscope", "style"), Output("ihistory", "style"),
                     Output("ibodefig", "style"), Output("dot-cav", "className"), Output("dot-int", "className"),
                     Output("push-version", "data")])
    N_CAV = 16

    @app.callback(tick_outputs, Input("tick", "n_intervals"), State("synced", "data"), State("frame-seq", "data"),
                  State("meas-seq", "data"), State("diag-key", "data"), State("bode-seq", "data"),
                  State("view", "data"), State("iframe-seq", "data"), State("idiag-key", "data"),
                  State("ibode-seq", "data"), State("dot-cav", "className"),
                  State("dot-int", "className"), State("push-version", "data"))
    def on_tick(_, synced, frame_seq, meas_seq, diag_key, bode_seq, view, iframe_seq, idiag_key, ibode_seq,
                dot_cav, dot_int, pushed):
        view = "intensity" if view == "intensity" else "cavity"
        if ctl.view != view:
            ctl.view = view                       # scope-schedule hint only: no hardware access
        snap = ctl.snapshot()
        s = snap["settings"]
        version_new = snap["version"] != synced
        logs = "\n".join(reversed(snap["logs"][-80:]))
        dots = ["vdot " + CAV_DOT.get(snap["mode"], "off"), "vdot " + it.status()[2]]
        dots = [d if d != old else no_update for d, old in zip(dots, (dot_cav, dot_int))]   # only on change
        cav = [no_update] * N_CAV
        ints = [no_update] * 10
        if view == "cavity":
            mode, an, li = snap["mode"], snap["analysis"] or {}, snap["lock"]
            new_frame = snap["seq"] != frame_seq or version_new
            frame = frame_data(snap) if new_frame else no_update
            if an.get("target") is not None and mode in ("acquire", "busy"):
                p = an["peaks"][an["target"]]
                ttxt = (f"{'Chosen' if s.get('target_mv') is not None else 'Tallest'} peak at {p['v']*1e3:+.1f} mV, "
                        f"height {p['height']:.3f} V" + (f", lock point {an['v0']*1e3:+.2f} mV"
                                                         if an.get("v0") is not None else ""))
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
                                 + (f", nearest other peak {an['mode_spacing']*1e3:.1f} mV away"
                                    if an.get("mode_spacing") else ""))
                lines += an.get("notes", [])
            elif li.get("v0") is not None:
                lines.append(f"lock point {li['v0']*1e3:+.3f} mV, slope {li['slope']:+.1f} /V, clamp "
                             f"{li['clamp'][0]*1e3:+.1f}..{li['clamp'][1]*1e3:+.1f} mV, relocks {li.get('relocks', 0)}")
                if li.get("err_rms") is not None:
                    lines.append(f"error rms {li['err_rms']*1e3:.2f} mV  ->  about "
                                 f"{abs(li['err_rms']/li['slope'])*1e6:.1f} uV of drive "
                                 f"({abs(li['err_rms']/li['slope'])/li['fwhm']*100:.2f}% of the swept FWHM)")
            lp_list = [s["lpf_hz"]] * int(s["lpf_order"])
            pm = A.predicted_margin(s, lp_list)
            fu, pmd = pm
            if fu:
                lines.append(f"model: unity gain {fu:.0f} Hz, phase margin {pmd:.0f} deg (integrator + error low-pass "
                             f"{' + '.join(f'{x:.0f} Hz' for x in lp_list)} + driver {s['driver_bw_hz']:g} Hz + 0.2 us)")
                if pmd < 45:
                    lines.append("WARN phase margin under 45 deg: lower the loop bandwidth or raise the "
                                 "low-pass/dither frequency")
            nz = snap["noise"]
            if nz:
                lines.append("noise (linewidths rms | intensity rms) by band: " + ", ".join(
                    f"{r['band']}: {r['det']*100 if r['det'] is not None else float('nan'):.2f}% | {r['rin']*100:.2f}%"
                    for r in nz["rows"]))
            meas_new = snap["meas_seq"] != meas_seq
            diag = SCH.diagram_state(snap, pm)
            key = str(hash(json.dumps(diag, sort_keys=True)))
            diag_out, key_out = (diag, key) if key != diag_key else (no_update, no_update)
            cav = [frame, snap["seq"], cav_status(snap), ttxt, "\n".join(lines), logs,
                   {} if "noise spectrum" in s["panels"] else {"display": "none"},
                   {} if "loop (Bode)" in s["panels"] else {"display": "none"},
                   {} if "setpoint history" in s["panels"] else {"display": "none"},
                   snap["meas_seq"] if meas_new else no_update, no_update, diag_out, key_out,
                   A.bode_json(snap["bode"]) if snap["bode_seq"] != bode_seq else no_update,
                   snap["bode_seq"] if snap["bode_seq"] != bode_seq else no_update,
                   {} if "Bode (swept)" in s["panels"] else {"display": "none"}]
        else:
            isnap = it.snapshot()
            new_frame = isnap["seq"] != iframe_seq or version_new
            idiag = ISCH.diagram_state(isnap)
            key = str(hash(json.dumps(idiag, sort_keys=True)))
            ip = isnap["settings"]["int_panels"]
            ints = [iframe_data(isnap) if new_frame else no_update, isnap["seq"],
                    idiag if key != idiag_key else no_update, key if key != idiag_key else no_update,
                    A.bode_json(isnap["bode"]) if isnap["bode_seq"] != ibode_seq else no_update,
                    isnap["bode_seq"] if isnap["bode_seq"] != ibode_seq else no_update,
                    int_lines(isnap),
                    {} if "traces" in ip else {"display": "none"},
                    {} if "history" in ip else {"display": "none"},
                    {} if "Bode (swept)" in ip else {"display": "none"}]
            cav[2] = int_status(isnap)
            cav[5] = logs
        if version_new:
            cav[10] = int(1000 / max(float(s["ui_hz"]), 1))
        return cav + ints + dots + [snap["version"] if version_new and pushed != snap["version"] else no_update]

    # Settings push: only when the controller changed settings (version). Kept out of the fast tick, whose
    # output list would otherwise carry every settings input of both views.
    @app.callback([Output("int-sp-slider", "value"), Output("int-sp-slider", "max")]
                  + [Output(f"s-{k}", "value") for k in KEYS] + [Output("synced", "data")],
                  Input("push-version", "data"), prevent_initial_call=True)
    def on_push(v):
        s = dict(ctl.s)
        smax = math.ceil(max(1000.0, s["int_setpoint_mv"]) / 10) * 10
        return [s["int_setpoint_mv"], smax] + [to_ui(k, s[k]) for k in KEYS] + [v]

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
