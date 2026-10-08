"""
Step 5: full HPD power sweep through measure_resonator_hpd, on the simulated
N5235A, compared with the existing SPD routine and with the true Qi.
"""
import sys, threading, warnings
sys.path.insert(0, "..")
warnings.filterwarnings("ignore")
import numpy as np
from qcodes import Station
from qcodes.dataset import initialise_or_create_database_at, load_by_id

from harness import make_pna, FakePNAHandle, Resonator
from core.drivers.keysight_pna import KeysightN5235A
from core.measure_workers import measure_resonator_hpd, measure_resonator_spd, _sched_for
from core.pna_segment import phase_uniformity

ok = True
def check(c, m):
    global ok; print(("  PASS " if c else "  FAIL ") + m); ok &= bool(c)

class FakeIM:
    def __init__(self, pna):
        self.pna = pna; self.station = Station(pna); self.sample_name = "chipA_sim"
    def label_temperature_k(self): return 0.02

def load_hpd(ds):
    """Same layout logic as analysis_io.load_run for the HPD branch."""
    pdata = ds.get_parameter_data()
    mag = np.asarray(pdata["mag"]["mag"], float)
    freq = np.asarray(pdata["frequency"]["frequency"], float)
    pw = np.asarray(pdata["mag"]["power"], float)
    return pw[:, 0], freq, mag

initialise_or_create_database_at("/tmp/claude-0/hpd_e2e.db")
res = Resonator(sigma_fr=0.0)
lw_hi = res.fr / res.Ql(0)
SPAN = 40 * lw_hi                       # user's chosen span: 40 linewidths at high power
r = {"num": 1, "chip": "chipA", "center_hz": res.fr + 0.2 * lw_hi,
     "fstart_hz": res.fr - 0.5 * SPAN + 3 * lw_hi, "fstop_hz": res.fr + 0.5 * SPAN + 3 * lw_hi,
     # seed as it would come from the Quality step (slightly off on purpose)
     "fr": res.fr + 0.1 * lw_hi, "Ql": 0.8 * res.Ql(0), "Qi": res.Qi(0)}
schedule = [(p, 1, 1000) for p in (0, -10, -20, -30, -40, -50, -60)]
sch, smap = _sched_for(r, schedule)
N = 1001
print(f"resonator: fr={res.fr/1e9:.6f} GHz, |Qc|={res.absQc:.3g}, Qi(0dBm)={res.Qi(0):.3g}, "
      f"Qi(-60dBm)={res.Qi(-60):.3g}; span={SPAN/1e3:.1f} kHz (off-centre), N={N}")

def run(mode, seed, rr=None, handle_mod=None, driver=KeysightN5235A):
    h = FakePNAHandle(resonator=res, seed=seed)
    if handle_mod: handle_mod(h)
    pna = make_pna(driver, h)
    im = FakeIM(pna)
    pts, msgs = [], []
    rr = dict(rr or r)
    if mode == "hpd":
        out = measure_resonator_hpd(im, rr, sch, smap, N, "S21", 7.0, "PowerDep", "20.0mK",
                                    threading.Event(), pts.append, msgs.append)
    else:
        out = measure_resonator_spd(im, rr, sch, smap, N, "S21", "PowerDep", "20.0mK",
                                    threading.Event(), pts.append, msgs.append)
    return out, pts, msgs, h, pna

# ---- one detailed run ------------------------------------------------------
out, pts, msgs, h, pna = run("hpd", 11)
print("\n".join("    " + m for m in msgs[:4]))
check(out["run_id"] is not None and len(pts) == len(schedule), "HPD run completed, all powers measured")
check(not any(p["fallback_spd"] for p in pts), "every power used the segment sweep (no fallback)")
check(all(abs(p["f_hz"][0] - r["fstart_hz"]) < 1 and abs(p["f_hz"][-1] - r["fstop_hz"]) < 1 for p in pts),
      "every power covers the FULL chosen span (no shrinking)")
u = [phase_uniformity(p["f_hz"], res.fr, res.Ql(p["power_dbm"])) for p in pts]
check(max(u) < 0.25, f"points homophasal w.r.t. the true resonance at every power "
      f"(phase-step std/mean {min(u):.2f}–{max(u):.2f}; SPD here ≈ "
      f"{phase_uniformity(np.linspace(r['fstart_hz'], r['fstop_hz'], N), res.fr, res.Ql(0)):.1f})")
fr_true = res.fr
inside = [np.mean(np.abs(p["f_hz"] - fr_true) < 0.5 * res.fr / res.Ql(p["power_dbm"])) for p in pts]
check(min(inside) > 0.35, f"{min(inside):.0%}–{max(inside):.0%} of points within ±linewidth/2 "
      f"(SPD over this span: {1/40:.1%})")
check(pna.sweep_type() == "LIN" and pna.points() == N, "PNA left on a linear sweep of the resonator")
check(h.form == "REAL,32" and pna.get_errors() == [], "format restored, no SCPI errors")
ds = load_by_id(out["run_id"])
pw, freq, mag = load_hpd(ds)
check(mag.shape == (len(schedule), N) and np.allclose(freq[0], pts[0]["f_hz"]),
      "saved run has the per-power frequency layout analysis_io expects")
