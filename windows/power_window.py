"""
windows/power_window.py
=======================
Step 4: power-dependent measurement.

* Power vector from start/stop/step.
* Averaging schedule as an editable table (power · averages · IF bw). Fill it
  two ways: type values directly, or use the rule fields (averages & IF bw at
  the lowest and highest power) which geometrically interpolate across the
  vector and pre-fill the table — then edit any cell.
* Sweep mode toggle: SPD (linear, low→high; default) or HPD (segment sweep,
  high→low; table regenerated per power from the running fit, seeded by the
  Phase-6 quality fit). HPD also exposes the Qi-jump reject factor.
* Live Qi-vs-power plot updates after every power point.
* Continue hands the measured resonators to the temperature step (Phase 7b).
"""

from __future__ import annotations

import logging
from typing import List, Dict

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QDoubleSpinBox, QSpinBox, QComboBox, QPushButton, QListWidget,
    QListWidgetItem, QTableWidget, QTableWidgetItem, QTextEdit, QSplitter,
)
from PyQt5.QtCore import Qt, pyqtSignal

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.measure_workers import PowerWorker
from core.fitting import format_q, fit_notch, s21_from_mag_phase
from windows.dialogs import QualityRunPicker
from core import analysis_io as aio

logger = logging.getLogger(__name__)
pg.setConfigOptions(antialias=True, background=None, foreground="#cdd6f4")


