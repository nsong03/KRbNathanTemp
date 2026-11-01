#!/usr/bin/env python3
"""
Dither (transmission) lock: SR810 output when the laser sweep crosses several cavity peaks.

Model
-----
laser   nu(t) = nu_ramp(t) + a cos(2 pi f_m t)            current dither; quasi-static since f_m << kappa
cavity  T(nu) = sum_i h_i / (1 + (2 (nu - nu_i)/kappa)^2)  modes add in power on a large-area PD
PD      V(t)  = V_PK * T(nu(t)) + white noise
SR810   X     = LPF^N [ V(t) * sqrt(2) cos(2 pi f_m t) ]   rms-calibrated PSD, reference phase already set
        CH1 OUTPUT = 10 V * X / SENS                       (Output key = X, offset off, expand x1)

All detunings are in units of the cavity FWHM kappa.  The time-domain trace is checked against the
closed-form quasi-static result  X = sqrt(2) V_PK sum_i h_i H1(2(det - c_i), 2a),
H1(x, m) = Im[ z / sqrt(z^2 - m^2) ] / m,  z = x + i.

Outputs: multipeak_error_signal.png, lockpoint_shift_vs_separation.png
"""
import numpy as np
import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import lfilter

# ----------------------------- parameters (edit these) -----------------------------
KAPPA_MHZ = 40.0            # cavity FWHM: 12 GHz FSR / finesse ~300
F_MOD = 500.0               # dither frequency [Hz]
TAU = 3e-3                  # lock-in time constant [s]
N_POLES = 4                 # 24 dB/oct
V_PK = 1.0                  # PD volts at the top of the tallest peak, no dither
SENS = 0.5                  # SR810 sensitivity [V rms full scale]
FS = 50e3                   # simulation sample rate [Hz]
SWEEP_S_PER_KAPPA = 0.25    # slow ramp: seconds to cross one linewidth
PD_NOISE_V = 3e-3           # white PD noise, V rms per sample (~19 uV/rtHz at FS)
A_OPT = 1 / (2 * np.sqrt(2))  # slope-optimal dither: peak deviation = 0.354 kappa
SEED = 1

SCENARIOS = [
    dict(title="A. Well separated (5$\\kappa$ apart)",
         centers=[0.0, 5.0, 10.0], heights=[1.0, 0.35, 0.15], a=A_OPT, span=(-3.0, 13.0)),
    dict(title="B. Overlapping (~1$\\kappa$ apart)",
         centers=[0.0, 1.2, 2.2], heights=[1.0, 0.5, 0.3], a=A_OPT, span=(-3.0, 5.5)),
    dict(title="C. 2.5$\\kappa$ apart, dither too big (a = 1.5$\\kappa$)",
         centers=[0.0, 2.5, 5.0], heights=[1.0, 0.5, 0.25], a=1.5, span=(-4.0, 9.0),
         compare_a=A_OPT),
]


# ----------------------------- physics -----------------------------
def transmission(det, centers, heights):
    det = np.asarray(det)[..., None]
    return np.sum(np.asarray(heights) / (1 + (2 * (det - np.asarray(centers))) ** 2), axis=-1)


def h1_single(x, m):
    """First cosine-Fourier coefficient of a unit Lorentzian under dither depth m (half-linewidth units)."""
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


def x_analytic(det, centers, heights, a):
    """Quasi-static SR810 X [V rms]."""
    out = np.zeros_like(np.asarray(det, dtype=float))
    for c, h in zip(centers, heights):
        out += h * h1_single(2 * (det - c), 2 * a)
    return np.sqrt(2) * V_PK * out


def simulate_sweep(centers, heights, a, span, rng):
    det0, det1 = span
    t_sweep = (det1 - det0) * SWEEP_S_PER_KAPPA
    t = np.arange(0.0, t_sweep, 1 / FS)
    det_ramp = det0 + (det1 - det0) * t / t_sweep
    det_inst = det_ramp + a * np.cos(2 * np.pi * F_MOD * t)
    pd = V_PK * transmission(det_inst, centers, heights) + PD_NOISE_V * rng.standard_normal(t.size)
    y = pd * np.sqrt(2) * np.cos(2 * np.pi * F_MOD * t)
    alpha = 1 - np.exp(-1 / (FS * TAU))
    for _ in range(N_POLES):
        y = lfilter([alpha], [1, -(1 - alpha)], y)
    return dict(t=t, det=det_ramp, pd=pd, x=y, ch1=np.clip(10 * y / SENS, -10.9, 10.9))


