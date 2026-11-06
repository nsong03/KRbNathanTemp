"""TEST ONLY: a stand-in for pyrpl.Pyrpl so the app can be exercised without hardware.
Cavity with three modes, PZT lag on the dither, quasi-static IQ demodulation with an n-pole low-pass,
PyRPL-style PID (quantized i, clamps), scope with 'asg0'/'asg1' triggers and the Qt-free acquisition calls.
OUT2: pid2 + asg1 summed into an AOM intensity plant (class AOM). Every register write by app code is logged
in WRITES as (time, module, attribute, value, app source file) for the isolation tests."""
import math
import os
import sys
import threading
import time

import numpy as np
from scipy.signal import lfilter

KAPPA = 0.0057                     # linewidth in OUT1 volts
MODES = [(0.0, 1.0), (0.120, 0.30), (-0.085, 0.12)]
V_PK = 0.8                         # photodiode volts on the TEM00 peak
PZT_LAG_DEG = 35.0
DEMOD_SCALE = 0.5
E_PK = 0.4                         # pi_app plant: external error signal amplitude [V] (slope -140 V/V at resonance)
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
P_STEP = 2.0 ** -12
I_MAX, P_MAX = (2 ** 23 - 1) * I_STEP, (2 ** 23 - 1) * P_STEP

WRITES = []                      # (t, module, attr, value, source file of the app frame that wrote it)
WRITE_N = [0]                    # all logged writes
WRITE_N_NONSCOPE = [0]           # logged writes to anything but the scope
_BUILDING = [False]              # True while the fake builds its modules (constructor writes are not logged)
_HERE = os.path.abspath(__file__)


def _src():
    """File name of the first app frame on the stack (None for the fake's own engine thread)."""
    if _BUILDING[0]:
        return None
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if os.path.abspath(fn) != _HERE:
            b = os.path.basename(fn)
            return None if b == "threading.py" else b
        f = f.f_back
    return None


class Mod:
    """A hardware module whose attribute writes are logged (private names and 'owner' are not registers)."""
    _name = "?"

    def __setattr__(self, k, v):
        if not k.startswith("_") and k not in ("owner", "eng", "rp", "iq"):
            src = _src()
            if src is not None:
                WRITE_N[0] += 1
                if self._name != "scope":
                    WRITE_N_NONSCOPE[0] += 1
                if len(WRITES) < 400000:
                    WRITES.append((time.time(), self._name, k, v if isinstance(v, (int, float, str, bool, type(None)))
                                   else repr(v), src))
        object.__setattr__(self, k, v)


def h1(x, m):
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


class Obj:
    pass


class PID(Mod):
    def __init__(self, name):
        object.__setattr__(self, "_name", name)
        self.input, self.output_direct, self.setpoint, self.p, self._i, self.ival = "off", "off", 0.0, 0.0, 0.0, 0.0
        self.min_voltage, self.max_voltage = -1.0, 1.0
        object.__setattr__(self, "_filt", [0, 0, 0, 0])

    @property
    def i(self):
        return self._i

    @i.setter
    def i(self, v):
        self._i = float(np.clip(round(v / I_STEP), -2 ** 23 + 1, 2 ** 23 - 1)) * I_STEP

    @property
    def p(self):
        return self._p

    @p.setter
    def p(self, v):
        self._p = float(np.clip(round(v / P_STEP), -2 ** 23 + 1, 2 ** 23 - 1)) * P_STEP

    @property
    def inputfilter(self):
        return list(self._filt)

    @inputfilter.setter
    def inputfilter(self, v):
        v = [b for b in np.atleast_1d(v)][:4]
        self._filt = [math.copysign(nearest_log(abs(b), LPF_LADDER), b) if b else 0 for b in v] + [0] * (4 - len(v))


