"""Builds cavity_lock.ipynb from the cell sources below."""
import sys
import nbformat as nbf

cells = []
md = lambda s: cells.append(nbf.v4.new_markdown_cell(s.strip("\n")))
code = lambda s: cells.append(nbf.v4.new_code_cell(s.strip("\n")))

md(r"""
# Cavity PZT dither lock

The Red Pitaya reads the SR810 error signal X on **IN1** and drives the cavity PZT driver from **OUT1**. It adds the DG922's 1 kHz dither (arriving on **IN2**) to its output, so no summer is needed.

Run the cells top to bottom: **Setup**, then **Part 1: Calibration**, then **Part 2: Locking**.

| Signal | From | To |
|---|---|---|
| Dither, 1 kHz sine, 40 mVpp | DG922 CH1 | Red Pitaya IN2 (LV jumper) |
| Reference, 1 kHz square, 0–5 V | DG922 CH2 | SR810 REF IN |
| Transmission | Photodiode | SR810 A (tee to scope) |
| Error signal X | SR810 CH1 OUTPUT | Red Pitaya IN1 (HV jumper) |
| Drive | Red Pitaya OUT1 | PZT driver MOD IN |
| Monitor (sweep or correction, no dither) | Red Pitaya OUT2 | Scope |
| *Optional:* transmission monitor | Photodiode (second tee) | SR810 rear AUX IN 1, read over GPIB/RS-232 |
""")

md("## Setup")

code(r'''
# ---------------- settings ----------------
HOSTNAME = "192.168.1.100"      # Red Pitaya IP address
PYRPL_CONFIG = "cavity_lock"    # PyRPL config file name

DG922_DITHER_VPP = 0.040        # DG922 CH1 amplitude into IN2 [Vpp]
DITHER_PASS_GAIN = 0.1          # IN2 -> OUT1 gain, so 4 mVpp of dither on OUT1
IN1_SCALE = 20.0                # IN1 on the HV jumper: BNC volts per raw volt read

SWEEP_AMPL, SWEEP_FREQ = 0.010, 5.0        # calibration sweep: 20 mVpp at 5 Hz  [V peak, Hz]
WIDE_AMPL, WIDE_FREQ = 0.20, 1.0           # wide sweep: +/-200 mV at 1 Hz -- check this is safe for the PZT
LOCKIN_TAU, LOCKIN_POLES = 1e-3, 4         # SR810 time constant and slope (24 dB/oct)

UGF_STEPS = [1.0, 2.0, 5.0, 10.0]          # loop bandwidth steps when engaging [Hz]
UGF_STEP_WAIT = 2.0                        # seconds at each step
CLAMP_FRACTION = 0.4                       # clamp half-width = this x distance to the nearest other mode
CLAMP_DEFAULT = 0.05                       # clamp half-width before the wide sweep [V]
OUT_LIMIT = 0.95                           # never command OUT1 beyond +/- this [V]

SR810_VISA = None               # e.g. "GPIB0::8::INSTR": turns on the transmission monitor (AUX IN 1)
SR810_SENS = 0.5                # SR810 sensitivity [V], used to predict the locked transmission

WATCH_PERIOD = 0.1              # watchdog tick [s]
UNLOCK_TIME = 0.5               # a bad reading must last this long to count as an unlock [s]
RELOCK_TRIES = 5
WATCH_SECONDS = None            # None = watch until you stop the cell

CAL_FILE = "calibration.json"
LOG_FILE = "lock_log.csv"
''')