def zero_crossings(det, y):
    """Return list of (position, stable?) from sign changes. With the servo sign chosen to lock to a
    transmission peak, a crossing is stable when dX/d(det) < 0 there."""
    s = np.sign(y)
    idx = np.where(np.diff(s) != 0)[0]
    out = []
    for i in idx:
        x0 = det[i] - y[i] * (det[i + 1] - det[i]) / (y[i + 1] - y[i])
        out.append((x0, y[i + 1] < y[i]))
    return out


# ----------------------------- figure 1: sweeps -----------------------------
def figure_sweeps(path):
    rng = np.random.default_rng(SEED)
    fig, axes = plt.subplots(2, 3, figsize=(17, 8.2), sharex="col",
                             gridspec_kw=dict(height_ratios=[1, 1.5], hspace=0.08, wspace=0.18))
    report = []
    for col, sc in enumerate(SCENARIOS):
        c, h, a, span = sc["centers"], sc["heights"], sc["a"], sc["span"]
        sim = simulate_sweep(c, h, a, span, rng)
        det_fine = np.linspace(*span, 40001)
        xa = x_analytic(det_fine, c, h, a)

        ax = axes[0, col]
        ax.plot(sim["det"], sim["pd"], color="#7aa6d6", lw=0.3, alpha=0.6, rasterized=True,
                label="PD (with 500 Hz dither)")
        ax.plot(det_fine, V_PK * transmission(det_fine, c, h), color="#1f3b63", lw=1.4,
                label="cavity transmission, no dither")
        for cc in c:
            ax.axvline(cc, color="0.6", ls=":", lw=1)
        ax.set_title(sc["title"], fontsize=12)
        ax.set_ylim(-0.05, 1.12 * V_PK)
        if col == 0:
            ax.set_ylabel("photodiode [V]")
            ax.legend(loc="upper right", fontsize=8.5, frameon=False)
        sec = ax.secondary_xaxis("top", functions=(lambda d: d * KAPPA_MHZ, lambda f: f / KAPPA_MHZ))
        sec.set_xlabel(f"detuning [MHz]  ($\\kappa$ = {KAPPA_MHZ:g} MHz)", fontsize=9)

        ax = axes[1, col]
        ax.axhline(0, color="0.3", lw=0.8)
        if "compare_a" in sc:
            ax.plot(det_fine, 10 * x_analytic(det_fine, c, h, sc["compare_a"]) / SENS, color="0.6",
                    lw=1.2, label=f"same peaks, optimal dither a = {sc['compare_a']:.2f}$\\kappa$")
        ax.plot(sim["det"], sim["ch1"], color="#c0392b", lw=1.3, label="SR810 CH1 OUTPUT (time-domain sim)")
        ax.plot(det_fine, 10 * xa / SENS, color="k", ls="--", lw=0.9, label="quasi-static closed form")
        for cc in c:
            ax.axvline(cc, color="0.6", ls=":", lw=1)

        zcs = zero_crossings(det_fine, xa)
        stable = [z for z, st in zcs if st]
        unstable = [z for z, st in zcs if not st]
        ax.plot(stable, np.zeros(len(stable)), "o", ms=9, mfc="#27ae60", mec="k", zorder=5,
                label="stable lock point")
        ax.plot(unstable, np.zeros(len(unstable)), "o", ms=8, mfc="white", mec="#c0392b", mew=1.8,
                zorder=5, label="unstable zero crossing")

        lines = []
        for i, cc in enumerate(c):
            near = [z for z in stable if abs(z - cc) < 0.5]
            if near:
                off = (near[0] - cc) * KAPPA_MHZ
                lines.append(f"peak {i+1} (h={h[i]:g}): lock offset {off:+.2f} MHz")
            else:
                lines.append(f"peak {i+1} (h={h[i]:g}): NO lock point")
        report.append((sc["title"], lines, zcs))
        ax.text(0.98, 0.04, "\n".join(lines), transform=ax.transAxes, ha="right", va="bottom",
                fontsize=8.8, family="monospace",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="0.75", alpha=0.92))
        ax.set_xlabel("laser detuning from tallest peak [units of $\\kappa$]")
        ax.set_ylim(-7, 7)
        if col == 0:
            ax.set_ylabel(f"CH1 OUTPUT [V]  (sens = {SENS*1e3:g} mV)")
        ax.legend(loc="upper right", fontsize=8, frameon=False)

    fig.suptitle(f"Dither-lock error signal across 3 cavity peaks:  f_m = {F_MOD:g} Hz,  "
                 f"$\\tau$ = {TAU*1e3:g} ms, {6*N_POLES} dB/oct,  sweep {SWEEP_S_PER_KAPPA*1e3:g} ms per $\\kappa$",
                 fontsize=13, y=1.0)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return report