class ASG(Mod):
    def __init__(self, eng, name):
        object.__setattr__(self, "_name", name)
        self.eng, self.waveform, self.frequency, self.amplitude, self.offset = eng, "ramp", 1.0, 0.0, 0.0
        self.output_direct, self.trigger_source, self.t0 = "off", "off", 0.0

    def setup(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.t0 = time.time()

    def value(self, t, out="out1"):
        """What this ASG adds to output `out` (zero unless routed there)."""
        if self.output_direct not in (out, "both"):
            return np.zeros_like(np.asarray(t, dtype=float))
        return self.raw(t)                       # amplitude 0: the offset alone (a DC level), like the hardware

    def raw_rel(self, t0, trel):
        """The internal signal at times t0 + trel, without losing precision on epoch times."""
        trel = np.asarray(trel, dtype=float)
        if self.amplitude == 0:
            return np.zeros_like(trel) + self.offset
        ph = (((t0 - self.t0) * self.frequency) % 1.0 + trel * self.frequency) % 1.0
        if self.waveform in ("sin", "cos"):
            return self.offset + self.amplitude * np.sin(2 * np.pi * ph + (np.pi / 2 if self.waveform == "cos" else 0))
        if self.waveform == "halframp":
            return self.offset + self.amplitude * (2 * ph - 1)
        return self.offset + self.amplitude * (1 - 4 * np.abs(ph - 0.5))

    def raw(self, t):
        """The ASG's internal signal (what the scope sees as 'asg0' / 'asg1'), routed or not."""
        if self.amplitude == 0:
            return np.zeros_like(np.asarray(t, dtype=float)) + self.offset
        ph = ((np.asarray(t) - self.t0) * self.frequency) % 1.0
        if self.waveform in ("sin", "cos"):
            return self.offset + self.amplitude * np.sin(2 * np.pi * ph + (np.pi / 2 if self.waveform == "cos" else 0))
        if self.waveform == "halframp":
            return self.offset + self.amplitude * (2 * ph - 1)                 # sawtooth from -1
        return self.offset + self.amplitude * (1 - 4 * np.abs(ph - 0.5))      # triangle from -1


AOM_DELAY = float(os.environ.get("FAKE_AOM_DELAY_US", "0.5")) * 1e-6   # loop delay: RP + acoustic + PD [s]
RIN_SCALE = float(os.environ.get("FAKE_RIN", "1.0"))                     # scales all intensity noise


class AOM:
    """OUT2 -> inverting bias driver (V_drv = BIAS - 10 u) -> Schottky + 100 ohm -> mixer IF current (positive
    only, limited to 25 mA) -> RF amplitude (proportional, saturating) -> amplifier -> AOM diffraction
    efficiency sin^2 (peak at RF amplitude A_PK, falls beyond) -> photodiode (gain, delay, 5 MHz pole, noise).
    The loop runs at ~100 kHz, far too fast to step in real time, so:
      * slow state (every 2 ms): light level drift, step disturbance, blocked beam, and pid2's integrator,
        which (much faster than 2 ms) runs to its stable equilibrium or to a clamp;
      * scope windows are synthesized: open loop nonlinear (scan), closed loop linearized around the equilibrium
        with the noise and an asg1 test sine shaped by the loop with the true delay."""
    BIAS, GAIN, V_D, R_S, I_LIM = 1.2, 10.0, 0.3, 100.0, 0.025
    A_PK = 0.75                       # RF amplitude (relative to full IF) at the diffraction-efficiency peak
    PD_PEAK = 0.9                     # photodiode volts at the efficiency peak
    PD_BW = 5e6
    WHITE, FLICKER = 2e-6, 3e-5       # relative ASD: white [1/rtHz]; 1/f: FLICKER/sqrt(f)
    LINES = [(120.0, 4e-4), (1300.0, 1.5e-4), (17e3, 6e-5)]   # vibration lines (frequency, relative amplitude)

    def __init__(self, rp, rng):
        self.rp, self.rng = rp, rng
        self.delay, self.rin = AOM_DELAY, RIN_SCALE
        self.drift = 0.0               # slow relative drift of the light (Ornstein-Uhlenbeck, ~1 %)
        self.disturb = 1.0             # step disturbance (tests)
        self.blocked = False           # beam blocked (tests)
        self.ug = np.linspace(-1.2, 1.2, 4801)
        self.eg = self.eta(self.ug)

    def eta(self, u):
        vd = np.clip(self.BIAS - self.GAIN * np.asarray(u, dtype=float), -4.5, 4.5)
        cur = np.clip((vd - self.V_D) / self.R_S, 0, self.I_LIM)
        arf = np.tanh(1.2 * cur / self.I_LIM) / math.tanh(1.2)
        return np.sin(np.pi / 2 * arf / self.A_PK) ** 2

    def eta_i(self, u):
        return np.interp(u, self.ug, self.eg)

    def light(self):
        return 0.0 if self.blocked else (1 + self.drift) * self.disturb

    def pd(self, u):
        return self.PD_PEAK * self.light() * self.eta_i(u)

    def slope(self, u, d=2e-4):
        return float((self.pd(u + d) - self.pd(u - d)) / (2 * d))

    def _ext(self, t):
        """Slow part of what asg1 adds to OUT2 (a fast test sine averages out)."""
        a = self.rp.asg1
        if a.output_direct not in ("out2", "both"):
            return 0.0
        if a.amplitude == 0 or a.frequency > 200:
            return float(a.offset)
        return float(a.raw(t))

    def pid_out(self):
        p2 = self.rp.pid2
        return float(np.clip(p2.ival, p2.min_voltage, p2.max_voltage)) if p2.output_direct in ("out2", "both") else 0.0

    def update(self, t, dt):
        self.drift += -self.drift * dt / 2.0 + 0.01 * self.rin * math.sqrt(dt) * self.rng.standard_normal()
        p2 = self.rp.pid2
        if p2.input != "in2" or p2.i == 0:
            return
        lo, hi = p2.min_voltage, p2.max_voltage
        ext = self._ext(t)
        routed = p2.output_direct in ("out2", "both")
        v0 = float(np.clip(p2.ival, lo, hi))
        e0 = float(self.pd(v0 + ext if routed else ext)) - p2.setpoint
        step = 2 * math.pi * p2.i * e0 * dt
        if not routed:                                 # its output does not reach the AOM: it just integrates
            p2.ival = float(np.clip(v0 + step, lo, hi))
            return
        d = 1 if step > 0 else -1
        v = np.linspace(v0, hi if d > 0 else lo, 400)
        e = self.pd(v + ext) - p2.setpoint
        flip = np.flatnonzero(np.sign(p2.i * e) != np.sign(p2.i * e0))
        if len(flip):
            k = flip[0]
            target = float(v[k - 1] - e[k - 1] * (v[k] - v[k - 1]) / (e[k] - e[k - 1])) if k > 0 else v0
        else:
            target = float(v[-1])
        p2.ival = target if abs(step) >= abs(target - v0) else v0 + step

    def sample_pd(self):
        return float(self.pd(self.pid_out() + self._ext(time.time()))) + 0.001 * self.rng.standard_normal()

    def noise_spec(self, n, dt):
        """Broadband relative intensity noise (white + 1/f) as an rfft spectrum (periodic in the window, so
        filtering it in the frequency domain is exact), and the vibration lines as complex amplitudes."""
        f = np.fft.rfftfreq(n, dt)
        f1 = f.copy()
        f1[0] = f[1] if n > 1 else 1.0
        asd = np.sqrt(self.WHITE ** 2 + self.FLICKER ** 2 / f1) * self.rin
        Z = np.fft.rfft(self.rng.standard_normal(n)) * asd / math.sqrt(2 * dt)
        Z[0] = 0
        lines = [(fl, a * self.rin * np.exp(1j * self.rng.uniform(0, 2 * np.pi))) for fl, a in self.LINES]
        return Z, lines

    @staticmethod
    def adc_noise(n, dt, rng):
        """Measurement noise of the scope: ~63 nV/rtHz up to 62.5 MHz (0.5 mV rms per 8 ns sample), averaged by
        the decimation."""
        return 6.3e-8 * math.sqrt(0.5 / max(dt, 8e-9)) * rng.standard_normal(n)

    def filt(self, f):
        """pid2's input filter: positive corners low-pass, negative high-pass (1st order each)."""
        F = np.ones(len(f), dtype=complex)
        for b in self.rp.pid2.inputfilter:
            if b > 0:
                F = F / (1 + 1j * f / b)
            elif b < 0:
                F = F * (1j * f / -b) / (1 + 1j * f / -b)
        return F

    def synth(self, t0, trel):
        rp = self.rp
        p2, a1 = rp.pid2, rp.asg1
        n = len(trel)
        dt = float(trel[1] - trel[0])
        Z, lines = self.noise_spec(n, dt)
        tt = np.asarray(trel, dtype=float)
        a1_raw = a1.raw_rel(t0, trel)
        a1_out = a1_raw if (a1.output_direct in ("out2", "both") and a1.amplitude != 0) else np.zeros(n)
        routed = p2.output_direct in ("out2", "both")
        lo, hi = p2.min_voltage, p2.max_voltage
        ival = float(np.clip(p2.ival, lo, hi))
        u_pid = ival if routed else 0.0
        closed = routed and p2.input == "in2" and (p2.i != 0 or p2.p != 0) and self.light() > 0 \
            and lo + 1e-4 < ival < hi - 1e-4
        sine = a1.waveform == "sin" and a1.amplitude != 0 and a1.output_direct in ("out2", "both")
        if closed:
            u_eq = u_pid + (float(a1.offset) if sine else float(np.mean(a1_out)))
            pd_eq = float(self.pd(u_eq))
            K = self.slope(u_eq)
            f = np.fft.rfftfreq(n, dt)
            f1 = f.copy()
            f1[0] = 1e-3
            H = np.exp(-2j * np.pi * f1 * self.delay) / (1 + 1j * f1 / self.PD_BW)
            C = (p2.p + p2.i / (1j * f1)) * self.filt(f1)
            G = -K * H * C
            D = pd_eq * Z
            e = np.fft.irfft(D / (1 + G), n)
            c = np.fft.irfft(C * D / (1 + G), n)
            for fl, a in lines:                         # each vibration line through the loop, exactly
                Hl = np.exp(-2j * np.pi * fl * self.delay) / (1 + 1j * fl / self.PD_BW)
                Cl = (p2.p + p2.i / (1j * fl)) * self.filt(np.array([fl]))[0]
                Sl = 1 / (1 - K * Hl * Cl)
                zl = pd_eq * a * np.exp(2j * np.pi * fl * tt)
                e = e + np.real(Sl * zl)
                c = c + np.real(Cl * Sl * zl)
            if sine:
                f0 = float(a1.frequency)
                H0 = np.exp(-2j * np.pi * f0 * self.delay) / (1 + 1j * f0 / self.PD_BW)
                C0 = (p2.p + p2.i / (1j * f0)) * self.filt(np.array([f0]))[0]
                G0 = -K * H0 * C0
                ph = 2 * np.pi * ((((t0 - a1.t0) * f0) % 1.0) + trel * f0)
                xh = a1.amplitude * np.exp(1j * (ph - np.pi / 2))       # sin as the real part
                e = e + np.real(K * H0 / (1 + G0) * xh)
                c = c + np.real(C0 * K * H0 / (1 + G0) * xh)
            in2 = p2.setpoint + e if abs(pd_eq - p2.setpoint) < 0.02 else pd_eq + e
            pid2 = np.clip(ival + c, lo, hi)
        else:
            u = u_pid + np.asarray(a1.raw_rel(t0, trel - self.delay) if np.any(a1_out) else a1_out)
            r = np.fft.irfft(Z, n)
            for fl, a in lines:
                r = r + np.real(a * np.exp(2j * np.pi * fl * tt))
            in2 = self.pd(u) * (1 + r)
            k = 1 - math.exp(-2 * math.pi * self.PD_BW * dt)
            if k < 0.999:
                in2 = lfilter([k], [1, -(1 - k)], in2, zi=[in2[0] * (1 - k)])[0]
            e = in2 - p2.setpoint
            pid2 = np.clip(p2.p * e + ival, lo, hi) if p2.p != 0 else np.full(n, ival)
        in2 = in2 + self.adc_noise(n, dt, self.rng)
        out2 = np.clip((pid2 if routed else 0.0) + a1_out, -1, 1)
        return {"in2": in2, "pid2": pid2, "asg1": a1_raw, "out2": out2}


class Engine:
    def __init__(self, rp):
        self.rp = rp
        self.vc = 0.012
        self.rng = np.random.default_rng(1)
        self.filt = np.zeros(4)
        self.hist = []                 # (t, drive, pd, err, pid_out)
        self.lock = threading.Lock()
        self.t = time.time()
        self.aom = AOM(rp, np.random.default_rng(7))
        self._n = 0
        # errsig_app wiring (FAKE_PZT_OUT=out2 or eng.pzt_out = "out2"): OUT2 drives the PZT; an external servo box
        # integrates OUT1 (the error output) into IN2: ext_v += 2 pi ext_i * OUT1 * dt, clamped to +/- ext_lim
        self.pzt_out = os.environ.get("FAKE_PZT_OUT", "out1")
        self.ext_on, self.ext_i, self.ext_v, self.ext_lim = False, 0.0, 0.0, 0.75
        # errsig2ph_app (FAKE_LOCKIN=iq2): the lock-in is iq2 with both quadratures, X = 'iq2' = A cos(phase - lag),
        # Y = 'iq2_2' = A sin(phase - lag) (same low-pass and gain)
        self.li_name = os.environ.get("FAKE_LOCKIN", "iq0")
        self.filt_y = np.zeros(4)
        # pi_app (FAKE_PLANT=errin): IN1 is an external error signal of the cavity, e(v) dispersive with slope
        # -2 E_PK / (KAPPA/2) at resonance; pid0 (input in1) is a full PI with its input filter; OUT1 = asg0 + pid0
        self.plant = os.environ.get("FAKE_PLANT", "cavity")
        self.pid_out, self.ifilt = 0.0, np.zeros(4)
        threading.Thread(target=self.loop, daemon=True).start()

    # physics helpers (vectorized over drive voltage)
    @property
    def li(self):
        return getattr(self.rp, self.li_name)

    def dither_pp(self):
        iq = self.li
        return 2 * iq.amplitude * DITHER_SCALE if iq.output_direct in (self.pzt_out, "both") else 0.0

    def pd_avg(self, v):
        d = self.dither_pp() / 2
        ph = np.linspace(0, 2 * np.pi, 24, endpoint=False)
        v = np.atleast_1d(v)[:, None]
        tot = 0
        for off, h in MODES:
            tot = tot + h * np.mean(1 / (1 + (2 * (v + d * np.sin(ph) - (self.vc + off)) / KAPPA) ** 2), axis=1)
        return V_PK * tot

    def err_raw(self, v, quad=0):
        dpp = self.dither_pp()
        if dpp <= 0:
            return np.zeros_like(np.atleast_1d(v), dtype=float)
        m = dpp / KAPPA
        iq = self.li
        c1 = sum(h * h1(2 * (np.atleast_1d(v) - (self.vc + off)) / KAPPA, m) for off, h in MODES)
        trig = math.sin if quad else math.cos
        return DEMOD_SCALE * iq.quadrature_factor * V_PK * c1 * trig(math.radians(iq.phase - PZT_LAG_DEG))

    def lpf_coeffs(self, dt):
        bws = [b for b in np.atleast_1d(self.li.bandwidth) if b > 0]
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

    def ext_err(self, v):
        """pi_app plant: the external error signal on IN1 for an OUT1 drive v (dispersive, 3 modes)."""
        tot = 0
        for off, h in MODES:
            x = 2 * (np.atleast_1d(v) - (self.vc + off)) / KAPPA
            tot = tot - h * x / (1 + x ** 2)
        return E_PK * tot

    def step_errin(self, t):
        p0 = self.rp.pid0
        v = float(self.rp.asg0.value(t, "out1")) + self.pid_out
        e = float(self.ext_err(v)[0]) + NOISE_ERR * self.rng.standard_normal()
        y = e
        for k, b in enumerate(p0.inputfilter):              # PyRPL: > 0 low-pass, < 0 high-pass, 0 off
            if not b:
                continue
            a = 1 - math.exp(-2 * math.pi * abs(b) * DT)
            self.ifilt[k] += a * (y - self.ifilt[k])
            y = self.ifilt[k] if b > 0 else y - self.ifilt[k]
        if p0.input == "in1" and p0.output_direct in ("out1", "both"):
            ev = y - p0.setpoint
            p0.ival = float(np.clip(p0.ival + 2 * math.pi * p0.i * ev * DT, p0.min_voltage, p0.max_voltage))
            self.pid_out = float(np.clip(p0.p * ev + p0.ival, p0.min_voltage, p0.max_voltage))
        else:
            self.pid_out = 0.0
        self.hist.append((t, v, e, y, self.pid_out, 0.0, 0.0, 0.0))
        if len(self.hist) > 40000:
            del self.hist[:20000]

    def step(self, t):
        self.vc += 0.0002 * DT + 0.0002 * math.sqrt(DT) * self.rng.standard_normal()
        if self.plant == "errin":
            self.step_errin(t)
            return
        p0 = self.rp.pid0
        self._n += 1
        if self._n % 4 == 0:
            self.aom.update(t, 4 * DT)
        po = self.pzt_out
        asg = float(self.rp.asg0.value(t, po)) + float(self.rp.asg1.value(t, po))
        if po == "out2":                               # errsig wiring: pid0 passes IN2 (the external servo) to OUT2
            pid_out = float(np.clip(p0.p * (self.ext_v - p0.setpoint) + p0.ival, p0.min_voltage, p0.max_voltage)) \
                if (p0.output_direct in ("out2", "both") and p0.input == "in2") else 0.0
        else:
            pid_out = float(np.clip(p0.ival, p0.min_voltage, p0.max_voltage)) if p0.output_direct in ("out1", "both") \
                else 0.0
        v = asg + pid_out
        y = float(self.err_raw(v)[0])
        coeffs = self.lpf_coeffs(DT)
        for k, a in enumerate(coeffs):
            self.filt[k] += a * (y - self.filt[k])
            y = self.filt[k]
        e = y + NOISE_ERR * self.rng.standard_normal()
        ey = 0.0
        if self.li_name == "iq2":
            y = float(self.err_raw(v, 1)[0])
            for k, a in enumerate(coeffs):
                self.filt_y[k] += a * (y - self.filt_y[k])
                y = self.filt_y[k]
            ey = y + NOISE_ERR * self.rng.standard_normal()
        if p0.output_direct != "off" and p0.input == self.li_name and p0.i != 0:
            p0.ival = float(np.clip(p0.ival + 2 * math.pi * p0.i * (e - p0.setpoint) * DT, p0.min_voltage, p0.max_voltage))
        pd = float(self.pd_avg(v)[0]) + NOISE_PD * self.rng.standard_normal()
        p1 = self.rp.pid1
        e1 = {self.li_name: e, self.li_name + "_2": ey}.get(p1.input)
        err_out = float(np.clip(p1.p * (e1 - p1.setpoint) + p1.ival, p1.min_voltage, p1.max_voltage)) \
            if (po == "out2" and p1.output_direct in ("out1", "both") and e1 is not None and p1.i == 0) else 0.0
        if self.ext_on:
            self.ext_v = float(np.clip(self.ext_v + 2 * math.pi * self.ext_i * err_out * DT, -self.ext_lim,
                                       self.ext_lim))
        self.hist.append((t, v, pd, e, pid_out, err_out, self.ext_v, ey))
        if len(self.hist) > 40000:
            del self.hist[:20000]

    def copies(self, ts):
        """The spare iq1 -> pid1 chain driven by an ASG (circuit-only Bode). iq1 with its oscillator at 0 Hz is
        input x cos(phase) through its low-pass stages and integer gain; pid1 is P + I of its input."""
        rp = self.rp
        iq, p2 = rp.iq1, rp.pid1
        n, dt = len(ts), float(ts[-1] - ts[0]) / (len(ts) - 1)   # not ts[1]-ts[0]: epoch times lose precision
        bws = [b for b in np.atleast_1d(iq.bandwidth) if b > 0]
        npre = int(min(8 / (2 * math.pi * min(bws)) if bws else 0.0, 0.5) / dt) + 1
        tf = np.concatenate([ts[0] - np.arange(npre, 0, -1) * dt, ts])
        src = {"asg0": rp.asg0.raw(tf), "asg1": rp.asg1.raw(tf)}
        x = src.get(iq.input, np.zeros(len(tf)))
        if iq.acbandwidth > 0:
            a = math.exp(-2 * math.pi * iq.acbandwidth * dt)
            x = lfilter([a, -a], [1, -a], x)
        y = x * (math.cos(math.radians(iq.phase)) if iq.frequency == 0 else 0.0)
        for b in bws:
            a = 1 - math.exp(-2 * math.pi * b * dt)
            y = lfilter([a], [1, -(1 - a)], y)
        y = y * iq.quadrature_factor
        u = y if p2.input == "iq1" else src.get(p2.input, np.zeros(len(tf)))
        u = u - p2.setpoint
        out = np.clip(p2.p * u + p2.ival + 2 * math.pi * p2.i * np.cumsum(u) * dt, p2.min_voltage, p2.max_voltage)
        return y[npre:], out[npre:]

    # scope traces
    def traces(self, names, duration, trigger, t_start=None):
        """Scope data. Triggered on asg0/asg1: computed analytically. Untriggered: the recorded window
        [t_start, t_start + duration] of the simulation (the real scope records after it is armed).
        Intensity signals (in2, pid2, out2, asg1) come from the AOM model for the same window."""
        n = 2 ** 14
        trel = np.arange(n) * duration / n
        if self.plant == "errin":
            return self.traces_errin(names, duration, trigger, t_start, n, trel)
        li = self.li_name
        cav_names = {"in1", li, li + "_2", "asg0", "pid0", "out1", "iq1", "pid1"}
        with self.lock:
            asg = self.rp.asg0
            now = time.time()
            sig = {}
            if trigger in ("asg0", "asg1"):
                g = asg if trigger == "asg0" else self.rp.asg1
                per = 1 / g.frequency
                t0 = g.t0 + math.ceil((now - g.t0) / per) * per
                ready_at = t0 + duration
            else:
                t0 = None
                ready_at = now + duration          # an untriggered trace still takes its duration to record
            if trigger == "asg0" and asg.output_direct != "off":
                ts = t0 + trel
                p0 = self.rp.pid0
                po = self.pzt_out
                if po == "out2":
                    pid_out = float(np.clip(p0.p * self.ext_v + p0.ival, p0.min_voltage, p0.max_voltage)) \
                        if (p0.output_direct in ("out2", "both") and p0.input == "in2") else 0.0
                else:
                    pid_out = p0.ival if p0.output_direct in ("out1", "both") else 0.0
                v = asg.value(ts, po) + pid_out
                bws = [b for b in np.atleast_1d(self.li.bandwidth) if b > 0]
                pre = 6 * 4 / (2 * math.pi * max(min(bws), 1)) if bws else 0.0
                npre = int(pre / (duration / n)) + 1
                tp = t0 - np.arange(npre, 0, -1) * duration / n
                vfull = np.concatenate([asg.value(tp, po) + pid_out, v])
                y = self.err_raw(vfull)
                dt = duration / n
                for a in self.lpf_coeffs(dt):
                    y = lfilter([a], [1, -(1 - a)], y)
                err = y[npre:] + NOISE_ERR * self.rng.standard_normal(n)
                if li == "iq2":
                    y = self.err_raw(vfull, 1)
                    for a in self.lpf_coeffs(dt):
                        y = lfilter([a], [1, -(1 - a)], y)
                    sig[li + "_2"] = y[npre:] + NOISE_ERR * self.rng.standard_normal(n)
                pd = self.pd_avg(v) + NOISE_PD * self.rng.standard_normal(n)
                d = self.dither_pp() / 2
                sig.update({"in1": pd, li: err, "asg0": asg.raw(ts), "pid0": np.full(n, pid_out),
                            po: v + d * np.sin(2 * np.pi * self.li.frequency * ts)})
                if po == "out2":
                    p1 = self.rp.pid1
                    src = sig.get(p1.input) if p1.input in (li, li + "_2") else None
                    sig["out1"] = np.clip(p1.p * src + p1.ival, p1.min_voltage, p1.max_voltage) \
                        if (p1.output_direct in ("out1", "both") and src is not None) else np.zeros(n)
                    sig["in2"] = np.full(n, self.ext_v)
            elif cav_names & set(names) or t0 is None:
                h = np.array(self.hist[-int((self.t - (t_start or self.t) + duration) / DT) - 4:])
                tt = h[:, 0]
                t_end = min(tt[-1], (t_start or tt[-1] - duration) + duration)
                if t0 is None:
                    t0 = t_end - duration
                ts = np.linspace(t_end - duration, t_end, n)
                d = self.dither_pp() / 2
                drive = np.interp(ts, tt, h[:, 1])
                sig.update({"in1": np.interp(ts, tt, h[:, 2]), li: np.interp(ts, tt, h[:, 3]),
                            li + "_2": np.interp(ts, tt, h[:, 7]),
                            "pid0": np.interp(ts, tt, h[:, 4]), "asg0": asg.raw_rel(t0, trel),
                            self.pzt_out: drive + d * np.sin(2 * np.pi * self.li.frequency * ts)})
                if self.pzt_out == "out2":
                    sig["out1"] = np.interp(ts, tt, h[:, 5])
                    sig["in2"] = np.interp(ts, tt, h[:, 6])
            if "iq1" in names or "pid1" in names:
                sig["iq1"], sig["pid1"] = self.copies(t0 + trel)
            if self.pzt_out == "out2":                  # errsig wiring: no AOM on OUT2; IN2 = the external servo
                sig.setdefault("asg1", self.rp.asg1.raw_rel(t0, trel))
            else:
                sig.update(self.aom.synth(t0, trel))
        return [sig.get(nm, np.zeros(n)) for nm in names], ready_at


def _traces_errin(self, names, duration, trigger, t_start, n, trel):
    """pi_app plant: in1 = external error, out1 = asg0 + pid0, pid0, asg0."""
    with self.lock:
        asg, now = self.rp.asg0, time.time()
        sig = {}
        if trigger == "asg0" and asg.output_direct != "off":
            per = 1 / asg.frequency
            t0 = asg.t0 + math.ceil((now - asg.t0) / per) * per
            ts = t0 + trel
            v = asg.value(ts, "out1") + self.pid_out
            sig.update({"in1": self.ext_err(v) + NOISE_ERR * self.rng.standard_normal(n), "out1": v,
                        "asg0": asg.raw(ts), "pid0": np.full(n, self.pid_out)})
            ready_at = t0 + duration
        else:
            h = np.array(self.hist[-int((self.t - (t_start or self.t) + duration) / DT) - 4:])
            tt = h[:, 0]
            t_end = min(tt[-1], (t_start or tt[-1] - duration) + duration)
            ts = np.linspace(t_end - duration, t_end, n)
            sig.update({"in1": np.interp(ts, tt, h[:, 2]) + NOISE_ERR * self.rng.standard_normal(n),
                        "out1": np.interp(ts, tt, h[:, 1]), "pid0": np.interp(ts, tt, h[:, 4]),
                        "asg0": asg.raw_rel(t_end - duration, trel)})
            ready_at = now + duration
    return [sig.get(nm, np.zeros(n)) for nm in names], ready_at


Engine.traces_errin = _traces_errin


class Scope:
    durations = [8e-9 * 2 ** k * 2 ** 14 for k in range(17)]

    def __init__(self, eng):
        object.__setattr__(self, "eng", eng)
        for k, v in dict(input1="in1", input2="in2", duration=self.durations[10], trigger_source="immediately",
                         trigger_delay=0.0, average=True, rolling_mode=False, _data=None, _ready_at=0.0,
                         _arm=None).items():
            object.__setattr__(self, k, v)

    _name = "scope"

    def __setattr__(self, k, v):
        if not k.startswith("_"):
            lan()                                        # a register write on the real board
            if k != "owner" and _src() is not None:
                WRITE_N[0] += 1
                if len(WRITES) < 400000:
                    WRITES.append((time.time(), "scope", k, v if isinstance(v, (int, float, str)) else repr(v),
                                   _src()))
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
        lan()
        return self.eng.hist[-1][2] if self.eng.hist else 0.0

    @property
    def in2(self):
        lan()
        if self.eng.pzt_out == "out2":
            return self.eng.ext_v + 0.0005 * self.eng.rng.standard_normal()
        return self.eng.aom.sample_pd()

    @property
    def pid2(self):
        lan()
        p2 = self.eng.rp.pid2
        return float(np.clip(p2.ival, p2.min_voltage, p2.max_voltage))


LPF_LADDER = [125e6 / (2 * math.pi) / 2 ** k for k in range(1, 30)]
AC_LADDER = [0] + [int(2.371593461809983 * 2 ** n) for n in range(1, 27)]


def nearest_log(v, ladder):
    pos = [x for x in ladder if x > 0]
    return min(pos, key=lambda x: abs(math.log(x / v)))


class IQ(Mod):
    """FPGA-like rounding: factor-2 filter ladder, 2 filter stages (iq2: 4), integer quadrature factor."""
    @property
    def bandwidth(self):
        return self._bw

    @bandwidth.setter
    def bandwidth(self, v):
        st = getattr(self, "_stages", 2)
        v = [b for b in np.atleast_1d(v)][:st]
        self._bw = [nearest_log(b, LPF_LADDER) if b > 0 else 0 for b in v] + [0] * (st - len(v))

    @property
    def acbandwidth(self):
        return self._ac

    @acbandwidth.setter
    def acbandwidth(self, v):
        self._ac = 0 if v <= 0 else nearest_log(v, AC_LADDER)

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
        for nm, iq in (("iq0", self.iq0), ("iq1", self.iq1), ("iq2", self.iq2)):
            object.__setattr__(iq, "_name", nm)
            object.__setattr__(iq, "_stages", 4 if nm == "iq2" else 2)
            iq.input, iq.frequency, iq.amplitude, iq.phase = "in1", 1000.0, 0.0, 0.0
            iq.bandwidth, iq.acbandwidth, iq.gain, iq.quadrature_factor = [250.0, 250.0], 100.0, 0.0, 1.0
            iq.output_signal, iq.output_direct = "quadrature", "off"
        self.pid0, self.pid1, self.pid2 = PID("pid0"), PID("pid1"), PID("pid2")
        self.pid0.ival = 0.0
        self.asg0 = ASG(None, "asg0")
        self.asg1 = ASG(None, "asg1")
        self.eng = Engine(self)
        self.scope = Scope(self.eng)
        self.sampler = Sampler(self.eng)


class NetworkAnalyzer(Mod):
    """Model transfer functions of the fake loop (plant = error slope x demodulation low-pass)."""
    _name = "networkanalyzer"

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
        _BUILDING[0] = True
        try:
            self.rp = RP()
            self.networkanalyzer = NetworkAnalyzer(self.rp)
        finally:
            _BUILDING[0] = False