code(r'''
# ---------------- connect and Red Pitaya helpers ----------------
import json, math, time, csv
import numpy as np
import matplotlib.pyplot as plt
%matplotlib inline

from pyrpl import Pyrpl
r = Pyrpl(config=PYRPL_CONFIG, hostname=HOSTNAME, gui=False).rp

for name in ("asg1", "pid2", "iq0", "iq1", "iq2"):     # modules not used here must not write to the outputs
    try:
        getattr(r, name).output_direct = "off"
    except Exception:
        pass
for p in (r.pid0, r.pid1):
    try:
        p.inputfilter = [0, 0, 0, 0]
    except Exception:
        pass

sr = None
if SR810_VISA:
    import pyvisa
    sr = pyvisa.ResourceManager().open_resource(SR810_VISA)
    sr.write("OUTX 1" if "GPIB" in SR810_VISA.upper() else "OUTX 0")   # SR810 answers on this port


def setup_dither():
    """pid1: DG922 dither IN2 -> OUT1 at DITHER_PASS_GAIN."""
    p = r.pid1
    p.i, p.ival, p.p, p.setpoint = 0, 0, DITHER_PASS_GAIN, 0
    p.min_voltage, p.max_voltage = -1.0, 1.0
    p.input, p.output_direct = "in2", "out1"


def setup_lock(ival, clamp=(-OUT_LIMIT, OUT_LIMIT)):
    """pid0: lock PID IN1 -> OUT1 + OUT2, integrator held (i = 0) at ival."""
    p = r.pid0
    p.i, p.p, p.setpoint = 0, 0, 0
    p.min_voltage, p.max_voltage = clamp
    p.ival = ival
    p.input, p.output_direct = "in1", "both"


def sweep_on(ampl, freq):
    r.asg0.setup(waveform="ramp", frequency=freq, amplitude=ampl, offset=0,
                 trigger_source="immediately", output_direct="both")


def sweep_off():
    r.asg0.output_direct, r.asg0.amplitude, r.asg0.offset = "off", 0, 0


def capture(in1, in2, duration):
    """One Red Pitaya scope shot -> (t, ch1, ch2)."""
    s = r.scope
    s.input1, s.input2 = in1, in2
    opts = getattr(s, "durations", None)
    if opts:
        duration = min([d for d in opts if d >= duration] or [max(opts)])
    s.duration, s.trigger_source = duration, "immediately"
    try:
        s.average = True
    except Exception:
        pass
    data = np.array(s.single() if hasattr(s, "single") else s.single_async().result())
    n = data.shape[-1]
    t = np.array(getattr(s, "times", np.linspace(0, s.duration, n)))[:n]
    return t, data[0], data[1]


def read_x(n=20):
    """Mean raw IN1 reading (x IN1_SCALE = volts at the SR810 output)."""
    return float(np.mean([float(r.sampler.in1) for _ in range(n)]))


def transmission():
    """Photodiode level [V] via SR810 AUX IN 1, or None without the monitor."""
    return None if sr is None else float(sr.query("OAUX? 1"))


print(f"Connected. Lock output now {float(r.pid0.ival)*1e3:+.3f} mV; transmission monitor {'on' if sr else 'off'}.")
''')

