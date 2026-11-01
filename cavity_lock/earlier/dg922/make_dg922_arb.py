#!/usr/bin/env python3
"""
Arbitrary waveform for the Rigol DG922 Pro: slow triangle sweep + small sine dither, summed.

  v(t) = triangle(F_TRI, VPP_TRI) + (VPP_SIN/2) * sin(2 pi F_SIN t)

The file holds exactly ONE repetition period, T = 1/gcd(F_TRI, F_SIN), so it loops seamlessly:
  30 Hz + 510 Hz -> T = 1/30 s  (1 triangle, 17 sine cycles)   -> set Arb frequency = 30 Hz
  30 Hz + 500 Hz -> T = 1/10 s  (3 triangles, 50 sine cycles)  -> set Arb frequency = 10 Hz

DG900 Pro Continuous-mode Arb uses 16,384 points, no file header, voltage data (floating) in *.csv.
File names may only contain letters, digits and underscores (plus the extension).
The triangle starts at its minimum, so the CH1 sync output (high during the first half period)
marks the up-sweep when there is one triangle per file.
"""
import math
import os
import numpy as np

N_PTS = 16384          # DG900 Pro Continuous-mode Arb length


def make_waveform(f_tri=30, vpp_tri=0.020, f_sin=510, vpp_sin=0.004, n=N_PTS):
    f_rep = math.gcd(int(f_tri), int(f_sin))
    if f_rep <= 0 or f_tri != int(f_tri) or f_sin != int(f_sin):
        raise ValueError("use integer frequencies so the period closes exactly")
    T = 1.0 / f_rep
    t = np.arange(n) * (T / n)                # endpoint excluded -> seamless loop
    p = (t * f_tri) % 1.0
    tri = 0.5 * vpp_tri * (1 - 4 * np.abs(p - 0.5))   # min at p=0, max at p=0.5
    sin = 0.5 * vpp_sin * np.sin(2 * np.pi * f_sin * t)
    return dict(t=t, v=tri + sin, tri=tri, sin=sin, T=T, f_rep=f_rep,
                n_tri=int(f_tri // f_rep), n_sin=int(f_sin // f_rep))


def write_files(w, stem, outdir, txt=False):
    paths = []
    csv = os.path.join(outdir, stem + ".csv")
    np.savetxt(csv, w["v"], fmt="%.9f")       # one voltage per line, no header
    paths.append(csv)
    if txt:
        tx = os.path.join(outdir, stem + ".txt")
        with open(tx, "w") as f:
            f.write(",".join(f"{x:.9f}" for x in w["v"]))
        paths.append(tx)
    return paths


if __name__ == "__main__":
    import sys
    out = os.path.dirname(os.path.abspath(__file__))
    # usage: python make_dg922_arb.py [F_TRI F_SIN [TRI_mVpp SIN_mVpp]]
    #   e.g. python make_dg922_arb.py 10 1000            (20 mVpp triangle, 4 mVpp sine)
    #        python make_dg922_arb.py 10 1000 60 8.5     (custom amplitudes)
    tag = lambda x: f"{x:g}".replace(".", "p")          # DG922 file names: letters/digits/_ only
    if len(sys.argv) == 5:
        a_tri, a_sin = float(sys.argv[3]) * 1e-3, float(sys.argv[4]) * 1e-3
        jobs = [(int(sys.argv[1]), int(sys.argv[2]), False, a_tri, a_sin)]
    elif len(sys.argv) == 3:
        jobs = [(int(sys.argv[1]), int(sys.argv[2]), False, 0.020, 0.004)]
    else:
        # requested: 30 Hz / 20 mVpp triangle + 4 mVpp sine; alternative: same amplitudes, 2 Hz sweep
        jobs = [(30, 510, True, 0.020, 0.004), (2, 510, False, 0.020, 0.004)]
    for f_tri, f_sin, txt, a_tri, a_sin in jobs:
        w = make_waveform(f_tri=f_tri, vpp_tri=a_tri, f_sin=f_sin, vpp_sin=a_sin)
        stem = f"tri{f_tri}Hz_sin{f_sin}Hz"
        if len(sys.argv) == 5:
            stem = f"tri{f_tri}Hz_{tag(a_tri*1e3)}mV_sin{f_sin}Hz_{tag(a_sin*1e3)}mV"
        paths = write_files(w, stem, out, txt=txt)
        v = w["v"]
        steps = np.abs(np.diff(v))
        print(f"{stem}: period {w['T']*1e3:.3f} ms -> set Arb Freq {w['f_rep']} Hz; "
              f"{w['n_tri']} triangle + {w['n_sin']} sine cycles; {v.size} pts")
        print(f"   min {v.min()*1e3:+.4f} mV  max {v.max()*1e3:+.4f} mV  pp {np.ptp(v)*1e3:.4f} mV  "
              f"mean {v.mean()*1e6:+.3f} uV")
        print(f"   max step inside {steps.max()*1e6:.3f} uV, step across loop seam "
              f"{abs(v[0]-v[-1])*1e6:.3f} uV")
        print("   wrote", ", ".join(os.path.basename(p) for p in paths))
