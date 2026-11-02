"""TEST ONLY: a stand-in for pyrpl.Pyrpl so the app can be exercised without hardware.
Cavity with three modes, PZT lag on the dither, quasi-static IQ demodulation with an n-pole low-pass,
PyRPL-style PID (quantized i, clamps), scope with 'asg0' trigger and the Qt-free acquisition calls."""
import math
import os
import threading
import time

import numpy as np
from scipy.signal import lfilter

KAPPA = 0.0057                     # linewidth in OUT1 volts
MODES = [(0.0, 1.0), (0.120, 0.30), (-0.085, 0.12)]
V_PK = 0.8                         # photodiode volts on the TEM00 peak
PZT_LAG_DEG = 35.0
DEMOD_SCALE = 0.5
NOISE_PD, NOISE_ERR = 0.002, 0.0005
DT = float(os.environ.get("FAKE_DT", "0.0005"))
DITHER_SCALE = 1.04
LAN_S = float(os.environ.get("FAKE_LAN_S", "0"))   # emulated network round trip per register access
REG_COUNT = [0]


def lan():
    REG_COUNT[0] += 1
    if LAN_S:
        time.sleep(LAN_S)                # the "hardware" dither is 4 % bigger than requested (tests the calibration)
I_STEP = 1.0 / (2 ** 32 * 2 * math.pi * 8e-9)


def stages_response(stages, f):
    """Complex response at f of PyRPL input-filter stages (positive = low-pass, negative = high-pass)."""
    H = 1.0 + 0j
    for fc in stages:
        if fc > 0:
            H /= 1 + 1j * f / fc
        elif fc < 0:
            H *= (1j * f / -fc) / (1 + 1j * f / -fc)
    return H


def run_stages(stages, x, dt):
    """Input-filter stages on a uniformly sampled signal. The FPGA runs them at 8 ns, where they equal the
    continuous 1st-order filters; the fake samples at the scope's spacing, so it uses the bilinear transform,
    which matches the continuous response at any sample rate (low-pass 1/(1 + s/w), high-pass = 1 - low-pass)."""
    from scipy.signal import bilinear
    for fc in stages:
        if fc == 0:
            continue
        w = 2 * math.pi * abs(fc)
        b, a = bilinear([1.0], [1 / w, 1.0], 1 / dt) if fc > 0 else bilinear([1 / w, 0.0], [1 / w, 1.0], 1 / dt)
        x = lfilter(b, a, x)
    return x


def h1(x, m):
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


class Obj:
    pass


class PID:
    """PyRPL-style PID: 4-stage input filter (positive corner = low-pass, negative = high-pass, snapped to the
    filter ladder), error = filtered input - setpoint, P + quantized I; no D (DERIVATIVE = 0 in the FPGA)."""
    def __init__(self):
        self.input, self.output_direct, self.setpoint, self.p, self._i, self.ival = "off", "off", 0.0, 0.0, 0.0, 0.0
        self.min_voltage, self.max_voltage, self._if = -1.0, 1.0, [0, 0, 0, 0]

    @property
    def inputfilter(self):
        return list(self._if)

    @inputfilter.setter
    def inputfilter(self, v):
        v = list(np.atleast_1d(v))[:4]
        self._if = [snap(float(x), PID_LADDER) for x in v] + [0] * (4 - len(v))

    @property
    def inputfilter_options(self):
        return options(PID_LADDER)

    @property
    def i(self):
        return self._i

    @i.setter
    def i(self, v):
        self._i = round(v / I_STEP) * I_STEP