code(r'''
# ---------------- error-signal analysis ----------------
def h1(x, m):
    """Dither error signal of a unit Lorentzian: x = detuning, m = dither pp, both in half-linewidths."""
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


def lobe_position(m):
    xs = np.linspace(1e-3, 3 + 2 * m, 6000)
    return xs[np.argmax(np.abs(h1(xs, m)))]


def kappa_from_lobes(lobe_sep, dither_pp):
    """Linewidth (drive volts) whose lobe separation matches the measured one for this dither."""
    g = lambda k: lobe_position(dither_pp / k) * k - lobe_sep
    lo, hi = dither_pp / 20, dither_pp * 200
    if g(lo) > 0 or g(hi) < 0:
        return None
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        lo, hi = (lo, mid) if g(mid) > 0 else (mid, hi)
    return math.sqrt(lo * hi)


def expected_locked_transmission(cal):
    """Locked photodiode level predicted from the calibrated lobe height and SR810_SENS."""
    m = cal["dither_pp"] / cal["kappa"] if cal.get("kappa") else 1 / math.sqrt(2)
    c = math.sqrt(2) * np.max(np.abs(h1(np.linspace(1e-3, 3 + 2 * m, 4000), m)))
    v_pk = cal["lobe_height"] * IN1_SCALE * SR810_SENS / (10 * c)
    return v_pk / math.sqrt(1 + m * m)


def binned(v, x, nbins=400):
    o = np.argsort(v)
    v, x = v[o], x[o]
    idx = np.clip(np.digitize(v, np.linspace(v[0], v[-1], nbins + 1)) - 1, 0, nbins - 1)
    n = np.bincount(idx, minlength=nbins)
    ok = n > 0
    return np.bincount(idx, v, nbins)[ok] / n[ok], np.bincount(idx, x, nbins)[ok] / n[ok]


def biggest_feature(vb, xb):
    """Lobes, zero crossing and slope of the largest dispersive feature."""
    i_hi, i_lo = int(np.argmax(xb)), int(np.argmin(xb))
    a, b = sorted((i_hi, i_lo))
    sv, sx = vb[a:b + 1], xb[a:b + 1]
    sc = np.where(np.diff(np.sign(sx)) != 0)[0]
    if len(sc) == 0:
        return None
    k = sc[np.argmin(np.abs(sc + a - (a + b) / 2))]
    vz = sv[k] - sx[k] * (sv[k + 1] - sv[k]) / (sx[k + 1] - sx[k])
    sep = abs(vb[i_hi] - vb[i_lo])
    near = np.abs(sv - vz) < 0.25 * sep
    if near.sum() < 3:
        near = np.argsort(np.abs(sv - vz))[:5]
    return dict(vz=vz, slope=np.polyfit(sv[near], sx[near], 1)[0], lobe_sep=sep,
                lobe_height=(xb[i_hi] - xb[i_lo]) / 2)


def analyze_sweep(t, x, ramp, center, rate):
    """Average of the up- and down-sweep zero crossings (cancels the lock-in delay) -> V0, S, lobes."""
    dr = np.gradient(ramp, t)
    res = {}
    for name, sel in (("up", dr > 0), ("down", dr < 0)):
        if sel.sum() > 200:
            f = biggest_feature(*binned(center + ramp[sel], x[sel]))
            if f:
                res[name] = f
    if not res:
        raise RuntimeError("no error-signal feature in the sweep window")
    lag = rate * LOCKIN_POLES * LOCKIN_TAU
    if len(res) == 2:
        v0, split = (res["up"]["vz"] + res["down"]["vz"]) / 2, res["up"]["vz"] - res["down"]["vz"]
    else:
        (name, f), = res.items()
        v0, split = (f["vz"] - lag if name == "up" else f["vz"] + lag), None
    mean = lambda key: float(np.mean([f[key] for f in res.values()]))
    return dict(v0=float(v0), slope=mean("slope"), lobe_sep=mean("lobe_sep"),
                lobe_height=mean("lobe_height"), split=split, lag=lag)


def sweep_and_analyze(center, ampl=SWEEP_AMPL, freq=SWEEP_FREQ):
    """Sweep around `center`, record X against the sweep, analyse. Returns (result, (t, x, ramp))."""
    setup_dither()
    setup_lock(center)
    sweep_on(ampl, freq)
    time.sleep(3 / freq)                          # let the lock-in settle
    t, x, ramp = capture("in1", "asg0", 2.2 / freq)
    sweep_off()
    return analyze_sweep(t, x, ramp, center, 4 * ampl * freq), (t, x, ramp)


def save_cal(cal):
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f, indent=2)


def load_cal():
    with open(CAL_FILE) as f:
        return json.load(f)
''')

md(r"""
## Part 1: Calibration

### 1. Dither check
Expect about 40 mVpp on IN2 and 4 mVpp on OUT1, with the strongest line at 1000 Hz.
""")

code(r'''
setup_dither()
sweep_off()
setup_lock(float(r.pid0.ival))
time.sleep(0.2)
t, a, b = capture("in2", "out1", 0.008)
f = np.fft.rfftfreq(len(b), t[1] - t[0])
f_pk = f[1:][np.argmax(np.abs(np.fft.rfft(b - b.mean()))[1:])]
print(f"IN2  {np.ptp(a)*1e3:6.2f} mVpp  (expect {DG922_DITHER_VPP*1e3:.0f})")
print(f"OUT1 {np.ptp(b)*1e3:6.2f} mVpp  (expect {DG922_DITHER_VPP*DITHER_PASS_GAIN*1e3:.1f}),  strongest line {f_pk:.0f} Hz")
ok = abs(np.ptp(b) / max(np.ptp(a), 1e-9) - DITHER_PASS_GAIN) < 0.3 * DITHER_PASS_GAIN and abs(f_pk - 1000) < 50
print("PASS" if ok else "CHECK: wrong ratio or frequency -- IN2 jumper (LV), DG922 CH1, or another module writing to OUT1")

fig, ax = plt.subplots(figsize=(8, 3))
ax.plot(t * 1e3, a * 1e3, label="IN2: DG922 dither")
ax.plot(t * 1e3, b * 1e3 * 10, label="OUT1 x 10")
ax.set(xlabel="time [ms]", ylabel="mV")
ax.legend()
plt.show()
''')

