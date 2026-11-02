# AOM intensity stabilization: hardware (not built yet)

The Red Pitaya's second channel stabilizes the power in the AOM's first-order beam: IN2 reads a pickoff photodiode,
`pid2` compares it with the setpoint, and OUT2 drives the RF power through an inverting bias driver and a mixer used
as a current-controlled attenuator. OUT2 is no longer the cavity lock's monitor output.

```
                      +4 dBm, 80 MHz (1 Vpp into 50 ohm)
                      ┌──────────────┐
                      │ 80 MHz source│──LO──┐
                      └──────────────┘      │      ≈ −1 dBm at full IF          ≤ ≈ +31 dBm
                                         ┌──▼─────────┐ RF  ┌──────────────┐   ┌─────┐  1st order  ┌──────────┐
 OUT2 ─┬─ 50 Ω tee                       │ ZX05-1+    ├────►│ ZHL-1-2W+    ├──►│ AOM ├────────────►│ pickoff  ├─► IN2
       │                                 │ mixer      │     │ 32 dB, +24 V │   └─────┘             │ PD (LV)  │  0.5–0.8 V
       │   ┌──────────────────────┐      └──▲─────────┘     └──────────────┘                       └──────────┘
       └──►│ R1 1 k               │  IF     │
           │        inverting     ├──►|─────┴── Rs 100 Ω   (Schottky, forward toward the mixer)
 bias pot ►│ R2 10 k   summer     │ V_drv = |V_bias| − 10·V_OUT2      (IF current ≤ ~25 mA; mixer limit 40 mA)
 0..−5 V   │ Rf 10 k  LM7171/AD817│
           └──────────────────────┘ ±5 V supply
```

## Parts and settings
| Part | What | Notes |
| --- | --- | --- |
| Photodiode | amplified Si photodiode on the AOM's 1st-order beam (pickoff) | ≥ a few MHz bandwidth; about 0.5–0.8 V at the operating point; Red Pitaya IN2 jumper on **LV** (±1 V) |
| OUT2 | Red Pitaya DAC, ±1 V | → 50 Ω tee (terminate the line) → driver input |
| Bias driver | inverting op-amp summer, LM7171 / AD817 class, ±5 V | R1 = 1 kΩ from OUT2, R2 = 10 kΩ from the bias pot (0 to −5 V), Rf = 10 kΩ → **V_drv = \|V_bias\| − 10·V_OUT2** |
| Current limit | Schottky diode (forward toward the mixer) + Rs = 100 Ω | positive IF current only; the driver limits it to about 25 mA (ZX05-1+ IF maximum 40 mA) |
| Mixer | Mini-Circuits ZX05-1+ | used as a current-controlled attenuator: IF current sets the RF amplitude; about −1 dBm RF out at full IF current with +4 dBm LO |
| RF source | external 80 MHz, 1 Vpp = +4 dBm into 50 Ω | → mixer LO port |
| RF amplifier | Mini-Circuits ZHL-1-2W+ | 32 dB gain, +24 V supply, **heat sink**, **never run without a load** |
| AOM | 80 MHz | about +31 dBm drive at most: **check the AOM's RF rating** (setting `int_aom_max_dbm`; the diagram warns above it) |

## What this means for the software
1. **More OUT2 gives less light** (the driver inverts). The plant gain K = dV_IN2/dV_OUT2 is negative; the app
   measures it with the power scan instead of assuming it. The PID rule is the cavity's: negative feedback needs
   i·K < 0, so with K < 0 the gains p and i come out positive.
2. **OUT2 = 0 V leaves the AOM at the bias level** (set by the pot). That is the safe default: at start-up the app
   resets OUT2 to 0 V (pid2 off, asg1 off) unless a servo is already running with non-zero gains (then it is
   adopted untouched).
3. Because of the ×10 driver gain, the useful OUT2 range is only about ±0.1–0.2 V. The app limits every scan and
   clamp to `int_out2_limit_mv` (default ±300 mV).
4. **Diffraction efficiency rises with RF power, peaks, then falls.** Past the peak K changes sign and the servo
   latches at full drive. The power scan finds the peak and puts the bright-side clamp where the light is
   (1 − margin) × peak (default margin 5 %) on the near side of the peak.
5. Expected loop bandwidth 100–200 kHz, limited by delays: Red Pitaya 0.21 µs, AOM acoustic transit about
   0.24 µs per mm (TeO2) of beam-to-transducer distance, plus the photodiode. A pure integrator with a total delay
   τ has 45° phase margin at f_u ≈ 1/(8τ): τ = 0.5 µs → about 250 kHz.
6. The PyRPL integrator register limits i to 38.9 kHz, so the integrator-limited bandwidth is at most
   38.9 kHz × |K| (the app shows this limit and warns). With the fake's |K| ≈ 9.5 V/V that is ≈ 370 kHz.

## Bring-up checklist (when it is built)
1. Amplifier: heat sink and load (AOM or a 50 Ω power attenuator) connected before +24 V.
2. OUT2 = 0 V (app start-up, Output off). Set the bias pot for a moderate light level; check the AOM RF power at
   the amplifier output with a power meter/attenuator against the AOM rating.
3. Photodiode on IN2 with the LV jumper; about 0.5–0.8 V at the intended operating point.
4. In the app's Intensity view: Output on, Scan on (loop open). Check the scan shows the efficiency peak or at
   least a monotonic rise, the clamp sits short of the peak, and K is negative.
5. Choose the setpoint below "max lockable", start with a modest bandwidth (e.g. 10 kHz), Lock, then raise the
   bandwidth while watching the model phase margin and the Bode plot.