check(np.all(np.diff(freq, axis=1) >= 1), "stored frequencies are the real 1 Hz-resolved stimulus")
nr = {p["power_dbm"]: p["noise_over_radius"] for p in pts}
true_nr = {P: res.noise_ref * 10 ** (-P / 20) / (res.Ql(P) / (2 * res.absQc)) for P in nr}
check(all(abs(nr[P] / true_nr[P] - 1) < 0.35 for P in nr if true_nr[P] > 0.01 and np.isfinite(nr[P])),
      "noise/radius diagnostic matches the simulated SNR: "
      + ", ".join(f"{P:g} dBm {nr[P]:.3f} (true {true_nr[P]:.3f})" for P in sorted(nr)))
warned = {P for P in nr if any(f"@ {P:g} dBm: noise/radius" in m for m in msgs)}
check(warned == {P for P in nr if nr[P] > 0.1}, f"low-SNR warning raised at {sorted(warned)} dBm")
pna.close()

# ---- statistics: HPD vs SPD vs truth ---------------------------------------
print("\n  Qi relative error, mean |err| over 6 noise realisations")
print(f"  {'P (dBm)':>8} {'true Qi':>9} {'SPD |err|':>10} {'HPD |err|':>10} {'SPD σQi/Qi':>11} {'HPD σQi/Qi':>11}")
errs = {"spd": {}, "hpd": {}}; sig = {"spd": {}, "hpd": {}}
for seed in range(6):
    for mode in ("spd", "hpd"):
        _, pts_m, _, _, p_ = run(mode, 100 + seed)
        p_.close()
        for p in pts_m:
            P = p["power_dbm"]; q = p["Qi"]; t = res.Qi(P)
            errs[mode].setdefault(P, []).append(abs(q - t) / t if q else np.nan)
            sig[mode].setdefault(P, []).append((p["Qi_err"] or np.nan) / q if q else np.nan)
better = 0
for P, _, _ in sorted(schedule, reverse=True):
    e_s, e_h = np.nanmean(errs["spd"][P]), np.nanmean(errs["hpd"][P])
    s_s, s_h = np.nanmean(sig["spd"][P]), np.nanmean(sig["hpd"][P])
    better += s_h < s_s
    print(f"  {P:8.0f} {res.Qi(P):9.3g} {e_s:10.2%} {e_h:10.2%} {s_s:11.2%} {s_h:11.2%}")
check(better == len(schedule), "HPD fit uncertainty below SPD at every power (same span, same N)")
good = [P for P, _, _ in schedule
        if res.noise_ref * 10 ** (-P / 20) / (res.Ql(P) / (2 * res.absQc)) < 0.1]
check(all(np.nanmean(errs["hpd"][P]) < np.nanmean(errs["spd"][P]) for P in good),
      f"HPD true error below SPD at every power with noise/radius < 0.1 ({good} dBm)")
check(all(np.nanmean(errs["hpd"][P]) > np.nanmean(errs["spd"][P])
          for P in (-50, -60)),
      "(known limit) at noise/radius ≈ 0.2–0.6 HPD's Qi is biased high — warned below")

# ---- robustness ------------------------------------------------------------
# no Ql seed in the resonator dict -> one seeding linear sweep
rr = dict(r); rr.pop("Ql", None)
out, pts, msgs, h, p_ = run("hpd", 5, rr=rr)
check(any("seed" in m for m in msgs) and not any(p["fallback_spd"] for p in pts),
      "no Ql seed: one linear seed sweep, then HPD")
p_.close()

# instrument refuses segment commands -> each power falls back to SPD, run still completes
def break_segments(h):
    h._seg_list = lambda arg: h._err(-113, "Undefined header")
    orig = h._segment
    def seg(num, sub, arg, head, query, unknown):
        if sub.startswith("ADD"):
            h._err(-113, "Undefined header"); return
        return orig(num, sub, arg, head, query, unknown)
    h._segment = seg
out, pts, msgs, h, p_ = run("hpd", 6, handle_mod=break_segments)
check(out["run_id"] is not None and len(pts) == len(schedule) and all(p["fallback_spd"] for p in pts),
      "segment table rejected: every power measured with linear fallback, flagged")
p_.close()

# stock QCoDeS driver (no segment API) -> graceful fallback, no crash
from qcodes.instrument_drivers.Keysight import KeysightN5245A as StockN5245A
res_hi = Resonator(p_c_dbm=-100)   # so the stock driver's -30 dBm limit is not hit
sch2, smap2 = _sched_for(r, [(p, 1, 1000) for p in (0, -10, -20)])
h = FakePNAHandle(resonator=res_hi, seed=3); stock = make_pna(StockN5245A, h)
pts2, msgs2 = [], []
measure_resonator_hpd(FakeIM(stock), dict(r), sch2, smap2, N, "S21", 7.0, "PowerDep", "x",
                      threading.Event(), pts2.append, msgs2.append)
check(len(pts2) == 3 and all(p["fallback_spd"] for p in pts2),
      "stock driver: HPD falls back to linear sweeps instead of crashing")
stock.close()

print("ALL PASS" if ok else "SOME FAILURES"); sys.exit(0 if ok else 1)