md(r"""
### 2. Sweep: centre TEM₀₀ and set the SR810 phase
This turns on a 20 mVpp, 5 Hz sweep, which you'll see on scope ch4. Then:
- Turn the PZT driver's DC knob until the TEM₀₀ peak (the tallest) is mid-sweep on the photodiode trace.
- Turn the SR810 **[Phase]** knob until the rear **Y** output is flat. X then shows the full derivative shape.

The sweep keeps running until step 3.
""")

code(r'''
setup_dither()
setup_lock(float(r.pid0.ival))
sweep_on(SWEEP_AMPL, SWEEP_FREQ)
print(f"Sweeping {2*SWEEP_AMPL*1e3:.0f} mVpp at {SWEEP_FREQ:g} Hz around {float(r.pid0.ival)*1e3:+.1f} mV.")
''')

md(r"""
### 3. Calibrate
This finds V0 (the drive voltage of the resonance), the slope S, the lobe height and the linewidth, and saves them to `calibration.json`. Re-run it just before locking.

What to check:
- The dither ratio is between 0.45 and 1.1 κ.
- The up/down split is small next to the lobe separation.
- The plot shows one clean derivative shape.
""")

code(r'''
center = float(r.pid0.ival)
res, (t, x, ramp) = sweep_and_analyze(center)
dither_pp = DG922_DITHER_VPP * DITHER_PASS_GAIN
kappa = kappa_from_lobes(res["lobe_sep"], dither_pp)
try:
    cal = load_cal()
except FileNotFoundError:
    cal = {}
cal.update(v0=res["v0"], slope=res["slope"], lobe_sep=res["lobe_sep"], lobe_height=res["lobe_height"],
           kappa=kappa, dither_pp=dither_pp, time=time.strftime("%Y-%m-%d %H:%M:%S"))
hw = cal.get("clamp_halfwidth", CLAMP_DEFAULT)
cal["clamp"] = [max(cal["v0"] - hw, -OUT_LIMIT), min(cal["v0"] + hw, OUT_LIMIT)]
save_cal(cal)
setup_lock(cal["v0"])                            # hold the drive on resonance

print(f"V0 (resonance)  {res['v0']*1e3:+8.3f} mV on OUT1")
if res["split"] is not None:
    print(f"up/down split   {res['split']*1e3:8.3f} mV   (lock-in delay alone ~{2*res['lag']*1e3:.2f} mV)")
print(f"slope S         {res['slope']:+8.2f} raw V/V")
print(f"lobe height     {res['lobe_height']*IN1_SCALE:8.3f} V at the SR810 output")
if kappa:
    q = dither_pp / kappa
    print(f"linewidth       {kappa*1e3:8.3f} mV of drive;  dither = {q:.2f} kappa pp  (optimum 0.71)")
    if not 0.45 < q < 1.1:
        print(f"  -> set DG922 CH1 to about {DG922_DITHER_VPP*0.71/q*1e3:.0f} mVpp, update DG922_DITHER_VPP, recalibrate")
else:
    print("linewidth       not solvable: dither far larger or smaller than the line")
print(f"clamp           {cal['clamp'][0]*1e3:+.1f} to {cal['clamp'][1]*1e3:+.1f} mV"
      + ("" if "clamp_halfwidth" in cal else "   (default; run step 4)"))

dr = np.gradient(ramp, t)
fig, ax = plt.subplots(figsize=(8, 4))
for sel, lab in ((dr > 0, "up-sweep"), (dr < 0, "down-sweep")):
    ax.plot((center + ramp[sel]) * 1e3, x[sel] * IN1_SCALE, ".", ms=1.5, label=lab)
ax.axvline(res["v0"] * 1e3, color="k", ls="--", lw=1, label=f"V0 = {res['v0']*1e3:+.3f} mV")
vv = res["v0"] + np.linspace(-0.3, 0.3, 5) * res["lobe_sep"]
ax.plot(vv * 1e3, res["slope"] * (vv - res["v0"]) * IN1_SCALE, lw=2, label=f"slope S = {res['slope']:+.1f}")
ax.axhline(0, color="0.5", lw=0.8)
ax.set(xlabel="drive on OUT1 [mV]", ylabel="SR810 X [V]", title="Calibration sweep")
ax.legend(fontsize=8)
plt.show()
''')

