"""
windows/quality_window.py
=========================
Step 3: quality assessment with an explicit per-resonator decision workflow.

Each confirmed resonator is swept once over its own start→stop span with the
chosen Power / IF bw / Averages / Points; the run name encodes the frequencies.
You can also Load quality runs from a database to bypass live wideband+quality.

Per resonator you then: select it → Run fit (extract fr, Qi, Ql, Qc) →
Confirm / Ignore / Delete, or Re-measure to sweep it again for a cleaner fit.

  • Delete  — removes it from the workflow and FREES its number for the next
              resonator. The saved run stays in the database.
  • Ignore  — keeps the number RESERVED (skipped in power/temperature runs).

Continue → Power-dependent stays disabled until every resonator is Confirmed or
Ignored (deleted ones removed), with at least one Confirmed.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Optional

import numpy as np
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QDoubleSpinBox, QSpinBox, QPushButton, QListWidget, QListWidgetItem,
    QTextEdit, QSplitter, QMessageBox,
)
from PyQt5.QtCore import Qt, pyqtSignal

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.measure_workers import QualityWorker
from core.fitting import fit_notch, s21_from_mag_phase, format_q
from windows.widgets.resonator_fit_view import ResonatorFitView
from windows.dialogs import QualityRunPicker
from core import analysis_io as aio
from core import theme

logger = logging.getLogger(__name__)

STATE_LABEL = {
    "unmeasured": "not measured", "measured": "measured — Run fit",
    "fitted": "fitted — decide", "confirmed": "✓ confirmed", "ignored": "skipped (ignored)",
}


class QualityWindow(QMainWindow):
    resonatorsForPower = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("3 · Quality Assessment")
        self.setMinimumSize(1180, 760)
        self._res: List[Dict] = []          # ordered resonators with state
        self._free_nums: List[int] = []     # freed by Delete, reused next
        self._worker = None
        self._remeasure_num: Optional[int] = None
        self._build_ui()
        instrument_manager.on_busy_changed(self._on_busy)

    # ------------------------------------------------------------------

    def load(self, confirmed: List[Dict]):
        """Receive resonators from the span picker (state 'unmeasured')."""
        self._res = []
        for r in confirmed:
            d = dict(r); d["state"] = "unmeasured"; d["fit"] = None
            d.setdefault("span_mhz", abs(d["fstop_hz"] - d["fstart_hz"]) / 1e6)
            self._res.append(d)
        self._free_nums = []
        self._refresh_list(); self._update_gate()

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget(); outer = QVBoxLayout(root); self.setCentralWidget(root)
        split = QSplitter(Qt.Horizontal); outer.addWidget(split, 1)

        left = QWidget(); L = QVBoxLayout(left); L.setContentsMargins(10, 10, 10, 10)
        grp = QGroupBox("Quality sweep parameters"); g = QGridLayout(grp)
        q = settings.block("quality")
        self.sp_power = QDoubleSpinBox(); self.sp_power.setRange(-90, 30); self.sp_power.setDecimals(2); self.sp_power.setSuffix(" dBm")
        self.sp_power.setValue(float(q.get("assess_power_dbm", -10.0)))
        self.sp_ifbw = QSpinBox(); self.sp_ifbw.setRange(1, 1_000_000); self.sp_ifbw.setSuffix(" Hz")
        self.sp_ifbw.setValue(int(q.get("assess_if_bw", 1000)))
        self.sp_avg = QSpinBox(); self.sp_avg.setRange(1, 1000); self.sp_avg.setValue(int(q.get("assess_averages", 1)))
        self.sp_points = QSpinBox(); self.sp_points.setRange(11, 100001); self.sp_points.setValue(int(q.get("points", 2001)))
        g.addWidget(QLabel("Power"), 0, 0); g.addWidget(self.sp_power, 0, 1)
        g.addWidget(QLabel("IF bw"), 1, 0); g.addWidget(self.sp_ifbw, 1, 1)
        g.addWidget(QLabel("Averages"), 2, 0); g.addWidget(self.sp_avg, 2, 1)
        g.addWidget(QLabel("Points"), 3, 0); g.addWidget(self.sp_points, 3, 1)
        L.addWidget(grp)

        brow = QHBoxLayout()
        self.btn_run = QPushButton("Run quality sweep"); self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run); brow.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop"); self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self._stop); brow.addWidget(self.btn_stop)
        L.addLayout(brow)
        self.btn_load = QPushButton("Load from database…  (bypass wideband + quality)")
        self.btn_load.clicked.connect(self._load_db); L.addWidget(self.btn_load)

        L.addWidget(QLabel("Resonators"))
        self.list = QListWidget(); self.list.currentRowChanged.connect(self._on_select)
        L.addWidget(self.list, 1)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(110)
        L.addWidget(self.log)
        split.addWidget(left)

        right = QWidget(); R = QVBoxLayout(right); R.setContentsMargins(10, 10, 10, 10)
        self.lbl_head = QLabel("Run the sweep (or load from a database), then select a resonator.")
        self.lbl_head.setStyleSheet(f"font-weight:bold; color:{theme.hx('accent')};")
        R.addWidget(self.lbl_head)
        self.lbl_metrics = QLabel(""); self.lbl_metrics.setStyleSheet(f"font-family:monospace; color:{theme.hx('text')};")
        R.addWidget(self.lbl_metrics)
        self.fitplot = ResonatorFitView(stacked=True); R.addWidget(self.fitplot, 1)

        frow = QHBoxLayout()
        self.btn_fit = QPushButton("Run fit"); self.btn_fit.setObjectName("primary")
        self.btn_fit.clicked.connect(self._run_fit); frow.addWidget(self.btn_fit)
        self.btn_confirm = QPushButton("Confirm"); self.btn_confirm.setObjectName("success")
        self.btn_confirm.clicked.connect(self._confirm); frow.addWidget(self.btn_confirm)
        self.btn_ignore = QPushButton("Ignore"); self.btn_ignore.clicked.connect(self._ignore); frow.addWidget(self.btn_ignore)
        self.btn_delete = QPushButton("Delete"); self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self._delete); frow.addWidget(self.btn_delete)
        self.btn_remeas = QPushButton("Re-measure"); self.btn_remeas.clicked.connect(self._remeasure); frow.addWidget(self.btn_remeas)
        R.addLayout(frow)
        split.addWidget(right); split.setStretchFactor(1, 1)

        b2 = QHBoxLayout()
        self.lbl_summary = QLabel(""); self.lbl_summary.setStyleSheet(f"color:{theme.hx('subtext')};")
        b2.addWidget(self.lbl_summary); b2.addStretch()
        self.btn_continue = QPushButton("Continue  →  Power-dependent"); self.btn_continue.setObjectName("success")
        self.btn_continue.clicked.connect(self._continue); b2.addWidget(self.btn_continue)
        outer.addLayout(b2)

    # ------------------------------------------------------------------
    # numbering pool
    # ------------------------------------------------------------------

    def _used_nums(self):
        return {r["num"] for r in self._res if r.get("num") is not None}

    def _assign_num(self, preferred=None):
        used = self._used_nums()
        if preferred is not None and preferred not in used:
            if preferred in self._free_nums:
                self._free_nums.remove(preferred)
            return preferred
        if self._free_nums:
            return self._free_nums.pop(0)
        return (max(used) + 1) if used else 1

    # ------------------------------------------------------------------
    # list / state
    # ------------------------------------------------------------------

    def _refresh_list(self):
        cur = self.list.currentRow()
        self.list.blockSignals(True); self.list.clear()
        for r in self._res:
            num = r["num"]; st = r.get("state", "unmeasured")
            fit = r.get("fit") or {}
            if fit.get("ok"):
                tag = f"fr={fit['fr']/1e9:.6f} GHz  Qi={format_q(fit['Qi'])}"
            else:
                tag = f"{r.get('center_hz', 0)/1e9:.6f} GHz"
            self.list.addItem(QListWidgetItem(f"Res {num}  ·  {tag}  ·  [{STATE_LABEL.get(st, st)}]"))
        self.list.blockSignals(False)
        if 0 <= cur < self.list.count():
            self.list.setCurrentRow(cur)
        self._update_gate()

    def _update_gate(self):
        n = len(self._res)
        decided = sum(1 for r in self._res if r["state"] in ("confirmed", "ignored"))
        confirmed = sum(1 for r in self._res if r["state"] == "confirmed")
        self.lbl_summary.setText(f"{n} resonators · {confirmed} confirmed · "
                                 f"{decided}/{n} decided · free #: {sorted(self._free_nums)}")
        ready = (n > 0 and decided == n and confirmed > 0 and not instrument_manager.busy)
        self.btn_continue.setEnabled(ready)

    def _current(self) -> Optional[Dict]:
        row = self.list.currentRow()
        return self._res[row] if 0 <= row < len(self._res) else None

    def _on_select(self, row):
        r = self._current()
        if r is None:
            return
        if r.get("f_hz") is None or not len(r["f_hz"]):
            self.lbl_head.setText(f"Res {r['num']} — not measured yet.")
            self.lbl_metrics.setText(""); self.fitplot.set_data([], [], []); return
        self._show(r)

    def _show(self, r):
        self.fitplot.set_data(r["f_hz"], r["mag_db"], r["phase_deg"])
        fit = r.get("fit") or {}
        self.fitplot.set_fit(fit)
        if fit.get("ok"):
            self.lbl_head.setText(f"Res {r['num']} — fr = {fit['fr']/1e9:.6f} GHz  [{STATE_LABEL[r['state']]}]")
            self.lbl_metrics.setText(
                f"Qi = {format_q(fit['Qi'])}   Ql = {format_q(fit['Ql'])}   "
                f"Qc = {format_q(fit['Qc'])}")
        else:
            self.lbl_head.setText(f"Res {r['num']} — press Run fit (drag the window to the resonance if needed).")
            self.lbl_metrics.setText(fit.get("error", "") if fit else "")

    # ------------------------------------------------------------------
    # measuring
    # ------------------------------------------------------------------

    def _params(self):
        return {"power_dbm": self.sp_power.value(), "if_bw": self.sp_ifbw.value(),
                "averages": self.sp_avg.value(), "avg_enabled": self.sp_avg.value() > 1,
                "points": self.sp_points.value(), "trace": "S21"}

    def _run(self):
        todo = [r for r in self._res if r.get("f_hz") is None or not len(r["f_hz"])]
        self._start_worker(todo, "Running quality sweep…")

    def _remeasure(self):
        r = self._current()
        if r is None:
            return
        from windows.dialogs import ReSpanDialog
        dlg = ReSpanDialog(self, r)
        if not (dlg.exec_() and dlg.result_value):
            return
        nv = dlg.result_value
        r["center_hz"] = nv["center_hz"]
        r["fstart_hz"] = nv["fstart_hz"]
        r["fstop_hz"] = nv["fstop_hz"]
        r["span_mhz"] = nv["span_mhz"]
        self._log(f"Res {r['num']}: new span {nv['span_mhz']:.3f} MHz "
                  f"around {nv['center_hz']/1e9:.6f} GHz — re-measuring…")
        self._remeasure_num = r["num"]
        self._start_worker([r], f"Re-measuring Res {r['num']} with new span…")

    def _start_worker(self, subset, msg):
        if not instrument_manager.pna_connected():
            self._log("✗ PNA not connected."); return
        if instrument_manager.busy:
            return
        if not subset:
            self._log("Nothing to measure (all resonators already have data)."); return
        settings.remember("quality", {
            "assess_power_dbm": self.sp_power.value(), "assess_if_bw": self.sp_ifbw.value(),
            "assess_averages": self.sp_avg.value(), "points": self.sp_points.value()})
        instrument_manager.set_busy(True)
        self.btn_run.setEnabled(False); self.btn_stop.setEnabled(True)
        self._log(msg)
        self._worker = QualityWorker(instrument_manager, subset, self._params())
        self._worker.progress.connect(self._log)
        self._worker.res_measured.connect(self._on_measured)
        self._worker.finished.connect(self._on_finished)
        self._worker.aborted.connect(self._on_aborted)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.abort(); self.btn_stop.setEnabled(False)
            self._log("Stopping… (aborting the PNA sweep)")

    def _on_measured(self, item: dict):
        num = item["num"]
        r = next((x for x in self._res if x["num"] == num), None)
        if r is None:
            return
        r["f_hz"] = item["f_hz"]; r["mag_db"] = item["mag_db"]; r["phase_deg"] = item["phase_deg"]
        r["run_id"] = item["run_id"]; r["power_dbm"] = item["power_dbm"]
        r["z"] = s21_from_mag_phase(item["mag_db"], item["phase_deg"])
        r["fit"] = fit_notch(item["f_hz"], r["z"])           # auto-preview fit
        r["state"] = "fitted" if r["fit"].get("ok") else "measured"
        self._refresh_list()
        # surface the just-measured resonator so it can be analysed while the
        # remaining ones are still being swept
        if self._current() is r or self.list.currentRow() < 0:
            idx = self._res.index(r)
            self.list.setCurrentRow(idx)
            self._show(r)

    def _on_finished(self, results):
        self._remeasure_num = None
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log(f"✓ Sweep complete ({len(results)} resonator(s)). "
                  "Select each, Run fit, then Confirm / Ignore / Delete.")
        if self.list.currentRow() < 0 and self._res:
            self.list.setCurrentRow(0)

    def _on_aborted(self):
        self._remeasure_num = None
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log("■ Stopped. Measured resonators are kept; the rest stay unmeasured.")

    def _on_error(self, tb):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log("✗ Error: " + tb.splitlines()[-1]); logger.error(tb)

    # ------------------------------------------------------------------
    # fit + decisions
    # ------------------------------------------------------------------

    def _run_fit(self):
        r = self._current()
        if r is None or r.get("z") is None:
            self._log("Select a measured resonator first."); return
        crop = self.fitplot.get_crop_hz()
        r["fit"] = fit_notch(r["f_hz"], r["z"], crop_hz=crop)
        if r["state"] not in ("confirmed", "ignored"):
            r["state"] = "fitted" if r["fit"].get("ok") else "measured"
        self._show(r); self._refresh_list()

    def _confirm(self):
        r = self._current()
        if r is None:
            return
        if not (r.get("fit") or {}).get("ok"):
            QMessageBox.information(self, "Run fit first",
                                   "Run a successful fit before confirming this resonator.")
            return
        r["state"] = "confirmed"; self._refresh_list()
        self._log(f"✓ Res {r['num']} confirmed.")

    def _ignore(self):
        r = self._current()
        if r is None:
            return
        if QMessageBox.question(
                self, "Ignore resonator",
                f"Ignore Res {r['num']}?\n\nIt will be SKIPPED in the power- and "
                f"temperature-dependent runs, but its number {r['num']} stays "
                f"reserved (not reused).",
                QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        r["state"] = "ignored"; self._refresh_list()
        self._log(f"Res {r['num']} ignored (number reserved/skipped).")

    def _delete(self):
        r = self._current()
        if r is None:
            return
        if QMessageBox.question(
                self, "Delete resonator",
                f"Delete Res {r['num']} from the workflow?\n\nThe saved run STAYS in "
                f"the database. Number {r['num']} is FREED and will be given to the "
                f"next resonator measured or loaded.",
                QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        num = r["num"]
        self._res.remove(r)
        if num is not None and num not in self._free_nums:
            self._free_nums.append(num); self._free_nums.sort()
        self._log(f"Res {num} deleted from workflow; number {num} freed. (Run kept in DB.)")
        self._refresh_list()
        self.lbl_head.setText("Select a resonator."); self.lbl_metrics.setText("")
        self.fitplot.set_data([], [], [])

    # ------------------------------------------------------------------
    # load from database
    # ------------------------------------------------------------------

    def _load_db(self):
        if instrument_manager.busy:
            return
        dlg = QualityRunPicker(self, single=False)
        if dlg.exec_() and dlg.result_value:
            db_path, run_ids = dlg.result_value
            added = 0
            for rid in run_ids:
                try:
                    res = aio.run_as_resonator(db_path, rid)
                except Exception as e:
                    self._log(f"✗ run {rid}: {e}"); continue
                res["num"] = self._assign_num(res.get("num"))
                res["z"] = s21_from_mag_phase(res["mag_db"], res["phase_deg"])
                res["fit"] = fit_notch(res["f_hz"], res["z"])
                res["state"] = "fitted" if res["fit"].get("ok") else "measured"
                self._res.append(res); added += 1
            self._log(f"Loaded {added} quality run(s) from database. "
                      "Select each, Run fit / adjust, then Confirm / Ignore / Delete.")
            self._refresh_list()
            if self.list.currentRow() < 0 and self._res:
                self.list.setCurrentRow(0)

    # ------------------------------------------------------------------

    def _continue(self):
        chosen = []
        for r in self._res:
            if r["state"] != "confirmed":
                continue
            fit = r.get("fit") or {}
            chosen.append({
                "num": r["num"], "center_hz": float(r["center_hz"]),
                "fstart_hz": float(r["fstart_hz"]), "fstop_hz": float(r["fstop_hz"]),
                "span_mhz": float(r.get("span_mhz", 0)),
                "fr": float(fit["fr"]) if fit.get("ok") else float(r["center_hz"]),
                "Qi": float(fit["Qi"]) if fit.get("ok") else None,
                "Ql": float(fit["Ql"]) if fit.get("ok") else None,
                "theta0": float(fit.get("theta0", 0.0)) if fit.get("ok") else 0.0,
            })
        if not chosen:
            return
        settings.set_list("power_resonators", chosen)
        self.resonatorsForPower.emit(chosen)
        self._log(f"Selected {len(chosen)} resonator(s) for power-dependent.")

    def _on_busy(self, busy):
        self.btn_run.setEnabled(not busy and instrument_manager.pna_connected())
        for w in (self.sp_power, self.sp_ifbw, self.sp_avg, self.sp_points,
                  self.btn_load, self.btn_remeas, self.btn_delete):
            w.setEnabled(not busy)
        self._update_gate()

    def _log(self, m):
        self.log.append(m); logger.info(m)
