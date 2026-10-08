"""
Headless GUI test (Qt offscreen) of the HPD-start column, the pre-run warning
and the seed dialog, driving the real PowerWindow / TemperatureWindow with a
real PowerWorker on the simulated PNA. App modules not under test (settings,
instrument manager, dialogs, theme, analysis_io) are stubbed.
"""
import os, sys, types, time, warnings
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".."); sys.path.insert(0, ".")
warnings.filterwarnings("ignore")
import numpy as np

# ------------------------------------------------------------------ stubs
class _Settings:
    def __init__(self): self.d = {}
    def block(self, k): return dict(self.d.get(k, {}))
    def remember(self, k, m): self.d.setdefault(k, {}).update(m)
    def set_list(self, *a, **k): pass
    def get(self, k, default=None): return default
settings_mod = types.ModuleType("core.settings"); settings_mod.settings = _Settings()

class _IM:
    def __init__(self): self.pna = None; self.station = None; self.busy = False; self._cb = []
    sample_name = "chipA_sim"
    def on_busy_changed(self, cb): self._cb.append(cb)
    def set_busy(self, v):
        self.busy = v
        for cb in self._cb: cb(v)
    def pna_connected(self): return self.pna is not None
    def fridge_connected(self): return True
    def label_temperature_k(self): return 0.02
im_mod = types.ModuleType("core.instrument_manager"); im_mod.instrument_manager = _IM()
dialogs_mod = types.ModuleType("windows.dialogs"); dialogs_mod.QualityRunPicker = object
theme_mod = types.ModuleType("core.theme"); theme_mod.hx = lambda name: "#cdd6f4"
aio_mod = types.ModuleType("core.analysis_io")
import core
for name, mod in (("core.settings", settings_mod), ("core.instrument_manager", im_mod),
                  ("core.theme", theme_mod), ("core.analysis_io", aio_mod),
                  ("windows.dialogs", dialogs_mod)):
    sys.modules[name] = mod
core.settings = settings_mod; core.instrument_manager = im_mod
core.theme = theme_mod; core.analysis_io = aio_mod
import windows; windows.dialogs = dialogs_mod

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QTimer
app = QApplication.instance() or QApplication([])

from windows.power_window import PowerWindow
from windows.temperature_window import TemperatureWindow
from windows.hpd_controls import HPDSeedCheckDialog, SWEEP_COL
import windows.hpd_controls as hc

ok = True
def check(c, m):
    global ok; print(("  PASS " if c else "  FAIL ") + m); ok &= bool(c)

def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)

shots = "/tmp/claude-0/gui_shots"; os.makedirs(shots, exist_ok=True)

# ------------------------------------------------------------------ table / controls
for cls in (PowerWindow, TemperatureWindow):
    w = cls(); w.resize(1250, 820)
    name = cls.__name__
    w.sp_pstart.setValue(-40); w.sp_pstop.setValue(0); w.sp_pstep.setValue(10)
    w._generate_table()
    w.cmb_mode.setCurrentIndex(0)
    col = [w.table.item(i, SWEEP_COL).text() for i in range(w.table.rowCount())]
    check(w.table.columnCount() == 4 and w.table.horizontalHeaderItem(3).text() == "Sweep"
          and set(col) == {"Linear"} and not w.hpd_ctrl.isVisibleTo(w),
          f"{name}: 'Sweep' column present; SPD mode → all Linear, HPD controls hidden")
    w.cmb_mode.setCurrentIndex(1)
    w.hpd_ctrl.set_start_dbm(-20)
    pw = [float(w.table.item(i, 0).text()) for i in range(w.table.rowCount())]
    col = {p: w.table.item(i, SWEEP_COL).text() for i, p in enumerate(pw)}
    check(w.hpd_ctrl.isVisibleTo(w) and col == {-40: "HPD", -30: "HPD", -20: "HPD",
                                                 -10: "Linear", 0: "Linear"},
          f"{name}: HPD mode, start -20 → 0/-10 Linear, -20…-40 HPD")
    row = pw.index(-30.0)
    w._on_table_click(row, SWEEP_COL)
    check(w.hpd_ctrl.start_dbm() == -30 and w.table.item(pw.index(-20.0), SWEEP_COL).text() == "Linear",
          f"{name}: clicking the Sweep cell at -30 moves the HPD start there")
    w._fill_table([(0, 1, 1000), (-15, 3, 300), (-45, 15, 10)])
    check([w.table.item(i, SWEEP_COL).text() for i in range(3)] == ["Linear", "Linear", "HPD"],
          f"{name}: column follows a loaded/modified table")
    p = w.hpd_ctrl.params()
    check(p == {"hpd_start_dbm": -30.0, "confirm_seed": True, "seed_confirm_timeout_s": 600.0},
          f"{name}: run params (HPD start, confirm on, 10 min) = {p}")
    w._fill_table([(p_, 1, 1000) for p_ in (0, -10, -20, -30, -40)])
    w.grab().save(f"{shots}/{name}.png")
    w.close()

