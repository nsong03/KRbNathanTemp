#!/usr/bin/env python3
"""Plot the DG922 arb file for verification and predict what the SR810 will show with this sweep."""
import os
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import lfilter
from make_dg922_arb import make_waveform

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# usage: python verify_dg922_arb.py [F_TRI F_SIN]
F_TRI, F_SIN = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) == 3 else (30, 510)
VPP_TRI, VPP_SIN = 0.020, 0.004
GENERIC = len(sys.argv) == 3

# assumption for the lock-in prediction: the 4 mVpp dither is the slope-optimal depth (0.354 kappa peak)
A_OPT = 1 / (2 * np.sqrt(2))
KAPPA_V = (VPP_SIN / 2) / A_OPT          # volts of drive per cavity linewidth (~5.7 mV)
V_PK, SENS = 1.0, 0.5                     # PD volts on resonance, SR810 sensitivity


def h1(x, m):
    z = x + 1j
    return np.imag(z / (np.sqrt(z - m) * np.sqrt(z + m))) / m


def lockin(v_drive, ref, fs, tau, poles=4):
    det = v_drive / KAPPA_V
    pd = V_PK / (1 + (2 * det) ** 2)
    y = pd * np.sqrt(2) * ref
    alpha = 1 - np.exp(-1 / (fs * tau))
    for _ in range(poles):
        y = lfilter([alpha], [1, -(1 - alpha)], y)
    return 10 * y / SENS


