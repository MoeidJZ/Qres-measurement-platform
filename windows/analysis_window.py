"""
windows/analysis_window.py
==========================
Analysis / fitting (Probst-style). Open any .db (the live one or another),
browse runs, and fit each trace with the notch-port circle fit shown across
three live plots (magnitude, phase, normalized circle). The fit updates in real
time as you drag the frequency range — no re-fit button.

Hotkeys:  n = next power   p = previous power   s = save (export current fit)
Fitted values shown include fr, Qi, Qc, Ql, phi and the photon number n_r
(from the VNA power minus the input attenuation you set).
"""

from __future__ import annotations

import logging
from typing import Dict

import numpy as np
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QComboBox, QDoubleSpinBox, QFileDialog,
    QInputDialog, QMessageBox, QSplitter, QGroupBox, QGridLayout, QShortcut,
)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtCore import Qt

from core.settings import settings
from core import analysis_io as aio
from core.fitting import fit_notch, s21_from_mag_phase, add_photons, format_q, format_photons
from windows.widgets.resonator_fit_view import ResonatorFitView

logger = logging.getLogger(__name__)


class AnalysisWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Analysis — Resonator Circle Fit")
        self.setMinimumSize(1180, 820)
        self.db_path = ""
        self.export_dir = ""
        self.base_name = ""
        self._loaded: Dict = {}
        self._runs = []
        self._build_ui()
        self._install_hotkeys()

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget(); outer = QVBoxLayout(root); self.setCentralWidget(root)

        top = QHBoxLayout()
        self.btn_open = QPushButton("Open .db…"); self.btn_open.setObjectName("primary")
        self.btn_open.clicked.connect(self._open_db); top.addWidget(self.btn_open)
        self.lbl_db = QLabel("No database open."); top.addWidget(self.lbl_db, 1)
        self.btn_refresh = QPushButton("Refresh runs"); self.btn_refresh.clicked.connect(self._refresh_runs)
        top.addWidget(self.btn_refresh)
        outer.addLayout(top)

        split = QSplitter(Qt.Horizontal); outer.addWidget(split, 1)

        # left
        left = QWidget(); L = QVBoxLayout(left); L.setContentsMargins(8, 8, 8, 8)
        L.addWidget(QLabel("Runs"))
        self.run_list = QListWidget(); self.run_list.currentRowChanged.connect(self._on_run)
        L.addWidget(self.run_list, 1)

        cg = QGroupBox("Trace & fit"); g = QGridLayout(cg)
        g.addWidget(QLabel("Power  (n / p)"), 0, 0)
        self.cmb_power = QComboBox(); self.cmb_power.currentIndexChanged.connect(self._on_power)
        g.addWidget(self.cmb_power, 0, 1)
        g.addWidget(QLabel("Attenuation (dB)"), 1, 0)
        self.sp_atten = QDoubleSpinBox(); self.sp_atten.setRange(-200, 0); self.sp_atten.setDecimals(1)
        self.sp_atten.setValue(float(settings.get("analysis.attenuation_db", -70)))
        self.sp_atten.valueChanged.connect(lambda *_: self._refit_live())
        g.addWidget(self.sp_atten, 1, 1)
        g.addWidget(QLabel("Smoothing σ"), 2, 0)
        self.sp_sigma = QDoubleSpinBox(); self.sp_sigma.setRange(0, 20); self.sp_sigma.setDecimals(1)
        self.sp_sigma.valueChanged.connect(lambda *_: self._refit_live())
        g.addWidget(self.sp_sigma, 2, 1)
        self.btn_reset = QPushButton("Reset range"); self.btn_reset.clicked.connect(self._reset_range)
        g.addWidget(self.btn_reset, 3, 0, 1, 2)
        L.addWidget(cg)

        eg = QGroupBox("Export"); e = QVBoxLayout(eg)
        self.lbl_export = QLabel("Open a database to set the export location.")
        self.lbl_export.setWordWrap(True)
        self.lbl_export.setStyleSheet("font-size:11px;")
        e.addWidget(self.lbl_export)
        self.btn_export = QPushButton("Save this fit  (s)"); self.btn_export.clicked.connect(self._export_current)
        e.addWidget(self.btn_export)
        self.btn_export_all = QPushButton("Fit && export all powers")
        self.btn_export_all.clicked.connect(self._export_all_powers)
        e.addWidget(self.btn_export_all)
        L.addWidget(eg)
        split.addWidget(left)

        # right
        right = QWidget(); R = QVBoxLayout(right); R.setContentsMargins(8, 8, 8, 8)
        self.lbl_head = QLabel("Open a database and select a run.")
        self.lbl_head.setStyleSheet("font-weight:bold;")
        R.addWidget(self.lbl_head)
        self.lbl_metrics = QLabel(""); self.lbl_metrics.setStyleSheet("font-family:monospace;")
        self.lbl_metrics.setWordWrap(True)
        R.addWidget(self.lbl_metrics)
        self.view = ResonatorFitView()
        self.view.rangeChanged.connect(lambda *_: self._refit_live())
        R.addWidget(self.view, 1)
        split.addWidget(right); split.setStretchFactor(1, 1)

        self.statusBar().showMessage("Ready")
        self._set_enabled(False)

    def _set_enabled(self, on):
        for w in (self.cmb_power, self.sp_atten, self.sp_sigma, self.btn_reset,
                  self.btn_export, self.btn_export_all):
            w.setEnabled(on)

    def _install_hotkeys(self):
        for key in ("n", "N", "Right"):
            QShortcut(QKeySequence(key), self, activated=self._next_power)
        for key in ("p", "P", "Left"):
            QShortcut(QKeySequence(key), self, activated=self._prev_power)
        for key in ("s", "S", "w", "W"):     # s (your preference) + w (Probst)
            QShortcut(QKeySequence(key), self, activated=self._export_current)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _open_db(self):
        start = settings.get("app.last_db_path", "")
        path, _ = QFileDialog.getOpenFileName(self, "Open QCoDeS database", start, "QCoDeS DB (*.db)")
        if not path:
            return
        export_dir = QFileDialog.getExistingDirectory(
            self, "Choose export folder for this database",
            settings.get("app.export_dir", "") or start)
        if not export_dir:
            QMessageBox.information(self, "Export needed", "An export folder is required.")
            return
        base, ok = QInputDialog.getText(self, "Export base name", "Base name for exported files:",
                                        text=settings.get("app.export_base_name", "") or "resonator")
        if not ok or not base.strip():
            return
        self.db_path, self.export_dir, self.base_name = path, export_dir, base.strip()
        settings.set("app.export_dir", export_dir)
        settings.set("app.export_base_name", self.base_name)
        self.lbl_db.setText(path)
        self.lbl_export.setText(f"Exports → {export_dir}\ndata: {self.base_name}_Res*_T_P.csv · "
                                f"metrics: {self.base_name}_fitting_parameters.csv")
        self._refresh_runs()

    def _refresh_runs(self):
        if not self.db_path:
            return
        self.run_list.clear()
        try:
            self._runs = aio.list_runs(self.db_path)
        except Exception as ex:
            QMessageBox.critical(self, "Read error", str(ex)); return
        for r in self._runs:
            self.run_list.addItem(QListWidgetItem(f"#{r['run_id']}  {r['name']}"))
        self.statusBar().showMessage(f"{len(self._runs)} runs")

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_run(self, row):
        if not (0 <= row < len(self._runs)):
            return
        rid = self._runs[row]["run_id"]
        try:
            self._loaded = aio.load_run(self.db_path, rid)
        except Exception as ex:
            self.lbl_head.setText(f"Run #{rid}: {ex}"); self._set_enabled(False); return
        self._set_enabled(True)
        self.cmb_power.blockSignals(True); self.cmb_power.clear()
        for pw in self._loaded["powers"]:
            self.cmb_power.addItem("n/a" if pw != pw else f"{pw:g} dBm")
        self.cmb_power.blockSignals(False)
        self.cmb_power.setCurrentIndex(0)
        self._on_power(0)

    def _trace(self):
        i = max(self.cmb_power.currentIndex(), 0)
        ld = self._loaded
        f, mag, phase = aio.trace_at(ld, i)
        return ld["powers"][i], f, mag, phase

    def _on_power(self, _idx):
        if not self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        self.view.set_data(freq, mag, phase)   # resets range -> triggers live fit
        self.lbl_head.setText(f"{self._loaded['name']}  ·  "
                              + ("single trace" if pw != pw else f"{pw:g} dBm"))

    def _next_power(self):
        if self.cmb_power.count() > 1:
            self.cmb_power.setCurrentIndex((self.cmb_power.currentIndex() + 1) % self.cmb_power.count())

    def _prev_power(self):
        if self.cmb_power.count() > 1:
            self.cmb_power.setCurrentIndex((self.cmb_power.currentIndex() - 1) % self.cmb_power.count())

    def _reset_range(self):
        self.view.reset_range()

    # ------------------------------------------------------------------
    # Live fit
    # ------------------------------------------------------------------

    def _refit_live(self):
        if not self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        fit = fit_notch(freq, s21_from_mag_phase(mag, phase),
                        crop_hz=self.view.get_range_hz(),
                        gaussian_sigma=self.sp_sigma.value())
        chip = (pw + self.sp_atten.value()) if pw == pw else float("nan")
        add_photons(fit, chip)
        self._loaded["_fit"] = fit
        self._loaded["_chip_dbm"] = chip
        self.view.set_fit(fit)
        if fit.get("ok"):
            self.lbl_metrics.setText(
                f"fr = {fit['fr']/1e9:.6f} GHz    "
                f"Qi = {format_q(fit['Qi'])} ± {format_q(fit.get('Qi_err'))}    "
                f"Qc = {format_q(fit['Qc'])} ± {format_q(fit.get('Qc_err'))}    "
                f"Ql = {format_q(fit['Ql'])}    φ = {fit['phi']:.3f}    "
                f"χ² = {fit.get('chi_square', 0):.2e}    "
                f"n̄ = {format_photons(fit.get('photons'))}"
                + ("" if pw != pw else f"  @ {chip:.1f} dBm chip"))
        else:
            self.lbl_metrics.setText("fit failed — adjust the range.  " + fit.get("error", ""))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_current(self):
        if not self._loaded or "_fit" not in self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        fit = self._loaded["_fit"]
        try:
            d = aio.export_trace(self.export_dir, self.base_name, self._loaded["name"],
                                 pw, freq, mag, phase, fit)
            aio.append_metrics(self.export_dir, self.base_name, self._loaded.get("run_id"),
                               self._loaded["name"], pw, fit)
            self.statusBar().showMessage(f"Saved {d}", 6000)
        except Exception as ex:
            QMessageBox.critical(self, "Export error", str(ex))

    def _export_all_powers(self):
        if not self._loaded:
            return
        crop = self.view.get_range_hz()
        atten = self.sp_atten.value(); settings.set("analysis.attenuation_db", atten)
        n = 0
        for pw, freq, mag, phase in aio.iter_traces(self._loaded):
            fit = fit_notch(freq, s21_from_mag_phase(mag, phase), crop_hz=crop,
                            gaussian_sigma=self.sp_sigma.value())
            add_photons(fit, (pw + atten) if pw == pw else float("nan"))
            try:
                aio.export_trace(self.export_dir, self.base_name, self._loaded["name"],
                                 pw, freq, mag, phase, fit)
                aio.append_metrics(self.export_dir, self.base_name, self._loaded.get("run_id"),
                                   self._loaded["name"], pw, fit)
                n += 1
            except Exception:
                logger.exception("export failed @ %s", pw)
        self.statusBar().showMessage(f"Exported {n} trace(s) + metrics.", 6000)