# ----------------------------- figure 2: lock-point pull vs separation -----------------------------
def lock_points_two_peaks(sep, h, a=A_OPT):
    """Stable lock points for a unit peak at 0 plus a neighbor of height h at +sep."""
    lo, hi = -1.0, sep + 1.0
    det = np.linspace(lo, hi, int((hi - lo) * 4000) + 1)
    y = x_analytic(det, [0.0, sep], [1.0, h], a)
    return [z for z, st in zero_crossings(det, y) if st]


def figure_shift(path):
    seps = np.geomspace(0.6, 60, 220)
    hs = [0.03, 0.1, 0.3, 1.0]
    colors = ["#8e44ad", "#2980b9", "#16a085", "#d35400"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.4), gridspec_kw=dict(wspace=0.32))
    k_asym = (1 + (2 * A_OPT) ** 2) ** 1.5 / 16  # 0.1148
    summary = {}
    for h, col in zip(hs, colors):
        main_shift, weak_shift, weak_seps = [], [], []
        for s in seps:
            st = lock_points_two_peaks(s, h)
            main = min(st, key=abs)
            main_shift.append(abs(main))
            if len(st) >= 2:
                weak = min(st, key=lambda z: abs(z - s))
                weak_shift.append(abs(weak - s))
                weak_seps.append(s)
        main_shift = np.array(main_shift)
        a1.loglog(seps, main_shift, color=col, lw=2, label=f"neighbor height h = {h:g}")
        a1.loglog(seps, k_asym * h / seps ** 3, color=col, ls=":", lw=1)
        if h < 1:
            a2.loglog(weak_seps, weak_shift, color=col, lw=2, label=f"h = {h:g}")
            if weak_seps and weak_seps[0] > seps[0]:
                a2.plot(weak_seps[0], weak_shift[0], "x", color=col, ms=10, mew=2.5)
                summary[h] = weak_seps[0]
    for ax in (a1, a2):
        ax.axvline(34, color="0.4", ls="--", lw=1)
        ax.text(34 / 1.06, 0.62, "TEM$_{00}$$\\to$TEM$_{01}$\nin your cavity\n(1.36 GHz = 34$\\kappa$)",
                fontsize=8.5, va="center", ha="right", transform=ax.get_xaxis_transform())
        ax.set_xticks([1, 2, 5, 10, 20, 50])
        ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%g"))
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.set_xlabel("separation to neighboring peak [units of $\\kappa$]")
        ax.grid(True, which="both", alpha=0.25)
        sec = ax.secondary_yaxis("right", functions=(lambda v: v * KAPPA_MHZ * 1e6,
                                                     lambda v: v / (KAPPA_MHZ * 1e6)))
        sec.set_ylabel(f"[Hz] for $\\kappa$ = {KAPPA_MHZ:g} MHz")
        ax.set_ylim(1e-9, 1)
    a1.set_ylabel("lock-point offset [units of $\\kappa$]")
    a1.set_title("Pull on the TALL peak's lock point\n(dotted: 0.115·h·$\\kappa$·($\\kappa$/sep)$^3$)", fontsize=11)
    a1.legend(fontsize=9, loc="lower left")
    a2.set_ylabel("lock-point offset [units of $\\kappa$]")
    a2.set_title("Pull on the WEAK peak's own lock point\n(× = closer than this, it has no lock point at all)",
                 fontsize=11)
    a2.legend(fontsize=9, loc="lower left")
    fig.suptitle("How much a neighboring peak shifts the lock point (optimal dither, a = 0.35$\\kappa$)",
                 fontsize=13, y=1.04)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return summary


if __name__ == "__main__":
    import os
    outdir = os.path.dirname(os.path.abspath(__file__))
    rep = figure_sweeps(os.path.join(outdir, "multipeak_error_signal.png"))
    for title, lines, zcs in rep:
        print(title)
        for ln in lines:
            print("   ", ln)
        print("    all crossings:", [(round(z, 4), "S" if s else "U") for z, s in zcs])
    vanish = figure_shift(os.path.join(outdir, "lockpoint_shift_vs_separation.png"))
    print("weak peak loses its lock point below separation (kappa):",
          {h: round(s, 2) for h, s in vanish.items()})
    print("apparent sweep lag of zero crossings: %.3f kappa (= N*tau / sweep time per kappa)"
          % (N_POLES * TAU / SWEEP_S_PER_KAPPA))