class ASG:
    def __init__(self, eng):
        self.eng, self.waveform, self.frequency, self.amplitude, self.offset = eng, "ramp", 1.0, 0.0, 0.0
        self.output_direct, self.trigger_source, self.t0 = "off", "off", 0.0

    def setup(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.t0 = time.time()

    def value(self, t):
        """What this ASG adds to OUT1 (zero unless routed there)."""
        if self.output_direct == "off" or self.amplitude == 0:
            return np.zeros_like(np.asarray(t, dtype=float))
        return self.raw(t)

    def raw(self, t, rel=None):
        """The ASG's internal signal (what the scope sees as 'asg0' / 'asg1'), routed or not.
        rel: times relative to self.t0 instead of epoch times t (epoch float64 resolves only 0.24 us)."""
        if self.amplitude == 0:
            return np.zeros_like(np.asarray(t if rel is None else rel, dtype=float)) + self.offset
        ph = ((np.asarray(t) - self.t0 if rel is None else np.asarray(rel)) * self.frequency) % 1.0
        if self.waveform in ("sin", "cos"):
            return self.offset + self.amplitude * np.sin(2 * np.pi * ph + (np.pi / 2 if self.waveform == "cos" else 0))
        if self.waveform == "halframp":
            return self.offset + self.amplitude * (2 * ph - 1)                 # sawtooth from -1
        return self.offset + self.amplitude * (1 - 4 * np.abs(ph - 0.5))      # triangle from -1


class Engine:
    def __init__(self, rp):
        self.rp = rp
        self.vc = 0.012
        self.rng = np.random.default_rng(1)
        self.filt = np.zeros(4)
        self.dstate = np.zeros(4)
        self.e_prev = 0.0
        self.hist = []                 # (t, drive, pd, err, pid_out)
        self.lock = threading.Lock()
        self.t = time.time()
        threading.Thread(target=self.loop, daemon=True).start()

    # physics helpers (vectorized over drive voltage)
    def dither_pp(self):
        iq = self.rp.iq0
        return 2 * iq.amplitude * DITHER_SCALE if iq.output_direct in ("out1", "both") else 0.0

    def pd_avg(self, v):
        d = self.dither_pp() / 2
        ph = np.linspace(0, 2 * np.pi, 24, endpoint=False)
        v = np.atleast_1d(v)[:, None]
        tot = 0
        for off, h in MODES:
            tot = tot + h * np.mean(1 / (1 + (2 * (v + d * np.sin(ph) - (self.vc + off)) / KAPPA) ** 2), axis=1)
        return V_PK * tot

    def err_raw(self, v):
        dpp = self.dither_pp()
        if dpp <= 0:
            return np.zeros_like(np.atleast_1d(v), dtype=float)
        m = dpp / KAPPA
        iq = self.rp.iq0
        c1 = sum(h * h1(2 * (np.atleast_1d(v) - (self.vc + off)) / KAPPA, m) for off, h in MODES)
        H = self.bandpass_at(iq.frequency)
        return (DEMOD_SCALE * iq.quadrature_factor * V_PK * c1 * abs(H) *
                math.cos(math.radians(iq.phase - PZT_LAG_DEG) + np.angle(H)))

    def bandpass_at(self, f):
        """Response of pid2 at f when iq0 takes its input from pid2 (the band-pass); 1 otherwise."""
        if self.rp.iq0.input != "pid2":
            return 1.0
        return stages_response(self.rp.pid2.inputfilter, f) * self.rp.pid2.p

    def lpf_coeffs(self, dt):
        bws = [b for b in np.atleast_1d(self.rp.iq0.bandwidth) if b > 0]
        return [(1 - math.exp(-2 * math.pi * b * dt)) for b in bws]

    def loop(self):
        while True:
            now = time.time()
            if now - self.t > 1.0:          # don't try to catch up after a long stall
                self.t = now - 1.0
            while self.t < now:
                with self.lock:
                    for _ in range(20):
                        self.t += DT
                        self.step(self.t)
                time.sleep(0)
            time.sleep(0.002)

    def step(self, t):
        self.vc += 0.0002 * DT + 0.0002 * math.sqrt(DT) * self.rng.standard_normal()
        p0 = self.rp.pid0
        asg = float(self.rp.asg0.value(t)) + float(self.rp.asg1.value(t))
        pid_out = (float(np.clip(p0.ival + p0.p * (self.e_prev - p0.setpoint), p0.min_voltage, p0.max_voltage))
                   if p0.output_direct != "off" else 0.0)
        p1, d_out = self.rp.pid1, 0.0                    # D path: pid1 on the error, high-pass input filter, to OUT1
        if p1.output_direct == "out1" and p1.input == "iq0":
            x = self.e_prev
            for k, fc in enumerate(p1.inputfilter):
                if fc == 0:
                    continue
                a = 1 - math.exp(-2 * math.pi * abs(fc) * DT)
                self.dstate[k] += a * (x - self.dstate[k])
                x = self.dstate[k] if fc > 0 else x - self.dstate[k]
            d_out = float(np.clip(p1.p * (x - p1.setpoint), p1.min_voltage, p1.max_voltage))
        v = asg + pid_out + d_out
        y = float(self.err_raw(v)[0])
        for k, a in enumerate(self.lpf_coeffs(DT)):
            self.filt[k] += a * (y - self.filt[k])
            y = self.filt[k]
        e = y + NOISE_ERR * self.rng.standard_normal()
        self.e_prev = e
        if p0.output_direct != "off" and p0.input == "iq0" and p0.i != 0:
            p0.ival = float(np.clip(p0.ival + 2 * math.pi * p0.i * (e - p0.setpoint) * DT, p0.min_voltage, p0.max_voltage))
        pd = float(self.pd_avg(v)[0]) + NOISE_PD * self.rng.standard_normal()
        self.hist.append((t, v, pd, e, pid_out))
        if len(self.hist) > 40000:
            del self.hist[:20000]

    def copies(self, rel):
        """Spare modules driven by asg1 (circuit-only Bode), on asg1-relative times rel: iq1 with its oscillator at
        0 Hz (input x cos(phase) through its low-pass stages and integer gain), and every PID whose input is asg1
        or iq1 (input-filter stages, P + I). FPGA pipeline delays as counted in the Verilog: IQ 3 clocks + 2 per
        low-pass stage; PID 4 clocks + 1 per high-pass and 2 per low-pass input-filter stage."""
        rp = self.rp
        iq = rp.iq1
        n, dt = len(rel), float(rel[-1] - rel[0]) / (len(rel) - 1)
        pids = {nm: getattr(rp, nm) for nm in ("pid1", "pid2") if getattr(rp, nm).input in ("asg1", "iq1")}
        corners = [b for b in np.atleast_1d(iq.bandwidth) if b > 0]
        for p in pids.values():
            corners += [abs(fc) for fc in p.inputfilter if fc != 0]
        npre = int(min(min(8 / (2 * math.pi * min(corners)) if corners else 0.0, 0.5) / dt, 2_000_000)) + 1
        tf = np.concatenate([rel[0] - np.arange(npre, 0, -1) * dt, rel])
        a1 = rp.asg1

        def src_at(name, delay):
            return a1.raw(None, rel=tf - delay) if name == "asg1" else np.zeros(len(tf))
        bws = [b for b in np.atleast_1d(iq.bandwidth) if b > 0]
        tau_iq = (3 + 2 * len(bws)) * 8e-9
        y = src_at(iq.input, tau_iq)
        if iq.acbandwidth > 0:
            a = math.exp(-2 * math.pi * iq.acbandwidth * dt)
            y = lfilter([a, -a], [1, -a], y)
        y = y * (math.cos(math.radians(iq.phase)) if iq.frequency == 0 else 0.0)
        y = run_stages(bws, y, dt) * iq.quadrature_factor
        out = {"iq1": y[npre:]}
        for nm, p in pids.items():
            st = p.inputfilter
            tau = (4 + sum(1 for fc in st if fc < 0) + 2 * sum(1 for fc in st if fc > 0)) * 8e-9
            u = y if p.input == "iq1" else src_at("asg1", tau)
            e = run_stages(st, u, dt) - p.setpoint
            o = np.clip(p.p * e + p.ival + 2 * math.pi * p.i * np.cumsum(e) * dt, p.min_voltage, p.max_voltage)
            out[nm] = o[npre:]
        return out

    # scope traces
    def traces(self, names, duration, trigger, t_start=None):
        """Scope data. Triggered on asg0: computed analytically. Untriggered: the recorded window
        [t_start, t_start + duration] of the simulation (the real scope records after it is armed)."""
        n = 2 ** 14
        with self.lock:
            asg = self.rp.asg0
            now = time.time()
            if trigger == "asg0" and asg.output_direct != "off":
                per = 1 / asg.frequency
                t0 = asg.t0 + math.ceil((now - asg.t0) / per) * per
                ts = t0 + np.arange(n) * duration / n
                p0 = self.rp.pid0
                pid_out = p0.ival if p0.output_direct != "off" else 0.0
                v = asg.value(ts) + pid_out
                bws = [b for b in np.atleast_1d(self.rp.iq0.bandwidth) if b > 0]
                pre = 6 * 4 / (2 * math.pi * max(min(bws), 1)) if bws else 0.0
                npre = int(pre / (duration / n)) + 1
                tp = t0 - np.arange(npre, 0, -1) * duration / n
                vfull = np.concatenate([asg.value(tp) + pid_out, v])
                y = self.err_raw(vfull)
                dt = duration / n
                for a in self.lpf_coeffs(dt):
                    y = lfilter([a], [1, -(1 - a)], y)
                err = y[npre:] + NOISE_ERR * self.rng.standard_normal(n)
                pd = self.pd_avg(v) + NOISE_PD * self.rng.standard_normal(n)
                d = self.dither_pp() / 2
                sig = {"in1": pd, "iq0": err, "asg0": asg.value(ts), "pid0": np.full(n, pid_out),
                       "out1": v + d * np.sin(2 * np.pi * self.rp.iq0.frequency * ts)}
                ready_at = t0 + duration
            else:
                h = np.array(self.hist[-int((self.t - (t_start or self.t) + duration) / DT) - 4:])
                tt = h[:, 0]
                t_end = min(tt[-1], (t_start or tt[-1] - duration) + duration)
                ts = np.linspace(t_end - duration, t_end, n)
                d = self.dither_pp() / 2
                drive = np.interp(ts, tt, h[:, 1])
                sig = {"in1": np.interp(ts, tt, h[:, 2]), "iq0": np.interp(ts, tt, h[:, 3]),
                       "pid0": np.interp(ts, tt, h[:, 4]), "asg0": asg.value(ts),
                       "out1": drive + d * np.sin(2 * np.pi * self.rp.iq0.frequency * ts)}
                ready_at = now + duration          # an untriggered trace still takes its duration to record
        rel1 = (ts[0] - self.rp.asg1.t0) + np.arange(n) * (duration / n)   # precise relative time axis
        sig["asg1"] = self.rp.asg1.raw(None, rel=rel1)
        if set(names) & {"iq1", "pid1", "pid2"}:        # only when a spare-module copy is recorded
            sig.update(self.copies(rel1))
        p1 = self.rp.pid1                                # OUT2 monitor: pid1 pass-through (P only, clamped)
        src = sig.get(p1.input, np.zeros(n)) if p1.output_direct == "out2" else np.zeros(n)
        sig["out2"] = np.clip(p1.p * (src - p1.setpoint), p1.min_voltage, p1.max_voltage)
        sig["in2"] = 0.0005 * self.rng.standard_normal(n)
        return [sig.get(nm, np.zeros(n)) for nm in names], ready_at


class Scope:
    durations = [8e-9 * 2 ** k * 2 ** 14 for k in range(17)]

    def __init__(self, eng):
        object.__setattr__(self, "eng", eng)
        for k, v in dict(input1="in1", input2="in2", duration=self.durations[10], trigger_source="immediately",
                         trigger_delay=0.0, average=True, rolling_mode=False, _data=None, _ready_at=0.0,
                         _arm=None).items():
            object.__setattr__(self, k, v)

    def __setattr__(self, k, v):
        if not k.startswith("_"):
            lan()                                        # a register write on the real board
        object.__setattr__(self, k, v)

    @property
    def times(self):
        lan(); lan()                                     # PyRPL reads duration and trigger_delay
        return np.linspace(0, self.duration, 2 ** 14, endpoint=False)

    def _start_acquisition(self):
        lan()
        object.__setattr__(self, "_data", None)
        if self.trigger_source == "immediately":       # record from now on; read out when complete
            object.__setattr__(self, "_arm", (self.eng.t, [self.input1, self.input2], self.duration))
            object.__setattr__(self, "_ready_at", time.time() + self.duration)
            return
        object.__setattr__(self, "_arm", None)
        d, r = self.eng.traces([self.input1, self.input2], self.duration, self.trigger_source)
        object.__setattr__(self, "_data", d)
        object.__setattr__(self, "_ready_at", r)

    def curve_ready(self):
        lan()
        return time.time() >= self._ready_at

    def _get_curve(self):
        if self._data is None and self._arm is not None:
            t0, names, dur = self._arm
            while self.eng.t < t0 + dur:                 # the simulation may run a few ms behind the clock
                time.sleep(0.001)
            d, _ = self.eng.traces(names, dur, "immediately", t_start=t0)
            object.__setattr__(self, "_data", d)
        return np.array(self._data)

    _start_trace_acquisition = _start_acquisition         # PyRPL 0.9.9 names
    _get_trace = _get_curve


class Sampler:
    def __init__(self, eng):
        self.eng = eng

    @property
    def in1(self):
        return self.eng.hist[-1][2] if self.eng.hist else 0.0


def filter_ladder(minbw):
    """Positive valid corners of a PyRPL filter block, as attributes.FilterRegister.valid_frequencies computes
    them: alpha = 2^shift / 2^MAXSHIFT, f = -ln(1 - alpha) 125 MHz / 2 pi (with PyRPL's correction factors)."""
    maxshift = int(math.floor(math.log2(125e6 / minbw))) + 1
    corr = {0.5: 0.7, 0.25: 1.65, 0.125: 1.17, 0.0625: 1.08, 0.03125: 1.04, 0.015625: 1.02, 0.0078125: 1.01}
    out = []
    for shift in range(0, maxshift - 2):
        alpha = 2.0 ** shift / 2 ** maxshift
        out.append(-math.log(1 - alpha) / 2 / math.pi * 125e6 * corr.get(alpha, 1.0))
    return out


IQ_LPF_LADDER = filter_ladder(10)     # QUADRATUREFILTERMINBW = 10
IQ_IN_LADDER = filter_ladder(50)      # IQ INPUTFILTERMINBW = 50 (acbandwidth)
PID_LADDER = filter_ladder(10)        # PID FILTERMINBW = 10


def options(ladder):
    return [-x for x in reversed(ladder)] + [0] + list(ladder)


def nearest_log(v, ladder):
    pos = [x for x in ladder if x > 0]
    return min(pos, key=lambda x: abs(math.log(x / v)))


def snap(v, ladder):
    return 0 if v == 0 else math.copysign(nearest_log(abs(v), ladder), v)


class IQ:
    """FPGA-like rounding: factor-2 filter ladder, 2 filter stages, integer quadrature factor."""
    @property
    def bandwidth(self):
        return self._bw

    @bandwidth.setter
    def bandwidth(self, v):
        v = [b for b in np.atleast_1d(v)][:2]
        self._bw = [nearest_log(b, IQ_LPF_LADDER) if b > 0 else 0 for b in v] + [0] * (2 - len(v))

    @property
    def bandwidths(self):
        return options(IQ_LPF_LADDER)

    @property
    def inputfilter_options(self):
        return options(IQ_IN_LADDER)

    @property
    def acbandwidth(self):
        return self._ac

    @acbandwidth.setter
    def acbandwidth(self, v):
        self._ac = 0 if v <= 0 else nearest_log(v, IQ_IN_LADDER)

    @property
    def quadrature_factor(self):
        return self._qf

    @quadrature_factor.setter
    def quadrature_factor(self, v):
        q = float(round(v))
        self._qf = q if q != 0 or v == 0 else math.copysign(1.0, v)


class RP:
    def __init__(self):
        self.iq0, self.iq1, self.iq2 = IQ(), IQ(), IQ()
        for iq in (self.iq0, self.iq1, self.iq2):
            iq.input, iq.frequency, iq.amplitude, iq.phase = "in1", 1000.0, 0.0, 0.0
            iq.bandwidth, iq.acbandwidth, iq.gain, iq.quadrature_factor = [250.0, 250.0], 100.0, 0.0, 1.0
            iq.output_signal, iq.output_direct = "quadrature", "off"
        self.pid0, self.pid1, self.pid2 = PID(), PID(), PID()
        self.pid0.ival = 0.0
        self.asg0 = ASG(None)
        self.asg1 = ASG(None)
        self.eng = Engine(self)
        self.scope = Scope(self.eng)
        self.sampler = Sampler(self.eng)


class NetworkAnalyzer:
    """Model transfer functions of the fake loop (plant = error slope x demodulation low-pass)."""
    def __init__(self, rp):
        self.rp = rp
        self.start_freq, self.stop_freq, self.points, self.rbw, self.amplitude = 10.0, 1000.0, 30, 100.0, 0.001
        self.input, self.output_direct, self.logscale, self.trace_average = "pid0", "out1", True, 1
        self.iq = Obj()

    def setup(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def frequencies(self):
        return np.logspace(np.log10(self.start_freq), np.log10(self.stop_freq), int(self.points))

    def single(self, timeout=None):
        f = self.frequencies
        eng, rp = self.rp.eng, self.rp
        d = 1e-5
        S = float((eng.err_raw(eng.vc + d) - eng.err_raw(eng.vc - d))[0] / (2 * d))
        P = S * np.ones_like(f, dtype=complex)
        for b in np.atleast_1d(rp.iq0.bandwidth):
            if b > 0:
                P = P / (1 + 1j * f / b)
        C = rp.pid0.i / (1j * f) + rp.pid0.p
        CP = C * P
        time.sleep(0.3)
        noise = 1 + 0.02 * (np.random.default_rng(3).standard_normal(len(f)))
        return (CP / (1 - CP) if self.input == "pid0" else P / (1 - CP)) * noise


class Pyrpl:
    def __init__(self, config=None, hostname=None, gui=False, **kw):
        self.rp = RP()
        self.networkanalyzer = NetworkAnalyzer(self.rp)