md(r"""
### 4. Wide sweep: mode spacing and clamp (once per alignment)
This sweeps ±200 mV at 1 Hz and lists every mode it finds. It then sets the clamp to ±0.4 × the distance to the nearest other mode, so a lost lock can't jump onto another mode.

**First**, check that ±200 mV × your driver's gain is safe for the PZT. If it isn't, lower `WIDE_AMPL`.
""")

code(r'''
cal = load_cal()
setup_dither()
setup_lock(cal["v0"])
sweep_on(WIDE_AMPL, WIDE_FREQ)
time.sleep(1.5 / WIDE_FREQ)
t, x, ramp = capture("in1", "asg0", 1.1 / WIDE_FREQ)
sweep_off()
setup_lock(cal["v0"])

# stable zero crossings on the up-sweep (lag-corrected), one per cavity mode
up = np.gradient(ramp, t) > 0
lag = 4 * WIDE_AMPL * WIDE_FREQ * LOCKIN_POLES * LOCKIN_TAU
vb, xb = binned(cal["v0"] + ramp[up] - lag, x[up], nbins=2000)
modes = []
for k in np.where(np.diff(np.sign(xb)) != 0)[0]:
    if np.sign(xb[k + 1] - xb[k]) != np.sign(cal["slope"]):
        continue                                  # unstable crossing between two modes
    w = slice(max(0, k - 60), k + 60)
    amp = (xb[w].max() - xb[w].min()) / 2
    if amp > 0.1 * np.abs(xb).max():
        modes.append((vb[k] - xb[k] * (vb[k + 1] - vb[k]) / (xb[k + 1] - xb[k]), amp))
if not modes:
    raise RuntimeError("no modes found: check the error signal, or widen WIDE_AMPL")

main = min(modes, key=lambda m: abs(m[0] - cal["v0"]))
print("modes found (drive mV, lobe at the SR810 output):")
for v, amp in modes:
    print(f"  {v*1e3:+8.2f}   {amp*IN1_SCALE:6.3f} V" + ("   <- TEM00" if v == main[0] else ""))
others = [v for v, _ in modes if v != main[0]]
if others:
    cal["mode_spacing"] = float(min(abs(v - main[0]) for v in others))
    hw = CLAMP_FRACTION * cal["mode_spacing"]
    print(f"nearest other mode {cal['mode_spacing']*1e3:.1f} mV away")
else:
    hw = CLAMP_FRACTION * WIDE_AMPL
    print(f"no other mode within +/-{WIDE_AMPL*1e3:.0f} mV")
cal["clamp_halfwidth"] = hw
cal["clamp"] = [max(cal["v0"] - hw, -OUT_LIMIT), min(cal["v0"] + hw, OUT_LIMIT)]
save_cal(cal)
print(f"clamp {cal['clamp'][0]*1e3:+.1f} to {cal['clamp'][1]*1e3:+.1f} mV")

fig, ax = plt.subplots(figsize=(9, 4))
ax.plot((cal["v0"] + ramp) * 1e3, x * IN1_SCALE, ".", ms=1)
for v, _ in modes:
    ax.axvline(v * 1e3, color="0.6", ls=":", lw=1)
ax.axvspan(cal["clamp"][0] * 1e3, cal["clamp"][1] * 1e3, alpha=0.12, color="g", label="clamp")
ax.set(xlabel="drive on OUT1 [mV]", ylabel="SR810 X [V]", title="Wide sweep: modes and clamp")
ax.legend()
plt.show()
''')

md(r"""
## Part 2: Locking

Re-run step 3 first if more than a few minutes have passed. The next cell only defines functions.
""")

