"""
Linear above the HPD-start power, HPD below; seed confirmation with timeout.
Simulated N5235A, resonator with strong Kerr shift at high power (the 0 dBm
fit is off by ~+50 % in Qi and ~1.7 linewidths in fr).
"""
import sys, threading, time, warnings
sys.path.insert(0, "..")
warnings.filterwarnings("ignore")
import numpy as np
from qcodes import Station
from qcodes.dataset import initialise_or_create_database_at, load_by_id

from harness import make_pna, FakePNAHandle, Resonator
from core.drivers.keysight_pna import KeysightN5235A
from core.measure_workers import measure_resonator_hpd, _sched_for
from core.pna_segment import phase_uniformity

ok = True
def check(c, m):
    global ok; print(("  PASS " if c else "  FAIL ") + m); ok &= bool(c)

class FakeIM:
    def __init__(self, pna):
        self.pna = pna; self.station = Station(pna); self.sample_name = "chipA_sim"
    def label_temperature_k(self): return 0.02

initialise_or_create_database_at("/tmp/claude-0/hpd_threshold.db")
res = Resonator(kerr_hz_0dbm=120e3)
lw = res.fr / res.Ql(0)
r0 = {"num": 1, "chip": "chipA", "center_hz": res.fr,
      "fstart_hz": res.fr - 17 * lw, "fstop_hz": res.fr + 23 * lw,
      # what the Quality step would have produced at 0 dBm (Kerr-distorted)
      "fr": res.fr - 47e3, "Ql": 1.55 * res.Ql(0), "Qi": 2.88e6}
POW = (0, -10, -20, -30, -40)
sch, smap = _sched_for(r0, [(p, 1, 1000) for p in POW])
N = 1001

def run(hpd_start=None, confirm=None, policy=None, seed=7, r=None):
    h = FakePNAHandle(resonator=res, seed=seed)
    pna = make_pna(KeysightN5235A, h)
    pts, msgs, asked = [], [], []
    def conf(info):
        asked.append(info)
        return confirm(info) if callable(confirm) else confirm
    abort = threading.Event()
    out = measure_resonator_hpd(FakeIM(pna), dict(r or r0), sch, smap, N, "S21", 7.0,
                                "PowerDep", "20.0mK", abort, pts.append, msgs.append,
                                hpd_start_dbm=hpd_start,
                                confirm_seed=(conf if confirm is not None else None),
                                seed_policy=policy)
    pna.close()
    return out, pts, msgs, asked, abort

def summary(pts):
    return {p["power_dbm"]: p for p in pts}

def uni(p):
    return phase_uniformity(p["f_hz"], res.fr, res.Ql(p["power_dbm"]))

# ---------------------------------------------------------------- A: old way
out, pts, msgs, _, _ = run(hpd_start=None)
A = summary(pts)
print(f"A  all-HPD seeded from the Kerr-distorted 0 dBm Quality fit:")
print("   " + "  ".join(f"{P:g} dBm: {A[P]['sweep']}, unif {uni(A[P]):.2f}" for P in POW))

# ---------------------------------------------------------------- C: new way, confirmed
out, pts, msgs, asked, _ = run(hpd_start=-20,
                                confirm=lambda info: {"action": "use",
                                                      "power": info["default_power_dbm"]})
C = summary(pts)
print("C  linear at 0/-10, HPD from -20, seed confirmed (-10 dBm fit):")
print("   " + "  ".join(f"{P:g} dBm: {C[P]['sweep']}, unif {uni(C[P]):.2f}" for P in POW))
check([C[P]["sweep"] for P in POW] == ["linear", "linear", "hpd", "hpd", "hpd"],
      "powers above HPD start are linear, at/below are HPD")
check(len(asked) == 1 and asked[0]["default_power_dbm"] == -10
      and [f["power_dbm"] for f in asked[0]["fits"]] == [-10, 0],
      "asked once, default seed = lowest linear power (-10), both linear fits offered")
check(any("seeded from the -10 dBm" in m for m in msgs), "log says which fit seeded the HPD")
check(uni(C[-20]) < 0.5 * uni(A[-10]),
      f"first HPD power well placed: unif {uni(C[-20]):.2f} vs {uni(A[-10]):.2f} when seeded "
      "from the Kerr fit")
ds = load_by_id(out["run_id"]); pd = ds.get_parameter_data()
check(np.asarray(pd["mag"]["mag"]).shape == (len(POW), N),
      "one run, every power N points (analysis_io layout unchanged)")
check(ds.metadata.get("hpd_start_dbm") == "-20", "HPD start stored in run metadata")
errC = {P: abs(C[P]["Qi"] / res.Qi(P) - 1) for P in (-20, -30, -40)}
check(all(e < 0.02 for e in errC.values()),
      "HPD Qi within 2 % of truth: " + ", ".join(f"{P}: {e:.2%}" for P, e in errC.items()))

# ---------------------------------------------------------------- user picks another power
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm={"action": "use", "power": 0.0})
check(any("seeded from the 0 dBm" in m for m in msgs), "user can choose a different linear fit (0 dBm)")

# ---------------------------------------------------------------- linear chosen
pol = {}
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm={"action": "linear"}, policy=pol)
check(all(p["sweep"] == "linear" for p in pts) and len(pts) == len(POW),
      "'keep linear' → every power linear, run completes")
check(pol.get("chipA|1", {}).get("action") == "linear", "decision remembered for this resonator")

# ---------------------------------------------------------------- timeout
pol = {}
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm={"action": "timeout"}, policy=pol)
check(all(p["sweep"] == "linear" for p in pts) and any("no answer" in m for m in msgs),
      "no answer → continues LINEAR, logged")

# ---------------------------------------------------------------- abort
out, pts, msgs, asked, abort = run(hpd_start=-20, confirm={"action": "abort"})
check(abort.is_set() and [p["power_dbm"] for p in pts] == [0, -10],
      "abort at the seed check stops the run (only 0/-10 measured)")

# ---------------------------------------------------------------- temperature reuse
pol = {}
run(hpd_start=-20, confirm={"action": "use", "power": -10.0}, policy=pol)
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm={"action": "linear"}, policy=pol, seed=9)
check(len(asked) == 0 and summary(pts)[-20]["sweep"] == "hpd"
      and any("seeded from the -10 dBm" in m for m in msgs),
      "next temperature: not asked again, reuses the -10 dBm choice from that temperature's data")
pol = {"chipA|1": {"action": "linear", "why": "you chose linear"}}
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm={"action": "use", "power": -10.0}, policy=pol)
check(len(asked) == 0 and all(p["sweep"] == "linear" for p in pts),
      "next temperature after 'linear': stays linear, not asked")

# ---------------------------------------------------------------- no confirmation requested
out, pts, msgs, asked, _ = run(hpd_start=-20, confirm=None)
check(summary(pts)[-20]["sweep"] == "hpd" and any("seeded from the -10 dBm" in m for m in msgs),
      "confirmation off → seeds automatically from the lowest linear power")

# ---------------------------------------------------------------- HPD start above all powers
out, pts, msgs, asked, _ = run(hpd_start=10, confirm={"action": "linear"})
check(len(asked) == 0 and all(p["sweep"] == "hpd" for p in pts),
      "HPD start above every power → all HPD, seeded as before, no question")

print("ALL PASS" if ok else "SOME FAILURES"); sys.exit(0 if ok else 1)