# ------------------------------------------------------------------ schedule_io
from core.schedule_io import save_schedule, load_schedule, load_schedule_options
import json
fn = "/tmp/claude-0/sched.json"
save_schedule(fn, [(0, 1, 1000), (-20, 5, 100)], hpd_start_dbm=-20)
check(load_schedule(fn) == [(0.0, 1, 1000), (-20.0, 5, 100)]
      and load_schedule_options(fn) == {"hpd_start_dbm": -20.0}, "config file keeps the HPD start")
json.dump({"kind": "qres_power_schedule", "version": 1, "schedule": [[0, 1, 1000]]}, open(fn, "w"))
check(load_schedule_options(fn) == {} and load_schedule(fn) == [(0.0, 1, 1000)],
      "old (v1) config files still load")

# ------------------------------------------------------------------ pre-run warning text
captured = {}
def fake_exec(self):
    captured["text"] = self.text(); captured["title"] = self.windowTitle()
    return captured.get("answer", QMessageBox.Cancel)
QMessageBox.exec_ = fake_exec
w = PowerWindow()
w.cmb_mode.setCurrentIndex(1); w.hpd_ctrl.set_start_dbm(-20)
sched = [(p_, 1, 1000) for p_ in (0, -10, -20, -30, -40)]
res_ok = hc.confirm_hpd_run(w, sched, w.hpd_ctrl)
t = captured["text"]
check(not res_ok and "Linear sweeps at: 0, -10 dBm" in t and "HPD (segment sweep) at: -20, -30, -40" in t
      and "fit at -10 dBm" in t and "measured once at -10 dBm" in t and "10 min" in t,
      "warning lists linear/HPD powers, the -10 dBm seed power, the up-front check and the 10-min fallback")
print("      ---- warning text ----\n      " + t.replace("\n", "\n      "))

# ------------------------------------------------------------------ full run with the seed-check window
sys.path.insert(0, "tests")
from harness import make_pna, FakePNAHandle, Resonator
from core.drivers.keysight_pna import KeysightN5235A
from qcodes import Station
from qcodes.dataset import initialise_or_create_database_at
from PyQt5.QtCore import Qt
initialise_or_create_database_at("/tmp/claude-0/gui_run.db")
res = Resonator(kerr_hz_0dbm=120e3)
lw = res.fr / res.Ql(0)
def resonator(num, off=0.0):
    return {"num": num, "chip": "chipA", "center_hz": res.fr + off, "_checked": True,
            "fstart_hz": res.fr + off - 17 * lw, "fstop_hz": res.fr + off + 23 * lw,
            "fr": res.fr - 47e3, "Ql": 1.55 * res.Ql(0), "Qi": 2.88e6}

