"""
pyrpl_spinbox.py -- P and I boxes that behave like PyRPL's own PID widget.

PyRPL 0.9.9.1 (widgets/module_widgets/pid_widget.py) calls set_log_increment() on the "p" and "i" spinboxes
(widgets/spinbox.py, NumberSpinBox). Copied here:
  * buttons "↓" box "↑"; the value is shown as "%.4e" (FloatSpinBox, decimals = 4);
  * a click (or the Up / Down key) steps once by the register increment (pid.p: 2^-12; pid.i:
    1 / (2^32 * 2 pi * 8 ns) = 0.0046 Hz), stopping at exactly 0 when it would cross zero;
  * held for more than 0.1 s (change_initial_latency) the value changes logarithmically: start value x
    2^(t / halflife) up, / down, halflife 0.5 s, refreshed every 20 ms (change_interval) when it moved by more than
    one increment; going down below half an increment it stops at 0 (and starts from 0 again);
  * Page Up / Page Down halve / double the halflife (tuning speed), like PyRPL;
  * the result is saturated to the register range (pid.p: +-2048; pid.i: +-38.9 kHz).
Nothing is converted: the number in the box is what is written to pid.p / pid.i.

Dash: the stepping runs in the browser (script()). Like PyRPL's FloatSpinBox, the full-precision value is kept
(cached per box) and only the text shows "%.4e"; a 50 ms clientside interval sends the newest full-precision value
to dcc.Store("pyspin") -> the app's "spin" command -> pid.p / pid.i (at most 20 writes per second while a button is
held, and once more with the final value). Typing a number + Enter goes through the normal settings callback.
"""
import math

P_STEP = 2.0 ** -12
I_STEP = 1.0 / (2 ** 32 * 2 * math.pi * 8e-9)
P_MAX = (2 ** 23 - 1) * P_STEP
I_MAX = (2 ** 23 - 1) * I_STEP
SPIN = {"p": (P_STEP, P_MAX), "i": (I_STEP, I_MAX), "int_p": (P_STEP, P_MAX), "int_i": (I_STEP, I_MAX)}

CSS = """
.pyspin { display: inline-flex; align-items: center; gap: 0; }
.pyspin > span:first-child { margin-right: 3px; }
.pyspin button.pyspin-btn { width: 17px; height: 20px; padding: 0; font-size: 12px; line-height: 18px;
                            cursor: pointer; user-select: none; }
.pyspin .dash-input-container { width: 92px !important; min-width: 92px !important; }
.pyspin input.pyspin-line, .pyspin .pyspin-line input { width: 92px; font-family: monospace; font-size: 11px;
                                                       padding: 1px 2px; box-sizing: border-box; }
"""


def fmt(v):
    """PyRPL FloatSpinBox text: '{:.4e}'."""
    try:
        return "{:.4e}".format(float(v))
    except (TypeError, ValueError):
        return v