code(r'''
def status(cal):
    """One watchdog reading: correction, X, transmission, and whether it looks unlocked."""
    ival, x, T = float(r.pid0.ival), read_x(), transmission()
    lo, hi = cal["clamp"]
    edge = 0.05 * (hi - lo)
    bad = (T < 0.5 * expected_locked_transmission(cal)) if T is not None else abs(x) > 0.5 * cal["lobe_height"]
    return dict(ival=ival, x=x, T=T, at_edge=ival < lo + edge or ival > hi - edge, bad=bad)


def show(st, prefix=""):
    tr = "" if st["T"] is None else f"   T {st['T']*1e3:6.1f} mV"
    print(f"{prefix}X {st['x']*IN1_SCALE:+7.3f} V   correction {st['ival']*1e3:+8.3f} mV{tr}")


def engage(cal):
    """Park at V0 inside the clamp, then step up the integrator gain. True if it holds."""
    sweep_off()
    setup_dither()
    setup_lock(cal["v0"], cal["clamp"])
    time.sleep(0.3)
    for ugf in UGF_STEPS:
        r.pid0.i = -ugf / cal["slope"]               # i*S < 0 = negative feedback; |i*S| = loop bandwidth
        i_set = float(r.pid0.i)                     # PyRPL rounds i to steps of ~0.0046 Hz
        if i_set == 0:
            print(f"i = {-ugf/cal['slope']:.4g} Hz is below PyRPL's resolution: lower the SR810 gain, recalibrate")
            return False
        for _ in range(int(UGF_STEP_WAIT / WATCH_PERIOD)):
            time.sleep(WATCH_PERIOD)
            st = status(cal)
            if st["at_edge"]:
                r.pid0.i = 0
                print(f"ran to the clamp at {ugf:g} Hz: wrong sign, or not near resonance")
                return False
        show(st, f"  bandwidth {abs(i_set*cal['slope']):5.2f} Hz (i = {i_set:+.4f})   ")
    if st["bad"]:
        r.pid0.i = 0
        print("still far from resonance after engaging (transmission low or X large)")
        return False
    return True


def relock(cal, center):
    """Re-find TEM00 around `center`: sweeps of +/-10, 30, 90 mV ... up to the clamp half-width."""
    hw = (cal["clamp"][1] - cal["clamp"][0]) / 2
    ampl = SWEEP_AMPL
    while ampl <= max(hw, SWEEP_AMPL):
        try:
            res, _ = sweep_and_analyze(center, ampl, SWEEP_FREQ * SWEEP_AMPL / ampl)
        except RuntimeError:
            res = None
        if res and res["lobe_height"] > 0.6 * cal["lobe_height"]:      # TEM00, not a weaker mode
            cal.update(v0=res["v0"], slope=res["slope"], lobe_height=res["lobe_height"],
                       clamp=[max(res["v0"] - hw, -OUT_LIMIT), min(res["v0"] + hw, OUT_LIMIT)])
            print(f"  found TEM00 at {res['v0']*1e3:+.3f} mV")
            return True
        ampl *= 3
    setup_lock(center, cal["clamp"])
    return False
''')

md(r"""
### 5. Engage
This parks the cavity at V0 with the clamp set, then steps the loop bandwidth through 1, 2, 5 and 10 Hz.

On the scope, a working lock shows:
- transmission at about 82% of the peak, with a 2 kHz ripple;
- X near 0;
- OUT2 wandering slowly, not pinned at a clamp.
""")

code(r'''
cal = load_cal()
if sr is not None:
    print(f"transmission monitor on: expected locked level {expected_locked_transmission(cal)*1e3:.0f} mV")
else:
    print("no transmission monitor: watch the photodiode on the scope")
print(f"parking at V0 = {cal['v0']*1e3:+.3f} mV, clamp {cal['clamp'][0]*1e3:+.1f} to {cal['clamp'][1]*1e3:+.1f} mV")
locked = engage(cal)
print("LOCKED" if locked else "not locked: integrator off, drive held")
''')

md(r"""
### 6. Watch and relock
This watches the lock every 0.1 s. It declares an unlock when either of these lasts 0.5 s:
- the correction is within 5% of a clamp edge;
- the transmission is below half the expected level. Without the monitor, the test is |X| above half a lobe.

On an unlock, it re-finds TEM₀₀ and engages again. **Stop the cell (■) to stop watching.** The lock itself keeps running on the Red Pitaya.
""")

