"""
Up-front HPD seed check (PowerWorker / TemperatureWorker), simulated N5235A.
Three resonators: two fine, one whose span misses the resonance (seed fit fails).
"""
import sys, threading, time, warnings
sys.path.insert(0, "..")
warnings.filterwarnings("ignore")
from PyQt5.QtCore import Qt, QCoreApplication
from qcodes import Station
from qcodes.dataset import initialise_or_create_database_at

from harness import make_pna, FakePNAHandle, Resonator
from core.drivers.keysight_pna import KeysightN5235A
from core.measure_workers import PowerWorker, TemperatureWorker

ok = True
def check(c, m):
    global ok; print(("  PASS " if c else "  FAIL ") + m); ok &= bool(c)

app = QCoreApplication.instance() or QCoreApplication([])
initialise_or_create_database_at("/tmp/claude-0/precheck.db")
res = Resonator(kerr_hz_0dbm=120e3)
lw = res.fr / res.Ql(0)

class FakeFridge:
    def __init__(self): self.set_calls = []
    def is_connected(self): return True
    def set_target_temperature(self, T, tol_k=1e-4): self.set_calls.append(T); return T
    def wait_until_stable(self, T, **kw): return True

class FakeIM:
    def __init__(self, pna):
        self.pna = pna; self.station = Station(pna); self.sample_name = "chipA_sim"
        self.fridge = FakeFridge()
    def label_temperature_k(self): return 0.02

def res_dict(num, off=0.0):
    return {"num": num, "chip": "chipA", "center_hz": res.fr + off,
            "fstart_hz": res.fr + off - 17 * lw, "fstop_hz": res.fr + off + 23 * lw,
            "fr": res.fr - 47e3, "Ql": 1.55 * res.Ql(0), "Qi": 2.88e6}

RES = [res_dict(1), res_dict(2), res_dict(3, off=60 * lw)]   # #3: resonance not in span
POW = (0, -10, -20, -30)
N = 801

def run(cls, answer, timeout_s=30, temps=None, confirm=True):
    h = FakePNAHandle(resonator=res, seed=3); pna = make_pna(KeysightN5235A, h)
    im = FakeIM(pna)
    p = {"mode": "hpd", "schedule": [(x, 1, 1000) for x in POW], "points": N,
         "qi_reject_factor": 7.0, "trace": "S21", "hpd_start_dbm": -20,
         "confirm_seed": confirm, "seed_confirm_timeout_s": timeout_s}
    if temps:
        p["temperatures_k"] = temps
    w = cls(im, [dict(r) for r in RES], p)
    req, closed, pts, prog, sweeps_at_request = [], [], [], [], []
    def on_req(info):
        req.append(info); sweeps_at_request.append(h.sweeps)
        if answer is not None:
            a = answer(info) if callable(answer) else answer
            threading.Timer(0.3, w.answer_seed, args=(a,)).start()
    w.seed_precheck.connect(on_req, Qt.DirectConnection)
    w.seed_precheck_closed.connect(closed.append, Qt.DirectConnection)
    w.point_measured.connect(pts.append, Qt.DirectConnection)
    w.progress.connect(prog.append, Qt.DirectConnection)
    w.run()
    pna.close()
    return dict(req=req, closed=closed, pts=pts, prog=prog, w=w, im=im,
                sweeps_at_request=sweeps_at_request)

def sweeps_by_res(pts, t=None):
    out = {}
    for p in pts:
        if t is None or p["t_label"] == t:
            out.setdefault(p["num"], []).append(p["sweep"])
    return out

# --- power sweep, user unticks #2 -------------------------------------------
R = run(PowerWorker, lambda info: {"action": "start",
                                   "use": {it["key"]: it["key"] != "chipA|2" for it in info["items"]}})
items = R["req"][0]["items"] if R["req"] else []
check(len(R["req"]) == 1 and [it["label"] for it in items] == ["chipA_Res1", "chipA_Res2", "chipA_Res3"]
      and all(it["power_dbm"] == -10 for it in items),
      "one seed-check request, all 3 resonators measured at the seed power (-10 dBm)")
check(R["sweeps_at_request"] == [3] and not R["pts"][:0],
      "the check happens BEFORE the power sweep (exactly 3 PNA sweeps when the window opens)")
check([it["ok"] for it in items] == [True, True, False],
      "the resonator whose span misses the resonance is reported as a failed fit")
s = sweeps_by_res(R["pts"])
check(s == {1: ["linear", "linear", "hpd", "hpd"], 2: ["linear"] * 4, 3: ["linear"] * 4},
      f"#1 ticked → HPD from -20; #2 unticked → linear; #3 failed fit → linear  {s}")
check(any("HPD for 1, linear for 2" in m for m in R["prog"]), "summary logged")

# --- nobody answers ------------------------------------------------------------
t0 = time.time()
R = run(PowerWorker, None, timeout_s=1.5)
check(R["closed"] == ["timeout"] and all(v == ["linear"] * 4 for v in sweeps_by_res(R["pts"]).values())
      and len(R["pts"]) == 12 and any("all resonators stay LINEAR" in m for m in R["prog"]),
      f"no answer → window told to close, every resonator linear, run completes ({time.time()-t0:.0f} s)")

# --- abort -------------------------------------------------------------------
R = run(PowerWorker, {"action": "abort"})
check(R["pts"] == [] and R["w"]._abort.is_set(), "abort at the seed check → nothing else measured")

# --- seed check off ------------------------------------------------------------
R = run(PowerWorker, None, confirm=False)
s = sweeps_by_res(R["pts"])
check(R["req"] == [] and s[1][2:] == ["hpd", "hpd"] and s[3] == ["linear"] * 4,
      "seed check off → no window; good fits seed HPD automatically, failed one stays linear")

# --- temperature sweep: ask once, both temperatures follow -----------------------
R = run(TemperatureWorker, lambda info: {"action": "start",
                                         "use": {it["key"]: it["key"] == "chipA|1" for it in info["items"]}},
        temps=[0.02, 0.05])
check(len(R["req"]) == 1 and R["sweeps_at_request"] == [3] and R["im"].fridge.set_calls == [0.02, 0.05],
      "temperature run: one seed check before the first temperature is set")
s20, s50 = sweeps_by_res(R["pts"], "20.0mK"), sweeps_by_res(R["pts"], "50.0mK")
check(s20 == s50 == {1: ["linear", "linear", "hpd", "hpd"], 2: ["linear"] * 4, 3: ["linear"] * 4},
      "both temperatures follow the same choice (Res1 HPD, Res2/3 linear), no further questions")

print("ALL PASS" if ok else "SOME FAILURES"); sys.exit(0 if ok else 1)