class PowerWindow(QMainWindow):
    resonatorsForTemperature = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("4 · Power-Dependent Measurement")
        self.setMinimumSize(1180, 760)
        self._resonators: List[Dict] = []
        self._res_visible: List[Dict] = []
        self._worker = None
        self._curves: Dict[int, Dict] = {}   # num -> {'powers':[], 'qis':[], 'curve':PlotDataItem}
        self._build_ui()
        instrument_manager.on_busy_changed(self._on_busy)

    def load(self, resonators: List[Dict]):
        self._resonators = [dict(r) for r in resonators]
        for r in self._resonators:
            r.setdefault("chip", "")
            r.setdefault("_checked", True)
        self._rebuild_chip_filter()
        self._refresh_res_list()

    def _res_label(self, r):
        chip = r.get("chip") or ""
        span_khz = abs(float(r["fstop_hz"]) - float(r["fstart_hz"])) / 1e3
        head = f"{chip}_Res{r['num']}" if chip else f"Res {r['num']}"
        return (f"{head}  {r.get('fr', r['center_hz'])/1e9:.6f} GHz "
                f"· Qi={format_q(r.get('Qi'))}  · span={span_khz:.1f} kHz")

    def _rebuild_chip_filter(self):
        chips = []
        for r in self._resonators:
            c = r.get("chip") or ""
            if c not in chips:
                chips.append(c)
        cur = self.cmb_chip.currentText() if hasattr(self, "cmb_chip") else ""
        self.cmb_chip.blockSignals(True)
        self.cmb_chip.clear(); self.cmb_chip.addItem("All chips")
        for c in chips:
            self.cmb_chip.addItem(c if c else "(unnamed)")
        idx = self.cmb_chip.findText(cur)
        self.cmb_chip.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_chip.blockSignals(False)

    def _chip_filter(self):
        if self.cmb_chip.currentIndex() <= 0:
            return None
        txt = self.cmb_chip.currentText()
        return "" if txt == "(unnamed)" else txt

    def _refresh_res_list(self):
        flt = self._chip_filter()
        self._res_visible = [r for r in self._resonators
                             if flt is None or (r.get("chip") or "") == flt]
        self.res_list.blockSignals(True)
        self.res_list.clear()
        for r in self._res_visible:
            it = QListWidgetItem(self._res_label(r))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r.get("_checked", True) else Qt.Unchecked)
            self.res_list.addItem(it)
        self.res_list.blockSignals(False)

    def _on_res_item_changed(self, item):
        row = self.res_list.row(item)
        if 0 <= row < len(self._res_visible):
            self._res_visible[row]["_checked"] = (item.checkState() == Qt.Checked)

    def _load_from_db(self):
        dlg = QualityRunPicker(self, single=False)
        if not (dlg.exec_() and dlg.result_value):
            return
        db_path, run_ids = dlg.result_value
        used = {r["num"] for r in self._resonators if r.get("num") is not None}
        added = 0
        for rid in run_ids:
            try:
                base = aio.run_as_resonator(db_path, rid)
                fit = fit_notch(base["f_hz"], s21_from_mag_phase(base["mag_db"], base["phase_deg"]))
            except Exception as e:
                self._log(f"✗ run {rid}: {e}"); continue
            if not fit.get("ok"):
                self._log(f"⚠ run {rid}: auto-fit failed; refine it on the Quality page first.")
                continue
            num = base.get("num")
            if num is None or num in used:
                num = (max(used) + 1) if used else 1
            used.add(num)
            chip = base.get("chip") or (base.get("name","").split("_Res")[0]
                                        if "_Res" in base.get("name","") else "")
            r = {"num": num, "chip": chip, "_checked": True,
                 "center_hz": base["center_hz"],
                 "fstart_hz": base["fstart_hz"], "fstop_hz": base["fstop_hz"],
                 "span_mhz": base["span_mhz"], "fr": fit["fr"], "Qi": fit["Qi"],
                 "Ql": fit["Ql"], "theta0": fit.get("theta0", 0.0)}
            self._resonators.append(r); added += 1
        self._rebuild_chip_filter(); self._refresh_res_list()
        self._log(f"Loaded {added} resonator(s) from database.")

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget(); outer = QVBoxLayout(root); self.setCentralWidget(root)
        split = QSplitter(Qt.Horizontal); outer.addWidget(split, 1)

        left = QWidget(); L = QVBoxLayout(left); L.setContentsMargins(10, 10, 10, 10)
        b = settings.block("power")

        # mode + power vector
        grp = QGroupBox("Sweep")
        g = QGridLayout(grp)
        self.cmb_mode = QComboBox(); self.cmb_mode.addItems(["SPD (linear, low→high)", "HPD (segment, high→low)"])
        self.cmb_mode.setCurrentIndex(int(b.get("mode_hpd", 0)))
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        g.addWidget(QLabel("Mode"), 0, 0); g.addWidget(self.cmb_mode, 0, 1, 1, 3)
        self.sp_pstart = QDoubleSpinBox(); self.sp_pstart.setRange(-90, 30); self.sp_pstart.setValue(float(b.get("p_start", -30)))
        self.sp_pstop = QDoubleSpinBox(); self.sp_pstop.setRange(-90, 30); self.sp_pstop.setValue(float(b.get("p_stop", 0)))
        self.sp_pstep = QDoubleSpinBox(); self.sp_pstep.setRange(0.1, 50); self.sp_pstep.setValue(float(b.get("p_step", 5)))
        g.addWidget(QLabel("P start"), 1, 0); g.addWidget(self.sp_pstart, 1, 1)
        g.addWidget(QLabel("P stop"), 1, 2); g.addWidget(self.sp_pstop, 1, 3)
        g.addWidget(QLabel("P step"), 2, 0); g.addWidget(self.sp_pstep, 2, 1)
        self.sp_points = QSpinBox(); self.sp_points.setRange(11, 100001); self.sp_points.setValue(int(b.get("points", 2001)))
        g.addWidget(QLabel("Points"), 2, 2); g.addWidget(self.sp_points, 2, 3)
        self.sp_reject = QDoubleSpinBox(); self.sp_reject.setRange(2, 100); self.sp_reject.setValue(float(b.get("qi_reject_factor", 7)))
        self.lbl_reject = QLabel("Qi reject ×")
        g.addWidget(self.lbl_reject, 3, 0); g.addWidget(self.sp_reject, 3, 1)
        self.btn_gen = QPushButton("Generate table"); self.btn_gen.clicked.connect(self._generate_table)
        g.addWidget(self.btn_gen, 3, 2, 1, 2)
        L.addWidget(grp)

        # rule
        rule = QGroupBox("Averaging / IF-bandwidth rule (interpolated across power)")
        rg = QGridLayout(rule)
        self.sp_avg_lo = QSpinBox(); self.sp_avg_lo.setRange(1, 15); self.sp_avg_lo.setValue(int(b.get("avg_low", 15)))
        self.sp_avg_hi = QSpinBox(); self.sp_avg_hi.setRange(1, 15); self.sp_avg_hi.setValue(int(b.get("avg_high", 1)))
        self.sp_bw_lo = QSpinBox(); self.sp_bw_lo.setRange(1, 1000); self.sp_bw_lo.setValue(int(b.get("ifbw_low", 10)))
        self.sp_bw_hi = QSpinBox(); self.sp_bw_hi.setRange(1, 1000); self.sp_bw_hi.setValue(int(b.get("ifbw_high", 1000)))
        rg.addWidget(QLabel("Avg @ low P"), 0, 0); rg.addWidget(self.sp_avg_lo, 0, 1)
        rg.addWidget(QLabel("Avg @ high P"), 0, 2); rg.addWidget(self.sp_avg_hi, 0, 3)
        rg.addWidget(QLabel("IFbw @ low P"), 1, 0); rg.addWidget(self.sp_bw_lo, 1, 1)
        rg.addWidget(QLabel("IFbw @ high P"), 1, 2); rg.addWidget(self.sp_bw_hi, 1, 3)
        self.btn_rule = QPushButton("Apply rule → table"); self.btn_rule.clicked.connect(self._apply_rule)
        rg.addWidget(self.btn_rule, 2, 0, 1, 4)
        L.addWidget(rule)

        # schedule table
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Power (dBm)", "Averages", "IF bw (Hz)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        L.addWidget(self.table, 1)

        trow = QHBoxLayout()
        self.btn_modify = QPushButton("Modify table…"); self.btn_modify.clicked.connect(self._modify_table)
        self.btn_savecfg = QPushButton("Save config…"); self.btn_savecfg.clicked.connect(self._save_config)
        self.btn_loadcfg = QPushButton("Load config…"); self.btn_loadcfg.clicked.connect(self._load_config)
        for x in (self.btn_modify, self.btn_savecfg, self.btn_loadcfg):
            trow.addWidget(x)
        L.addLayout(trow)

        # resonators
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Chip"))
        self.cmb_chip = QComboBox(); self.cmb_chip.addItem("All chips")
        self.cmb_chip.currentIndexChanged.connect(lambda *_: self._refresh_res_list())
        crow.addWidget(self.cmb_chip, 1)
        L.addLayout(crow)
        L.addWidget(QLabel("Resonators to run"))
        self.res_list = QListWidget(); self.res_list.setMaximumHeight(120)
        self.res_list.itemChanged.connect(self._on_res_item_changed)
        L.addWidget(self.res_list)
        rrow = QHBoxLayout()
        self.btn_loaddb = QPushButton("Load resonator(s) from DB…")
        self.btn_loaddb.clicked.connect(self._load_from_db)
        self.btn_spans = QPushButton("Edit spans (kHz)…")
        self.btn_spans.clicked.connect(self._edit_spans)
        rrow.addWidget(self.btn_loaddb); rrow.addWidget(self.btn_spans)
        L.addLayout(rrow)

        brow = QHBoxLayout()
        self.btn_run = QPushButton("Run power sweep"); self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run); brow.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop"); self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self._stop); brow.addWidget(self.btn_stop)
        L.addLayout(brow)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(120); L.addWidget(self.log)
        split.addWidget(left)

        # right: live Qi vs power
        right = QWidget(); R = QVBoxLayout(right); R.setContentsMargins(10, 10, 10, 10)
        R.addWidget(QLabel("Internal quality factor vs power (live)"))
        self.qi_plot = pg.PlotWidget()
        self.qi_plot.setLabel("bottom", "PNA power", units="dBm")
        self.qi_plot.setLabel("left", "Qi")
        self.qi_plot.setLogMode(x=False, y=True)
        self.qi_plot.showGrid(x=True, y=True, alpha=0.3)
        self.qi_plot.addLegend()
        R.addWidget(self.qi_plot, 1)
        b2 = QHBoxLayout(); b2.addStretch()
        self.btn_cont = QPushButton("Continue  →  Temperature-dependent")
        self.btn_cont.setObjectName("success"); self.btn_cont.clicked.connect(self._continue)
        b2.addWidget(self.btn_cont)
        R.addLayout(b2)
        split.addWidget(right); split.setStretchFactor(1, 1)

        self._mode_changed()
        self._generate_table()

    # ------------------------------------------------------------------

    def _is_hpd(self):
        return self.cmb_mode.currentIndex() == 1

    def _mode_changed(self):
        hpd = self._is_hpd()
        self.lbl_reject.setVisible(hpd); self.sp_reject.setVisible(hpd)

    def _power_vector(self):
        a, b, s = self.sp_pstart.value(), self.sp_pstop.value(), self.sp_pstep.value()
        lo, hi = min(a, b), max(a, b)
        n = int(round((hi - lo) / s)) + 1
        return [round(lo + i * s, 3) for i in range(max(n, 1))]

    def _generate_table(self):
        powers = self._power_vector()
        self.table.setRowCount(len(powers))
        for i, pw in enumerate(powers):
            self.table.setItem(i, 0, QTableWidgetItem(f"{pw:g}"))
            if self.table.item(i, 1) is None:
                self.table.setItem(i, 1, QTableWidgetItem("1"))
            if self.table.item(i, 2) is None:
                self.table.setItem(i, 2, QTableWidgetItem("1000"))
        self._apply_rule()

    def _apply_rule(self):
        powers = [float(self.table.item(i, 0).text()) for i in range(self.table.rowCount())]
        if not powers:
            return
        p_lo, p_hi = min(powers), max(powers)
        a_lo, a_hi = self.sp_avg_lo.value(), self.sp_avg_hi.value()
        b_lo, b_hi = self.sp_bw_lo.value(), self.sp_bw_hi.value()
        span = (p_hi - p_lo) or 1.0
        for i, pw in enumerate(powers):
            frac = (pw - p_lo) / span
            av = int(round(a_lo * (a_hi / a_lo) ** frac)) if a_lo > 0 else a_hi
            bw = int(round(b_lo * (b_hi / b_lo) ** frac)) if b_lo > 0 else b_hi
            av = int(np.clip(av, 1, 15))
            bw = int(np.clip(bw, 1, 1000))
            self.table.setItem(i, 1, QTableWidgetItem(str(av)))
            self.table.setItem(i, 2, QTableWidgetItem(str(bw)))

    def _schedule(self):
        out = []
        for i in range(self.table.rowCount()):
            try:
                pw = float(self.table.item(i, 0).text())
                av = int(np.clip(int(float(self.table.item(i, 1).text())), 1, 100000))
                bw = int(np.clip(int(float(self.table.item(i, 2).text())), 1, 15000000))
                out.append((pw, av, bw))
            except Exception:
                continue
        return out

    def _fill_table(self, schedule):
        self.table.setRowCount(len(schedule))
        for i, (pw, av, bw) in enumerate(schedule):
            self.table.setItem(i, 0, QTableWidgetItem(f"{pw:g}"))
            self.table.setItem(i, 1, QTableWidgetItem(str(int(av))))
            self.table.setItem(i, 2, QTableWidgetItem(str(int(bw))))

    def _modify_table(self):
        from windows.dialogs import PowerScheduleDialog
        try:
            dlg = PowerScheduleDialog(self, self._schedule())
            if dlg.exec_() and dlg.result_value:
                self._fill_table(dlg.result_value)
                self._log(f"Power schedule updated: {len(dlg.result_value)} point(s).")
        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Power schedule error",
                                f"Could not apply the modified table:\n{e}")
            self._log(f"✗ Power schedule error: {e}")

    def _save_config(self):
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        from core.schedule_io import save_schedule
        sched = self._schedule()
        if not sched:
            QMessageBox.warning(self, "Nothing to save", "The power table is empty."); return
        path, _ = QFileDialog.getSaveFileName(self, "Save power config",
                                              "power_schedule.json", "JSON config (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            save_schedule(path, sched); self._log(f"Saved power config → {path}")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _load_config(self):
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        from core.schedule_io import load_schedule
        path, _ = QFileDialog.getOpenFileName(self, "Load power config",
                                              "", "JSON config (*.json);;All files (*)")
        if not path:
            return
        try:
            sched = load_schedule(path)
            self._fill_table(sched); self._log(f"Loaded power config ({len(sched)} points) ← {path}")
        except Exception as e:
            QMessageBox.warning(self, "Load failed", str(e))
            self._log(f"✗ Config load error: {e}")

    def _edit_spans(self):
        from windows.dialogs import ResonatorSpanDialog
        from PyQt5.QtWidgets import QMessageBox
        if not self._resonators:
            QMessageBox.information(self, "No resonators",
                                    "Load or receive resonators first."); return
        dlg = ResonatorSpanDialog(self, self._resonators)
        if not (dlg.exec_() and dlg.result_value):
            return
        for r in self._resonators:
            span_hz = dlg.result_value.get(r.get("num"))
            if not span_hz:
                continue
            center = float(r.get("center_hz", 0.5 * (r["fstart_hz"] + r["fstop_hz"])))
            r["center_hz"] = center
            r["fstart_hz"] = center - span_hz / 2.0
            r["fstop_hz"] = center + span_hz / 2.0
            r["span_mhz"] = span_hz / 1e6
        self._refresh_res_list()
        self._log("Updated resonator spans (centre unchanged).")

    def _checked_resonators(self):
        # checked resonators within the current chip view
        return [r for r in self._res_visible if r.get("_checked", True)]

    # ------------------------------------------------------------------

    def _run(self):
        if not instrument_manager.pna_connected():
            self._log("✗ PNA not connected."); return
        if instrument_manager.busy:
            return
        resonators = self._checked_resonators()
        schedule = self._schedule()
        if not resonators or not schedule:
            self._log("Need at least one resonator and one power point."); return
        settings.remember("power", {
            "mode_hpd": 1 if self._is_hpd() else 0,
            "p_start": self.sp_pstart.value(), "p_stop": self.sp_pstop.value(),
            "p_step": self.sp_pstep.value(), "points": self.sp_points.value(),
            "qi_reject_factor": self.sp_reject.value(),
            "avg_low": self.sp_avg_lo.value(), "avg_high": self.sp_avg_hi.value(),
            "ifbw_low": self.sp_bw_lo.value(), "ifbw_high": self.sp_bw_hi.value(),
        })
        # reset plot
        self.qi_plot.clear(); self._curves = {}
        params = {
            "mode": "hpd" if self._is_hpd() else "spd",
            "schedule": schedule, "points": self.sp_points.value(),
            "qi_reject_factor": self.sp_reject.value(), "trace": "S21",
        }
        instrument_manager.set_busy(True)
        self.btn_run.setEnabled(False); self.btn_stop.setEnabled(True)
        self._log(f"Starting {'HPD' if self._is_hpd() else 'SPD'} power sweep "
                  f"on {len(resonators)} resonator(s)…")
        self._worker = PowerWorker(instrument_manager, resonators, params)
        self._worker.progress.connect(self._log)
        self._worker.point_measured.connect(self._on_point)
        self._worker.res_finished.connect(lambda d: self._log(f"✓ Res {d['num']} done."))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.abort(); self._log("Stop requested…")

    def _color(self, num):
        palette = ["#89b4fa", "#f38ba8", "#a6e3a1", "#f9e2af", "#cba6f7",
                   "#94e2d5", "#fab387", "#74c7ec"]
        return palette[num % len(palette)]

    def _on_point(self, d: dict):
        num = d["num"]; pw = d["power_dbm"]; qi = d.get("Qi"); qe = d.get("Qi_err")
        if num not in self._curves:
            c = self.qi_plot.plot([], [], pen=pg.mkPen(self._color(num), width=2),
                                  symbol='o', symbolSize=6,
                                  symbolBrush=self._color(num), name=f"Res {num}")
            eb = pg.ErrorBarItem(x=np.array([]), y=np.array([]),
                                 top=np.array([]), bottom=np.array([]),
                                 pen=pg.mkPen(self._color(num), width=1), beam=0.0)
            self.qi_plot.addItem(eb)
            self._curves[num] = {"powers": [], "qis": [], "errs": [], "curve": c, "eb": eb}
        if d.get("fit_ok") and qi and 1e2 <= qi <= 1e10:
            self._curves[num]["powers"].append(pw)
            self._curves[num]["qis"].append(qi)
            self._curves[num]["errs"].append(qe if (qe and np.isfinite(qe)) else 0.0)
            order = np.argsort(self._curves[num]["powers"])
            xs = np.array(self._curves[num]["powers"])[order]
            ys = np.array(self._curves[num]["qis"])[order]
            es = np.array(self._curves[num]["errs"])[order]
            # A poorly conditioned circle fit can report an enormous Qi error
            # (e.g. 1e27). On a log axis that whisker would rescale the whole
            # plot and hide the real points, so cap it for display (the true
            # value is still printed in the log below).
            es = np.clip(es, 0.0, ys * 0.9)
            self._curves[num]["curve"].setData(xs, ys)
            self._curves[num]["eb"].setData(x=xs, y=ys, top=es, bottom=es, beam=0.0)
        tag = " (reused seed)" if d.get("reused") else ""
        tag += " (SPD fallback)" if d.get("fallback_spd") else ""
        qitxt = (f"Qi={format_q(qi)}±{format_q(qe)}{tag}"
                 if d.get("fit_ok") else f"fit failed{tag}")
        self._log(f"  Res {num} @ {pw:g} dBm: " + qitxt)

    def _on_finished(self, results):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log(f"✓ Power sweep complete ({len(results)} resonator(s)).")

    def _on_error(self, tb):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log("✗ Error: " + tb.splitlines()[-1]); logger.error(tb)

    def _continue(self):
        chosen = self._checked_resonators()
        if chosen:
            settings.set_list("temperature_resonators", chosen)
            self.resonatorsForTemperature.emit(chosen)
            self._log(f"Sent {len(chosen)} resonator(s) to the temperature step.")

    def _on_busy(self, busy):
        self.btn_run.setEnabled(not busy and instrument_manager.pna_connected())
        for w in (self.cmb_mode, self.sp_pstart, self.sp_pstop, self.sp_pstep,
                  self.sp_points, self.btn_gen, self.btn_rule, self.table,
                  self.btn_modify, self.btn_savecfg, self.btn_loadcfg,
                  self.btn_spans, self.btn_loaddb):
            w.setEnabled(not busy)

    def _log(self, m):
        self.log.append(m); logger.info(m)
