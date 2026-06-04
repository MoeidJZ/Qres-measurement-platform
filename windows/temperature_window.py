"""
windows/temperature_window.py
=============================
Step 5: temperature- (and power-) dependent measurement.

At each target temperature the controller setpoint is set *and read back to
confirm it matches* (the fix for the setpoint/target mismatch), the loop waits
for stability, then a full power sweep runs (SPD or HPD, same machinery as the
power step). Every temperature produces its own run id(s), with the temperature
encoded in each run name.
"""

from __future__ import annotations

import logging
from typing import List, Dict

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QDoubleSpinBox, QSpinBox, QComboBox, QCheckBox, QPushButton,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QTextEdit,
    QSplitter,
)
from PyQt5.QtCore import Qt

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.measure_workers import TemperatureWorker, format_temp_label
from core.fitting import format_q, fit_notch, s21_from_mag_phase
from windows.dialogs import QualityRunPicker
from core import analysis_io as aio
from core import theme

logger = logging.getLogger(__name__)
pg.setConfigOptions(antialias=True, background=None, foreground="#cdd6f4")


class TemperatureWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("5 · Temperature- & Power-Dependent Measurement")
        self.setMinimumSize(1220, 800)
        self._resonators: List[Dict] = []
        self._worker = None
        self._curves: Dict[str, Dict] = {}
        self._build_ui()
        instrument_manager.on_busy_changed(self._on_busy)

    def load(self, resonators: List[Dict]):
        self._resonators = [dict(r) for r in resonators]
        self.res_list.clear()
        for r in self._resonators:
            self._add_res_item(r)

    def _add_res_item(self, r):
        it = QListWidgetItem(f"Res {r['num']}  {r.get('fr', r['center_hz'])/1e9:.6f} GHz")
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        self.res_list.addItem(it)

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
            r = {"num": num, "center_hz": base["center_hz"],
                 "fstart_hz": base["fstart_hz"], "fstop_hz": base["fstop_hz"],
                 "span_mhz": base["span_mhz"], "fr": fit["fr"], "Qi": fit["Qi"],
                 "Ql": fit["Ql"], "theta0": fit.get("theta0", 0.0)}
            self._resonators.append(r); self._add_res_item(r); added += 1
        self._log(f"Loaded {added} resonator(s) from database.")

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget(); outer = QVBoxLayout(root); self.setCentralWidget(root)
        split = QSplitter(Qt.Horizontal); outer.addWidget(split, 1)
        left = QWidget(); L = QVBoxLayout(left); L.setContentsMargins(10, 10, 10, 10)
        b = settings.block("temperature")

        # temperature vector
        tg = QGroupBox("Temperatures")
        t = QGridLayout(tg)
        self.sp_tstart = QDoubleSpinBox(); self.sp_tstart.setRange(0, 400); self.sp_tstart.setDecimals(3); self.sp_tstart.setValue(float(b.get("t_start", 15)))
        self.sp_tstop = QDoubleSpinBox(); self.sp_tstop.setRange(0, 400); self.sp_tstop.setDecimals(3); self.sp_tstop.setValue(float(b.get("t_stop", 300)))
        self.sp_tstep = QDoubleSpinBox(); self.sp_tstep.setRange(0.001, 400); self.sp_tstep.setDecimals(3); self.sp_tstep.setValue(float(b.get("t_step", 50)))
        self.cmb_tunit = QComboBox(); self.cmb_tunit.addItems(["mK", "K"]); self.cmb_tunit.setCurrentText(b.get("t_unit", "mK"))
        self.chk_reverse = QCheckBox("High → low"); self.chk_reverse.setChecked(bool(b.get("reverse", False)))
        t.addWidget(QLabel("Start"), 0, 0); t.addWidget(self.sp_tstart, 0, 1)
        t.addWidget(QLabel("Stop"), 0, 2); t.addWidget(self.sp_tstop, 0, 3)
        t.addWidget(QLabel("Step"), 1, 0); t.addWidget(self.sp_tstep, 1, 1)
        t.addWidget(QLabel("Unit"), 1, 2); t.addWidget(self.cmb_tunit, 1, 3)
        t.addWidget(self.chk_reverse, 2, 0, 1, 2)
        self.lbl_temps = QLabel(""); self.lbl_temps.setStyleSheet(f"color:{theme.hx('muted')};")
        t.addWidget(self.lbl_temps, 2, 2, 1, 2)
        for w in (self.sp_tstart, self.sp_tstop, self.sp_tstep):
            w.valueChanged.connect(self._update_temps_label)
        self.cmb_tunit.currentIndexChanged.connect(self._update_temps_label)
        L.addWidget(tg)

        # stability
        sg = QGroupBox("Stability criterion")
        s = QGridLayout(sg)
        self.sp_mean = QDoubleSpinBox(); self.sp_mean.setRange(0.01, 100); self.sp_mean.setDecimals(2); self.sp_mean.setSuffix(" mK"); self.sp_mean.setValue(float(b.get("stable_mean_mk", 2)))
        self.sp_std = QDoubleSpinBox(); self.sp_std.setRange(0.01, 100); self.sp_std.setDecimals(2); self.sp_std.setSuffix(" mK"); self.sp_std.setValue(float(b.get("stable_std_mk", 2)))
        self.sp_win = QSpinBox(); self.sp_win.setRange(5, 500); self.sp_win.setValue(int(b.get("window", 30)))
        self.sp_poll = QDoubleSpinBox(); self.sp_poll.setRange(0.5, 120); self.sp_poll.setSuffix(" s"); self.sp_poll.setValue(float(b.get("poll_s", 5)))
        self.sp_timeout = QSpinBox(); self.sp_timeout.setRange(0, 1440); self.sp_timeout.setSuffix(" min"); self.sp_timeout.setValue(int(b.get("timeout_min", 0)))
        s.addWidget(QLabel("|mean−T|<"), 0, 0); s.addWidget(self.sp_mean, 0, 1)
        s.addWidget(QLabel("std <"), 0, 2); s.addWidget(self.sp_std, 0, 3)
        s.addWidget(QLabel("window"), 1, 0); s.addWidget(self.sp_win, 1, 1)
        s.addWidget(QLabel("poll"), 1, 2); s.addWidget(self.sp_poll, 1, 3)
        s.addWidget(QLabel("timeout (0=none)"), 2, 0); s.addWidget(self.sp_timeout, 2, 1)
        L.addWidget(sg)

        # sweep (power) — same controls as the power step
        wg = QGroupBox("Power sweep at each temperature")
        w = QGridLayout(wg)
        bp = settings.block("power")
        self.cmb_mode = QComboBox(); self.cmb_mode.addItems(["SPD (low→high)", "HPD (high→low)"])
        self.cmb_mode.setCurrentIndex(int(bp.get("mode_hpd", 0)))
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        w.addWidget(QLabel("Mode"), 0, 0); w.addWidget(self.cmb_mode, 0, 1, 1, 3)
        self.sp_pstart = QDoubleSpinBox(); self.sp_pstart.setRange(-90, 30); self.sp_pstart.setValue(float(bp.get("p_start", -30)))
        self.sp_pstop = QDoubleSpinBox(); self.sp_pstop.setRange(-90, 30); self.sp_pstop.setValue(float(bp.get("p_stop", 0)))
        self.sp_pstep = QDoubleSpinBox(); self.sp_pstep.setRange(0.1, 50); self.sp_pstep.setValue(float(bp.get("p_step", 5)))
        self.sp_points = QSpinBox(); self.sp_points.setRange(11, 100001); self.sp_points.setValue(int(bp.get("points", 2001)))
        w.addWidget(QLabel("P start"), 1, 0); w.addWidget(self.sp_pstart, 1, 1)
        w.addWidget(QLabel("P stop"), 1, 2); w.addWidget(self.sp_pstop, 1, 3)
        w.addWidget(QLabel("P step"), 2, 0); w.addWidget(self.sp_pstep, 2, 1)
        w.addWidget(QLabel("Points"), 2, 2); w.addWidget(self.sp_points, 2, 3)
        self.lbl_reject = QLabel("Qi reject ×"); self.sp_reject = QDoubleSpinBox()
        self.sp_reject.setRange(2, 100); self.sp_reject.setValue(float(bp.get("qi_reject_factor", 7)))
        w.addWidget(self.lbl_reject, 3, 0); w.addWidget(self.sp_reject, 3, 1)
        self.btn_gen = QPushButton("Generate table"); self.btn_gen.clicked.connect(self._generate_table)
        w.addWidget(self.btn_gen, 3, 2, 1, 2)
        L.addWidget(wg)

        # rule + table (avg 1-15, ifbw 1-1000)
        rg = QGroupBox("Averaging / IF-bw rule  (avg 1–15, IFbw 1–1000 Hz)")
        r = QGridLayout(rg)
        self.sp_avg_lo = QSpinBox(); self.sp_avg_lo.setRange(1, 15); self.sp_avg_lo.setValue(int(bp.get("avg_low", 15)))
        self.sp_avg_hi = QSpinBox(); self.sp_avg_hi.setRange(1, 15); self.sp_avg_hi.setValue(int(bp.get("avg_high", 1)))
        self.sp_bw_lo = QSpinBox(); self.sp_bw_lo.setRange(1, 1000); self.sp_bw_lo.setValue(int(bp.get("ifbw_low", 10)))
        self.sp_bw_hi = QSpinBox(); self.sp_bw_hi.setRange(1, 1000); self.sp_bw_hi.setValue(int(bp.get("ifbw_high", 1000)))
        r.addWidget(QLabel("Avg @ low P"), 0, 0); r.addWidget(self.sp_avg_lo, 0, 1)
        r.addWidget(QLabel("Avg @ high P"), 0, 2); r.addWidget(self.sp_avg_hi, 0, 3)
        r.addWidget(QLabel("IFbw @ low P"), 1, 0); r.addWidget(self.sp_bw_lo, 1, 1)
        r.addWidget(QLabel("IFbw @ high P"), 1, 2); r.addWidget(self.sp_bw_hi, 1, 3)
        self.btn_rule = QPushButton("Apply rule → table"); self.btn_rule.clicked.connect(self._apply_rule)
        r.addWidget(self.btn_rule, 2, 0, 1, 4)
        L.addWidget(rg)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Power (dBm)", "Averages", "IF bw (Hz)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(150)
        L.addWidget(self.table)

        L.addWidget(QLabel("Resonators to run"))
        self.res_list = QListWidget(); self.res_list.setMaximumHeight(90)
        L.addWidget(self.res_list)
        self.btn_loaddb = QPushButton("Load resonator(s) from DB…")
        self.btn_loaddb.clicked.connect(self._load_from_db)
        L.addWidget(self.btn_loaddb)

        brow = QHBoxLayout()
        self.btn_run = QPushButton("Run temperature sweep"); self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run); brow.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop"); self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self._stop); brow.addWidget(self.btn_stop)
        L.addLayout(brow)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(110); L.addWidget(self.log)
        split.addWidget(left)

        # right
        right = QWidget(); R = QVBoxLayout(right); R.setContentsMargins(10, 10, 10, 10)
        self.lbl_temp = QLabel("Idle."); self.lbl_temp.setStyleSheet(f"font-weight:bold; color:{theme.hx('accent')};")
        R.addWidget(self.lbl_temp)
        R.addWidget(QLabel("Qi vs power (one curve per resonator · temperature)"))
        self.qi_plot = pg.PlotWidget()
        self.qi_plot.setLabel("bottom", "PNA power", units="dBm")
        self.qi_plot.setLabel("left", "Qi"); self.qi_plot.setLogMode(x=False, y=True)
        self.qi_plot.showGrid(x=True, y=True, alpha=0.3); self.qi_plot.addLegend()
        R.addWidget(self.qi_plot, 1)
        split.addWidget(right); split.setStretchFactor(1, 1)

        self._mode_changed(); self._generate_table(); self._update_temps_label()

    # ------------------------------------------------------------------
    # helpers (shared with power window pattern)
    # ------------------------------------------------------------------

    def _is_hpd(self):
        return self.cmb_mode.currentIndex() == 1

    def _mode_changed(self):
        hpd = self._is_hpd(); self.lbl_reject.setVisible(hpd); self.sp_reject.setVisible(hpd)

    def _unit_scale(self):
        return 1e-3 if self.cmb_tunit.currentText() == "mK" else 1.0

    def _temps_k(self):
        a, b, s = self.sp_tstart.value(), self.sp_tstop.value(), self.sp_tstep.value()
        lo, hi = min(a, b), max(a, b)
        n = int(round((hi - lo) / s)) + 1
        vals = [round((lo + i * s) * self._unit_scale(), 9) for i in range(max(n, 1))]
        if self.chk_reverse.isChecked():
            vals = vals[::-1]
        return vals

    def _update_temps_label(self):
        ts = self._temps_k()
        self.lbl_temps.setText(f"{len(ts)} temps: " +
                               ", ".join(format_temp_label(x) for x in ts[:6]) +
                               (" …" if len(ts) > 6 else ""))

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
            av = int(np.clip(round(a_lo * (a_hi / a_lo) ** frac), 1, 15))
            bw = int(np.clip(round(b_lo * (b_hi / b_lo) ** frac), 1, 1000))
            self.table.setItem(i, 1, QTableWidgetItem(str(av)))
            self.table.setItem(i, 2, QTableWidgetItem(str(bw)))

    def _schedule(self):
        out = []
        for i in range(self.table.rowCount()):
            try:
                pw = float(self.table.item(i, 0).text())
                av = int(np.clip(int(float(self.table.item(i, 1).text())), 1, 15))
                bw = int(np.clip(int(float(self.table.item(i, 2).text())), 1, 1000))
                out.append((pw, av, bw))
            except Exception:
                continue
        return out

    def _checked_resonators(self):
        return [self._resonators[i] for i in range(self.res_list.count())
                if self.res_list.item(i).checkState() == Qt.Checked]

    # ------------------------------------------------------------------

    def _run(self):
        if not instrument_manager.pna_connected():
            self._log("✗ PNA not connected."); return
        if not instrument_manager.fridge_connected():
            self._log("✗ Fridge not connected — temperature control unavailable."); return
        if instrument_manager.busy:
            return
        resonators = self._checked_resonators()
        schedule = self._schedule()
        temps = self._temps_k()
        if not (resonators and schedule and temps):
            self._log("Need resonators, a power table, and temperatures."); return
        settings.remember("temperature", {
            "t_start": self.sp_tstart.value(), "t_stop": self.sp_tstop.value(),
            "t_step": self.sp_tstep.value(), "t_unit": self.cmb_tunit.currentText(),
            "reverse": self.chk_reverse.isChecked(),
            "stable_mean_mk": self.sp_mean.value(), "stable_std_mk": self.sp_std.value(),
            "window": self.sp_win.value(), "poll_s": self.sp_poll.value(),
            "timeout_min": self.sp_timeout.value(),
        })
        self.qi_plot.clear(); self._curves = {}
        params = {
            "tag": "TempPowerDep",
            "mode": "hpd" if self._is_hpd() else "spd",
            "schedule": schedule, "points": self.sp_points.value(),
            "qi_reject_factor": self.sp_reject.value(), "trace": "S21",
            "temperatures_k": temps,
            "stable_mean_k": self.sp_mean.value() / 1000.0,
            "stable_std_k": self.sp_std.value() / 1000.0,
            "window": self.sp_win.value(),
            "time_between_readings": self.sp_poll.value(),
            "timeout_s": (self.sp_timeout.value() * 60 if self.sp_timeout.value() > 0 else None),
        }
        instrument_manager.set_busy(True)
        self.btn_run.setEnabled(False); self.btn_stop.setEnabled(True)
        self._log(f"Starting temperature sweep: {len(temps)} temps × "
                  f"{len(self._schedule())} powers × {len(resonators)} resonators "
                  f"({'HPD' if self._is_hpd() else 'SPD'}).")
        self._worker = TemperatureWorker(instrument_manager, resonators, params)
        self._worker.progress.connect(self._log)
        self._worker.temperature_update.connect(self._on_temp)
        self._worker.point_measured.connect(self._on_point)
        self._worker.temp_finished.connect(
            lambda d: self._log(f"✓ Temperature {d['t_label']} complete."))
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.abort(); self._log("Stop requested…")

    def _color(self, idx):
        palette = ["#89b4fa", "#f38ba8", "#a6e3a1", "#f9e2af", "#cba6f7",
                   "#94e2d5", "#fab387", "#74c7ec", "#eba0ac", "#b4befe"]
        return palette[idx % len(palette)]

    def _on_temp(self, t_k):
        import math
        self.lbl_temp.setText("Waiting… measured T = "
                              + ("—" if math.isnan(t_k) else format_temp_label(t_k)))

    def _on_point(self, d):
        key = f"Res {d['num']} @ {d.get('t_label','')}"
        if key not in self._curves:
            idx = len(self._curves)
            c = self.qi_plot.plot([], [], pen=pg.mkPen(self._color(idx), width=2),
                                  symbol='o', symbolSize=6, symbolBrush=self._color(idx), name=key)
            eb = pg.ErrorBarItem(x=np.array([]), y=np.array([]),
                                 top=np.array([]), bottom=np.array([]),
                                 pen=pg.mkPen(self._color(idx), width=1), beam=0.0)
            self.qi_plot.addItem(eb)
            self._curves[key] = {"powers": [], "qis": [], "errs": [], "curve": c, "eb": eb}
        qi = d.get("Qi"); qe = d.get("Qi_err")
        if d.get("fit_ok") and qi and qi > 0:
            self._curves[key]["powers"].append(d["power_dbm"])
            self._curves[key]["qis"].append(qi)
            self._curves[key]["errs"].append(qe if (qe and np.isfinite(qe)) else 0.0)
            order = np.argsort(self._curves[key]["powers"])
            xs = np.array(self._curves[key]["powers"])[order]
            ys = np.array(self._curves[key]["qis"])[order]
            es = np.array(self._curves[key]["errs"])[order]
            self._curves[key]["curve"].setData(xs, ys)
            self._curves[key]["eb"].setData(x=xs, y=ys, top=es, bottom=es, beam=0.0)
        self.lbl_temp.setText(f"{d.get('t_label','')} · Res {d['num']} @ {d['power_dbm']:g} dBm "
                              + (f"Qi={format_q(qi)}±{format_q(qe)}" if d.get("fit_ok") else "(fit failed)"))

    def _on_finished(self, results):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self.lbl_temp.setText("Done.")
        self._log(f"✓ Temperature sweep complete ({len(results)} temperatures).")

    def _on_error(self, msg):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)
        self._log("✗ " + msg.splitlines()[-1]); logger.error(msg)

    def _on_busy(self, busy):
        self.btn_run.setEnabled(not busy and instrument_manager.pna_connected())
        for w in (self.cmb_mode, self.sp_tstart, self.sp_tstop, self.sp_tstep,
                  self.cmb_tunit, self.chk_reverse, self.sp_pstart, self.sp_pstop,
                  self.sp_pstep, self.sp_points, self.btn_gen, self.btn_rule, self.table):
            w.setEnabled(not busy)

    def _log(self, m):
        self.log.append(m); logger.info(m)