def gui_run(action, untick=(), short_timeout=None):
    """action: 'start' | 'abort' | 'close' | None (never answer)."""
    h = FakePNAHandle(resonator=res, seed=5)
    pna = make_pna(KeysightN5235A, h)
    im_mod.instrument_manager.pna = pna
    im_mod.instrument_manager.station = Station(pna)
    w = PowerWindow(); w.load([resonator(1), resonator(2), resonator(3, off=60 * lw)])
    w.sp_points.setValue(801)
    w._fill_table(sched); w.cmb_mode.setCurrentIndex(1); w.hpd_ctrl.set_start_dbm(-20)
    captured["answer"] = QMessageBox.Ok
    if short_timeout:
        orig = hc.HPDStartControls.params
        hc.HPDStartControls.params = lambda self: dict(orig(self), seed_confirm_timeout_s=short_timeout)
    dialogs = []
    orig_show = w._show_seed_dialog
    def spy(worker, info):
        orig_show(worker, info)
        dlg = w._seed_dlg; dialogs.append(dlg)
        pump(0.3)
        if len(dialogs) == 1 and action == "start" and untick:
            dlg.lst.setCurrentRow(2); pump(0.2); dlg.grab().save(f"{shots}/seed_check_failed_row.png")
            dlg.lst.setCurrentRow(0); pump(0.2); dlg.grab().save(f"{shots}/seed_check.png")
        for i in range(dlg.lst.count()):
            if dlg.items[i]["key"] in untick:
                dlg.lst.item(i).setCheckState(Qt.Unchecked)
        if action == "start":
            QTimer.singleShot(200, dlg.btn_start.click)
        elif action == "abort":
            QTimer.singleShot(200, dlg.btn_abort.click)
        elif action == "close":
            QTimer.singleShot(200, dlg.close)
    w._show_seed_dialog = spy
    points = []
    w._run()
    w._worker.point_measured.connect(points.append)
    t0 = time.time()
    while w._worker.isRunning() and time.time() - t0 < 900:
        pump(0.05)
    pump(0.3)
    if short_timeout:
        hc.HPDStartControls.params = orig
    log = w.log.toPlainText()
    pna.close()
    by = {}
    for p_ in points:
        by.setdefault(p_["num"], []).append(p_["sweep"])
    return w, dialogs, by, log

def _gone(d):
    try:
        return not d.isVisible()
    except RuntimeError:          # already deleted (WA_DeleteOnClose) = closed
        return True

LIN4 = ["linear"] * 5
# 1) start with Res2 unticked
w, dialogs, by, log = gui_run("start", untick=("chipA|2",))
d = dialogs[0] if dialogs else None
check(len(dialogs) == 1, "one seed-check window for all resonators")
check(by == {1: ["linear", "linear", "hpd", "hpd", "hpd"], 2: LIN4, 3: LIN4},
      f"Res1 ticked → HPD from -20; Res2 unticked → linear; Res3 (no resonance) → linear  {by}")
check("Seed check: HPD for 1 resonator(s) (Res 1), linear for 2." in log,
      "choice logged in the window")
check(not im_mod.instrument_manager.busy and w.btn_run.isEnabled(), "window un-busied after the run")

# 2) close the window = all linear
w, dialogs, by, log = gui_run("close")
check(len(dialogs) == 1 and by == {1: LIN4, 2: LIN4, 3: LIN4}, "closing the window → all linear")

# 3) abort
w, dialogs, by, log = gui_run("abort")
check(by == {} and "aborted at the HPD seed check" in log, "abort → no power sweep at all")

# 4) nobody answers (timeout shortened to 2 s for the test)
w, dialogs, by, log = gui_run(None, short_timeout=2.0)
check(len(dialogs) == 1 and by == {1: LIN4, 2: LIN4, 3: LIN4} and "timed out" in log
      and all(_gone(x) for x in dialogs),
      "nobody answers → window closes itself, all resonators linear, run completes")

print("screenshots:", sorted(os.listdir(shots)))
print("ALL PASS" if ok else "SOME FAILURES"); sys.exit(0 if ok else 1)
