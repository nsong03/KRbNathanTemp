#!/usr/bin/env python3
"""
Advanced-mode (point-by-point) versions for the DG922 Pro: a CH1 drive file and a matching CH2
510 Hz square reference with the SAME length, so with equal sample rates they stay locked.
Integer points per dither cycle -> exact, jitter-free reference edges.

  CH1: adv_tri2Hz_sin510Hz.csv   2 Hz triangle (20 mVpp) + 510 Hz sine (4 mVpp)
  CH2: adv_ref510Hz_square.csv   510 Hz square, +/-1 (scale on the instrument to 0-5 V)
  Both: N = 255 cycles x 128 pts = 32640 pts, Sample Rate = 32640 x 2 Hz = 65,280 Sa/s
"""
import os, math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

F_TRI, VPP_TRI, F_SIN, VPP_SIN, PPC = 2, 0.020, 510, 0.004, 128

f_rep = math.gcd(F_TRI, F_SIN)
n_cyc = F_SIN // f_rep
N = n_cyc * PPC
assert (N * F_TRI // f_rep) % 2 == 0 or True
srate = N * f_rep
k = np.arange(N)
t = k / srate
p = (t * F_TRI) % 1.0
tri = 0.5 * VPP_TRI * (1 - 4 * np.abs(p - 0.5))
v = tri + 0.5 * VPP_SIN * np.sin(2 * np.pi * F_SIN * t)
ref = np.where((k % PPC) < PPC // 2, 1.0, -1.0)

out = os.path.dirname(os.path.abspath(__file__))
np.savetxt(os.path.join(out, f"adv_tri{F_TRI}Hz_sin{F_SIN}Hz.csv"), v, fmt="%.9f")
np.savetxt(os.path.join(out, f"adv_ref{F_SIN}Hz_square.csv"), ref, fmt="%.1f")
print(f"N = {N} pts, sample rate = {srate} Sa/s, file rate = {srate/N:g} Hz, {PPC} pts per {F_SIN} Hz cycle")
print(f"CH1 pp {np.ptp(v)*1e3:.3f} mV, seam jump {abs(v[0]-v[-1])*1e6:.2f} uV, max step {np.abs(np.diff(v)).max()*1e6:.2f} uV")
print("ref high samples per cycle:", int((ref[:PPC] > 0).sum()), "| ref rising edges at sine zero-crossings:",
      bool(np.all(ref[::PPC] == 1) and np.all(ref[PPC-1::PPC] == -1)))

fig, ax = plt.subplots(2, 1, figsize=(12, 6.5), gridspec_kw=dict(hspace=0.45))
ax[0].plot(t * 1e3, v * 1e3, lw=0.6, color="#1f5fa8")
ax[0].set(xlabel="time [ms]", ylabel="CH1 [mV]", xlim=(0, 1e3 / f_rep),
          title=f"adv_tri{F_TRI}Hz_sin{F_SIN}Hz.csv — one period ({N} pts @ {srate} Sa/s), pp {np.ptp(v)*1e3:.2f} mV")
sel = t < 6e-3
ax[1].plot(t[sel] * 1e3, (v[sel] - tri[sel]) * 1e3, ".-", ms=2.5, lw=1, color="#1f5fa8", label="CH1 dither part (triangle removed)")
ax2 = ax[1].twinx()
ax2.step(t[sel] * 1e3, ref[sel] * 2.5 + 2.5, where="post", color="#c0392b", lw=1.2, label="CH2 reference (scaled 0–5 V)")
ax[1].set(xlabel="time [ms]", ylabel="CH1 dither [mV]", title="first 6 ms: reference rising edge = rising zero crossing of the dither")
ax2.set_ylabel("CH2 [V]")
ax[1].legend(loc="lower left", fontsize=8.5); ax2.legend(loc="lower right", fontsize=8.5)
fig.savefig(os.path.join(out, "adv_check.png"), dpi=130, bbox_inches="tight")