def main():
    w = make_waveform(F_TRI, VPP_TRI, F_SIN, VPP_SIN)
    t, v, tri = w["t"], w["v"], w["tri"]
    T, n = w["T"], t.size
    fs = n / T
    ms, mv = 1e3, 1e3

    fig = plt.figure(figsize=(15, 13.5))
    gs = fig.add_gridspec(4, 2, height_ratios=[1.1, 1, 1, 1.25], hspace=0.42, wspace=0.18)

    # (a) full period
    ax = fig.add_subplot(gs[0, :])
    ax.plot(t * ms, v * mv, color="#1f5fa8", lw=0.9, label="arb file (triangle + sine)")
    ax.plot(t * ms, tri * mv, color="k", ls="--", lw=0.9, label=f"{F_TRI} Hz triangle alone")
    ax.set_xlim(0, T * ms)
    ax.set_xlabel("time [ms]")
    ax.set_ylabel("output [mV]")
    ax.set_title(f"(a) One file period = {T*ms:.3f} ms  ({w['n_tri']} triangle, {w['n_sin']} sine cycles, "
                 f"{n} pts)  —  set Arb Freq = {w['f_rep']} Hz, HighZ;  file pp = {np.ptp(v)*mv:.2f} mVpp",
                 fontsize=11)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)

    # (b) zoom at the triangle peak
    ax = fig.add_subplot(gs[1, 0])
    sel = (t > T / 2 - 3e-3) & (t < T / 2 + 3e-3)
    ax.plot(t[sel] * ms, v[sel] * mv, color="#1f5fa8", lw=1.2)
    ax.plot(t[sel][::40] * ms, v[sel][::40] * mv, "o", ms=2.5, color="#1f5fa8")
    ax.plot(t[sel] * ms, tri[sel] * mv, "k--", lw=0.9)
    ax.set_title(f"(b) Zoom at the turnaround: {VPP_SIN*mv:g} mVpp, {F_SIN} Hz sine on the ramp\n"
                 "(dots = every 40th file point)", fontsize=10.5)
    ax.set_xlabel("time [ms]")
    ax.set_ylabel("output [mV]")
    ax.grid(alpha=0.25)

    # (c) loop seam
    ax = fig.add_subplot(gs[1, 1])
    k = int(2e-3 * fs)
    seam_v = np.concatenate([v[-k:], v[:k]])
    seam_t = (np.arange(-k, k)) / fs
    ax.plot(seam_t * ms, seam_v * mv, color="#1f5fa8", lw=1.2)
    ax.plot(seam_t[::20] * ms, seam_v[::20] * mv, "o", ms=2.5, color="#1f5fa8")
    ax.axvline(0, color="#c0392b", ls=":", lw=1.5)
    ax.text(0.03, 0.9, f"end of file → start of file\njump at seam {abs(v[0]-v[-1])*1e6:.1f} µV "
            f"(normal step ≤ {np.abs(np.diff(v)).max()*1e6:.1f} µV)", transform=ax.transAxes,
            fontsize=9, va="top")
    ax.set_title("(c) Loop seam: last 2 ms + first 2 ms (should be seamless)", fontsize=10.5)
    ax.set_xlabel("time relative to wrap [ms]")
    ax.set_ylabel("output [mV]")
    ax.grid(alpha=0.25)

    # (d) spectrum (exact bins: file is periodic)
    ax = fig.add_subplot(gs[2, :])
    spec = np.abs(np.fft.rfft(v)) * 2 / n
    freqs = np.fft.rfftfreq(n, d=1 / fs)
    fmax = max(1500, 2.5 * F_SIN)
    m = freqs <= fmax
    ml, sl, bl = ax.stem(freqs[m], spec[m] * mv, basefmt=" ")
    plt.setp(sl, lw=1.0, color="#1f5fa8")
    plt.setp(ml, ms=3, color="#1f5fa8")
    ax.set_yscale("log")
    ax.set_ylim(1e-6, 30)
    ax.set_xlim(0, fmax)
    i510 = np.argmin(abs(freqs - F_SIN))
    nh = F_SIN / F_TRI
    extra = (f"\n(includes triangle's {int(nh)}th harmonic, {8*VPP_TRI/2/(np.pi**2*nh**2)*1e6:.0f} µV)"
             if nh == int(nh) and int(nh) % 2 == 1 else "\n(triangle has no harmonic here)")
    ax.annotate(f"{F_SIN} Hz dither: {spec[i510]*mv:.2f} mV pk" + extra,
                xy=(F_SIN, spec[i510] * mv), xytext=(F_SIN * 1.25, 3), fontsize=9,
                arrowprops=dict(arrowstyle="->", color="0.3"))
    ax.set_title(f"(d) Spectrum: triangle odd harmonics ({F_TRI}, {3*F_TRI}, {5*F_TRI} … Hz, falling as 1/n²) "
                 "+ one clean dither line; no leakage → loop closes", fontsize=10.5)
    ax.set_xlabel("frequency [Hz]")
    ax.set_ylabel("amplitude [mV pk]")
    ax.grid(alpha=0.25, which="both")

    # (e) predicted SR810 CH1 OUTPUT, plotted against sweep phase so different sweep rates overlay
    ax = fig.add_subplot(gs[3, :])
    reps = 5

    def run(f_tri, f_dith, tau):
        wv = make_waveform(F_TRI if f_tri is None else f_tri, VPP_TRI, f_dith, VPP_SIN)
        nn, TT = wv["t"].size, wv["T"]
        fss = nn / TT
        tt = np.arange(reps * nn) / fss
        x = lockin(np.tile(wv["v"], reps), np.sin(2 * np.pi * f_dith * tt), fss, tau)
        return wv["t"] / TT, x[(reps - 1) * nn:]

    ph = t / T
    x_ideal = 10 * np.sqrt(2) * V_PK * h1(2 * tri / KAPPA_V, 2 * A_OPT) / SENS
    if GENERIC:
        runs = [(F_TRI, F_SIN, tau, col, 2.0, 1.0,
                 f"this file ({F_TRI} Hz sweep, {F_SIN} Hz dither), τ = {tau*1e3:g} ms")
                for tau, col in [(3e-3, "#7f8c8d"), (1e-3, "#c0392b"), (0.3e-3, "#27ae60")]]
    else:
        runs = [
            (30, F_SIN, 0.3e-3, "#e67e22", 1.0, 0.55, f"THIS FILE (30 Hz sweep, {F_SIN} Hz dither), τ = 0.3 ms"),
            (30, F_SIN, 1e-3, "#c0392b", 2.0, 1.0, f"THIS FILE (30 Hz sweep, {F_SIN} Hz dither), τ = 1 ms"),
            (2, F_SIN, 1e-3, "#2471a3", 2.0, 1.0, f"tri2Hz_sin{F_SIN}Hz.csv (2 Hz sweep), τ = 1 ms"),
            (30, 335 * 30, 100e-6, "#27ae60", 2.0, 1.0, "30 Hz sweep with 10.05 kHz dither, τ = 100 µs"),
        ]
    for f_tri, f_d, tau, col, lw, alpha, lab in runs:
        p, x = run(f_tri, f_d, tau)
        ax.plot(p, x, color=col, lw=lw, alpha=alpha, label=lab)
    ax.plot(ph, x_ideal, "k--", lw=1.6, zorder=5, label="ideal error signal (quasi-static)")
    ax.axhline(0, color="0.4", lw=0.8)
    for xp, s in [(0.25, "resonance (up-sweep)"), (0.75, "resonance (down-sweep)")]:
        ax.axvline(xp, color="0.6", ls=":", lw=1)
        ax.text(xp, 7.3, " " + s, fontsize=9, color="0.35")
    ax.set_xlim(0, 1)
    ax.set_ylim(-8, 8)
    ax.set_xlabel("fraction of sweep period")
    ax.set_ylabel("CH1 OUTPUT [V]")
    ax.set_title(f"(e) Predicted SR810 output — assumes {VPP_SIN*mv:g} mVpp is the optimal dither "
                 f"(κ ≈ {KAPPA_V*mv:.1f} mV of drive), resonance at mid-sweep, 24 dB/oct", fontsize=10.5)
    ax.legend(loc="lower left", fontsize=8.8, ncol=2)
    ax.grid(alpha=0.25)

    path = os.path.join(HERE, f"check_tri{F_TRI}Hz_sin{F_SIN}Hz.png" if GENERIC else "dg922_arb_check.png")
    fig.savefig(path, dpi=140, bbox_inches="tight")
    print("saved", path, "| kappa in drive volts:", KAPPA_V)


if __name__ == "__main__":
    main()