code(r'''
log = []                                         # (t, correction, x, transmission, event)
t0 = time.time()
tries, bad_ticks, ticks, good = 0, 0, 0, []
need = int(round(UNLOCK_TIME / WATCH_PERIOD))
print("watching...")
try:
    while WATCH_SECONDS is None or ticks * WATCH_PERIOD < WATCH_SECONDS:
        time.sleep(WATCH_PERIOD)
        ticks += 1
        st = status(cal)
        log.append((time.time() - t0, st["ival"], st["x"], st["T"], ""))
        bad_ticks = bad_ticks + 1 if st["bad"] else 0
        if not (st["at_edge"] or bad_ticks >= need):
            good = (good + [st["ival"]])[-50:]
            continue
        why = ("correction at the clamp" if st["at_edge"] else
               "transmission low" if st["T"] is not None else "error signal large")
        print(f"{time.strftime('%H:%M:%S')}  UNLOCK ({why}) at {st['ival']*1e3:+.3f} mV")
        log[-1] = log[-1][:4] + ("unlock",)
        r.pid0.i = 0
        tries += 1
        if tries > RELOCK_TRIES:
            print("too many unlocks: integrator off, drive held")
            break
        if not relock(cal, float(np.median(good)) if good else cal["v0"]):
            print("TEM00 not found in the allowed range: re-centre with the DC knob, then re-run step 3")
            break
        if not engage(cal):
            print("relock failed: integrator off, drive held")
            break
        bad_ticks, good = 0, []
        print(f"{time.strftime('%H:%M:%S')}  relocked")
except KeyboardInterrupt:
    print("stopped watching; the lock is still running on the Red Pitaya (step 7 turns it off)")
finally:
    with open(LOG_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "correction_V", "x_raw_V", "transmission_V", "event"])
        w.writerows(log)
''')

md("Plot the watch log: the correction (with clamp limits), X, and the transmission. Red lines mark unlocks.")

code(r'''
if not log:
    print("no log yet: run step 6 first")
else:
    L = np.array([(t_, c, x_, np.nan if T is None else T) for t_, c, x_, T, _ in log])
    unlocks = [t_ for t_, *_, e in log if e == "unlock"]
    fig, axs = plt.subplots(3 if sr is not None else 2, 1, sharex=True, figsize=(9, 6))
    axs[0].plot(L[:, 0], L[:, 1] * 1e3)
    for lim in cal["clamp"]:
        axs[0].axhline(lim * 1e3, ls=":", color="0.5")
    axs[0].set_ylabel("correction [mV]")
    axs[0].set_title("dotted lines: clamp now (it moves with each relock)", fontsize=9)
    axs[1].plot(L[:, 0], L[:, 2] * IN1_SCALE)
    axs[1].set_ylabel("X [V]")
    if sr is not None:
        axs[2].plot(L[:, 0], L[:, 3] * 1e3)
        axs[2].set_ylabel("transmission [mV]")
    for ax in axs:
        for u in unlocks:
            ax.axvline(u, color="r", lw=0.8)
    axs[-1].set_xlabel("time [s]")
    plt.show()
    print(f"{len(unlocks)} unlock(s); correction moved {(L[-1, 1] - L[0, 1])*1e3:+.2f} mV in {L[-1, 0]:.0f} s")
''')

md(r"""
### 7. Stop
This turns the integrator off and holds the drive where it is. Set `ZERO = True` to ramp the correction back to 0 V over 2 s.
""")

code(r'''
ZERO = False
r.pid0.i = 0
sweep_off()
if ZERO:
    v = float(r.pid0.ival)
    for k in range(40, -1, -1):
        r.pid0.ival = v * k / 40
        time.sleep(0.05)
print(f"integrator off, correction held at {float(r.pid0.ival)*1e3:+.3f} mV")
''')

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
nb["metadata"]["language_info"] = {"name": "python"}
out = sys.argv[1] if len(sys.argv) > 1 else "cavity_lock.ipynb"
nbf.write(nb, out)
print("wrote", out, "with", len(cells), "cells")
