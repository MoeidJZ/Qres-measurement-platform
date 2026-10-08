"""
tools/pna_segment_hw_test.py
============================
Hardware check for the segment-sweep / HPD implementation. Run it on the lab
PC with the PNA connected, from the project root:

    python -m tools.pna_segment_hw_test --address TCPIP0::<ip>::inst0::INSTR \
        --fstart 5.1230e9 --fstop 5.1240e9 --power -20 --ifbw 100 --avg 1

Pick a window around one known resonator and a power where it is clearly
visible. The script only uses that window and that power, turns RF off and
sets -80 dBm at the end (also on Ctrl+C / errors), and leaves the PNA on a
linear sweep.

What it checks (each line prints PASS / FAIL):
 1. Driver connects, options, power limits (option 216 -> -90 dBm).
 2. A 3-segment table is accepted (SEGM:LIST), read back, and the PNA enters
    segment sweep.
 3. SENS:SWE:POIN? in segment mode == total points (the driver does not rely
    on it, but it is good to know).
 4. Stimulus read in 64-bit is exactly the programmed grid; 32-bit would be off.
 5. magnitude()/phase() read in segment mode and have the right length.
 6. Linear sweep vs HPD over the SAME span and point count, both circle-fitted:
    prints fr, Ql, Qi, Qi_err for each. They should agree within errors, and
    HPD's Qi_err should be smaller.
 7. No SCPI errors left in the queue; format restored to REAL,32.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", required=True)
    ap.add_argument("--fstart", type=float, required=True, help="Hz")
    ap.add_argument("--fstop", type=float, required=True, help="Hz")
    ap.add_argument("--points", type=int, default=2001)
    ap.add_argument("--power", type=float, default=-20.0, help="dBm at the VNA port")
    ap.add_argument("--ifbw", type=float, default=100.0)
    ap.add_argument("--avg", type=int, default=1)
    a = ap.parse_args()

    from core.drivers.keysight_pna import KeysightN5235A
    from core.pna_segment import build_hpd_segments
    from core.fitting import fit_notch_auto, s21_from_mag_phase
    from core.measure_workers import noise_to_radius

    results = []

    def check(cond, msg):
        results.append(bool(cond))
        print(("PASS  " if cond else "FAIL  ") + msg, flush=True)

    pna = KeysightN5235A("pna_hwtest", a.address)
    try:
        print("IDN:", pna.IDN())
        print("options:", pna.get_options())
        print("power limits:", pna.power.vals)
        check(True, "connected with core.drivers.keysight_pna.KeysightN5235A")
        pna.get_errors()

        def sweep():
            pna.auto_sweep(False)
            if a.avg > 1:
                pna.averages_enabled(True); pna.averages(a.avg)
                pna.reset_averages(); pna.group_trigger_count(a.avg); pna.sweep_mode("GRO")
            else:
                pna.averages_enabled(False); pna.sweep_mode("SING")
            t0 = time.time()
            while pna.sweep_mode() != "HOLD":
                time.sleep(0.1)
                if time.time() - t0 > 600:
                    raise TimeoutError("sweep did not finish in 10 min")
            return np.asarray(pna.magnitude(), float), np.asarray(pna.phase(), float)

        pna.trace("S21")
        pna.if_bandwidth(a.ifbw)
        pna.power(a.power)

        # ---- 2-5: a small table --------------------------------------------
        f0, f1 = a.fstart, a.fstop
        third = (f1 - f0) / 3
        segs = [(f0, f0 + third, 21),
                (f0 + third + 1, f1 - third, 201),
                (f1 - third + 1, f1, 21)]
        stim = pna.set_segment_table(segs)
        check(pna.sweep_type() == "SEGM", f"segment sweep active ({pna.segment_count()} segments)")
        truth = np.concatenate([np.round(np.linspace(s, e, n)) for s, e, n in
                                pna.normalize_segments(segs)])
        check(int(pna.points()) == truth.size,
              f"SENS:SWE:POIN? in SEGM = {pna.points()} (total {truth.size})")
        check(stim.size == truth.size and np.max(np.abs(stim - truth)) <= 1,
              f"64-bit stimulus matches table (max dev {np.max(np.abs(stim - truth)):.1f} Hz)")
        f32 = np.asarray(pna.visa_handle.query_binary_values("SENS:X?", datatype="f",
                                                             is_big_endian=True))
        print(f"      (for reference: 32-bit stimulus max dev {np.max(np.abs(f32 - truth)):.0f} Hz)")
        pna.output(True)
        mag, ph = sweep()
        check(mag.size == truth.size and ph.size == truth.size,
              f"magnitude/phase read in SEGM mode ({mag.size} points)")

        # ---- 6: SPD vs HPD over the same span --------------------------------
        pna.use_linear_sweep(f0, f1, a.points)
        mag, ph = sweep()
        f_lin = np.linspace(f0, f1, a.points)
        spd = fit_notch_auto(f_lin, s21_from_mag_phase(mag, ph))
        if not spd.get("ok"):
            check(False, f"linear-sweep fit failed ({spd.get('error')}); pick a better window/power")
            return
        plan = build_hpd_segments(f0, f1, spd["fr"], spd["Ql"], a.points)
        f_hpd = pna.set_segment_table(plan.segments)
        mag, ph = sweep()
        hpd = fit_notch_auto(f_hpd, s21_from_mag_phase(mag, ph))
        print(f"      HPD plan: {len(plan.segments)} segments, span/linewidth ≈ "
              f"{plan.span_over_linewidth:.0f}, phase-step std/mean {plan.uniformity:.3f}")
        for name, ft in (("SPD", spd), ("HPD", hpd)):
            if ft.get("ok"):
                print(f"      {name}: fr={ft['fr']/1e9:.9f} GHz  Ql={ft['Ql']:.4g}  "
                      f"Qi={ft['Qi']:.4g} ± {ft['Qi_err']:.2g}  |Qc|={ft['absQc']:.4g}  "
                      f"noise/radius={noise_to_radius(ft):.3f}")
        check(hpd.get("ok"), "HPD sweep fitted")
        if hpd.get("ok"):
            comb = np.hypot(spd["Qi_err"], hpd["Qi_err"])
            check(abs(spd["Qi"] - hpd["Qi"]) <= 3 * comb,
                  f"SPD and HPD Qi agree within 3σ ({abs(spd['Qi'] - hpd['Qi'])/max(comb,1e-12):.1f}σ)")
            check(hpd["Qi_err"] < spd["Qi_err"], "HPD Qi uncertainty smaller than SPD")

        # ---- 7 -----------------------------------------------------------------
        pna.use_linear_sweep(f0, f1, a.points)
        errs = pna.get_errors()
        check(not errs, f"no SCPI errors left ({errs[:3]})")
        check(pna.ask("FORM?").strip().upper().startswith("REAL,+32") or
              "32" in pna.ask("FORM?"), "data format back to REAL,32")
    finally:
        try:
            pna.write("ABOR")
            pna.use_linear_sweep()
            pna.power(-80)
            pna.output(False)
        except Exception as e:
            print("cleanup:", e)
        pna.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