def script():
    steps = ", ".join(f'"{k}": [{s!r}, {m!r}]' for k, (s, m) in SPIN.items())
    return """
<script>
(function() {
  var SPIN = {%s};
  var LATENCY = 0.1, INTERVAL = 20;
  var half = {};                       // per box: halflife [s] (PyRPL default 0.5, Page Up / Down change it)
  var st = null;                       // the step in progress
  var cache = {};                      // full-precision value per box (the text shows only 4 decimals)
  window.__pyspin_out = {};
  function fmt(v) {                    // Python '{:.4e}'
    var s = Number(v).toExponential(4), m = s.match(/^(-?[0-9.]+)e([+-])([0-9]+)$/);
    return m ? m[1] + 'e' + m[2] + (m[3].length < 2 ? '0' + m[3] : m[3]) : s;
  }
  function el(key) { return document.getElementById('s-' + key); }
  function cur(key) {                  // FloatSpinBox.val: the cached value unless the text was edited
    var e = el(key), t = e ? e.value : '';
    if (key in cache && fmt(cache[key]) === t) { return cache[key]; }
    var v = parseFloat(t);
    return isNaN(v) ? 0 : v;
  }
  function sat(key, v) { var m = SPIN[key][1]; return Math.max(-m, Math.min(m, v)); }
  function emit(key, v) {
    var e = el(key);
    cache[key] = v;
    if (e) { e.value = fmt(v); }
    window.__pyspin_out[key] = v;
    st && (st.val = v);
  }
  function first(key, dir) {           // NumberSpinBox.first_step
    var step = SPIN[key][0], v0 = cur(key), v = v0 + step * dir;
    if (Math.sign(v) * Math.sign(v0) < 0) { v = 0; }
    v = sat(key, v);
    st = {key: key, dir: dir, t0: Date.now() / 1000, v0: v0, val: v};
    emit(key, v);
    if (st.v0 === 0) { st.v0 = v; }    // log mode: avoid a zero start value
  }
  function cont() {                    // NumberSpinBox.continue_step (log increment)
    if (!st) { return; }
    var key = st.key, step = SPIN[key][0], hl = half[key] || 0.5;
    var dt = Date.now() / 1000 - st.t0;
    if (dt <= LATENCY) { return; }
    if (st.v0 === 0) { var d = st.dir; first(key, d); return; }
    var sign = st.dir * Math.sign(st.v0), v = st.v0 * Math.pow(2, dt / hl * sign);
    if (Math.abs(v) <= step / 2 && sign < 0) { st.v0 = 0; v = 0; st.t0 = Date.now() / 1000; }
    if (Math.abs(st.val - v) > step) { emit(key, sat(key, v)); }
  }
  function start(key, dir) { stop(); first(key, dir); st.timer = setInterval(cont, INTERVAL); }
  function stop() { if (st && st.timer) { clearInterval(st.timer); } st = null; }
  document.addEventListener('mousedown', function(ev) {
    var b = ev.target.closest && ev.target.closest('.pyspin-btn');
    if (!b) { return; }
    ev.preventDefault();
    start(b.getAttribute('data-key'), parseInt(b.getAttribute('data-dir'), 10));
  });
  document.addEventListener('mouseup', stop);
  document.addEventListener('keydown', function(ev) {
    var e = ev.target;
    if (!e || !e.id || e.id.indexOf('s-') !== 0) { return; }
    var key = e.id.slice(2);
    if (!(key in SPIN)) { return; }
    if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown' || ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault();
      if (!ev.repeat) { start(key, (ev.key === 'ArrowUp' || ev.key === 'ArrowRight') ? 1 : -1); }
    } else if (ev.key === 'PageUp' || ev.key === 'PageDown') {
      ev.preventDefault();
      half[key] = (half[key] || 0.5) / (ev.key === 'PageUp' ? 2 : 0.5);
    }
  });
  document.addEventListener('keyup', function(ev) {
    if (['ArrowUp', 'ArrowDown', 'ArrowRight', 'ArrowLeft'].indexOf(ev.key) >= 0) { stop(); }
  });
})();
</script>
""" % steps


# clientside: hand the newest stepped (full-precision) values to dcc.Store("pyspin")
OUT_JS = """
function(n) {
  var o = window.__pyspin_out || {}, keys = Object.keys(o);
  if (!keys.length) { return window.dash_clientside.no_update; }
  window.__pyspin_out = {};
  return {v: o, t: Date.now()};
}
"""


def install(app, ctl):
    """Inject the script and CSS; the interval -> Store("pyspin") -> ctl "spin" command."""
    from dash import Input, Output
    app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + CSS + "</style>")
    app.index_string = app.index_string.replace("</body>", script() + "</body>")
    app.clientside_callback(OUT_JS, Output("pyspin", "data"), Input("pyspin-tick", "n_intervals"),
                            prevent_initial_call=True)

    @app.callback(Output("pyspin-ack", "data"), Input("pyspin", "data"), prevent_initial_call=True)
    def on_spin(d):
        for k, v in ((d or {}).get("v") or {}).items():
            if k in SPIN:
                ctl.submit("spin", (k, float(v)))
        return d and d.get("t")


def parse(text, current):
    """Box text -> value. The text shows only 4 decimals: if it still reads as the present value, keep the present
    full-precision value (PyRPL FloatSpinBox.val does the same)."""
    if text is None or text == "":
        return current
    if isinstance(text, str) and current is not None and fmt(current) == text.strip():
        return current
    return float(text)


def component(key, value, short, label):
    """↓ [%.4e] ↑ like PyRPL's PID widget. The box keeps the id s-{key} (the app's settings callback reads it)."""
    from dash import dcc, html
    btn = {"className": "pyspin-btn", "n_clicks": 0}
    return html.Span([html.Span(short),
                      html.Button("↓", title=f"{label}: click = one register step down, hold = halve every 0.5 s",
                                  **{"data-key": key, "data-dir": "-1"}, **btn),
                      dcc.Input(id=f"s-{key}", type="text", value=fmt(value), debounce=True,
                                className="pyspin-line",
                                style={"width": "92px", "minWidth": "92px", "fontFamily": "monospace",
                                       "fontSize": "11px"}),
                      html.Button("↑", title=f"{label}: click = one register step up, hold = double every 0.5 s",
                                  **{"data-key": key, "data-dir": "1"}, **btn)],
                     className="ic pyspin",
                     title=f"{label}. Up/Down keys in the box step like the buttons, Page Up/Down change the speed.")


def interval():
    from dash import dcc, html
    return html.Div([dcc.Interval(id="pyspin-tick", interval=50), dcc.Store(id="pyspin"), dcc.Store(id="pyspin-ack")])
