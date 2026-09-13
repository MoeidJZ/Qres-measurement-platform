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
    QListWidget, QListWidgetItem, QComboBox, QDoubleSpinBox, QSpinBox, QSlider, QFileDialog,
    QInputDialog, QMessageBox, QSplitter, QGroupBox, QGridLayout, QShortcut,
    QApplication, QAbstractSpinBox, QLineEdit, QTextEdit, QCheckBox,
)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtCore import Qt, QEvent

from core.settings import settings
from core import analysis_io as aio
from core.fitting import (fit_notch, fit_notch_auto, s21_from_mag_phase,
                          add_photons, format_q, format_photons)
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
        self._keep_range = False   # once the user sets a range, keep it across powers
        self._last_slider = None    # last frequency slider the user adjusted (for arrow keys)
        self._is_wide = False       # wideband scan (>=10000 pts): show S21, no fit
        self._build_ui()
        self._last_slider = self.sl_lo
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
        prow = QHBoxLayout()
        self.btn_prev = QPushButton("◀ Prev"); self.btn_prev.clicked.connect(self._prev_power)
        self.cmb_power = QComboBox(); self.cmb_power.currentIndexChanged.connect(self._on_power)
        self.btn_next = QPushButton("Next ▶"); self.btn_next.clicked.connect(self._next_power)
        prow.addWidget(self.btn_prev); prow.addWidget(self.cmb_power, 1); prow.addWidget(self.btn_next)
        g.addLayout(prow, 0, 1)

        g.addWidget(QLabel("Fit start"), 1, 0)
        self.sl_lo = QSlider(Qt.Horizontal); self.sl_lo.setRange(0, 0)
        self.sl_lo.setFocusPolicy(Qt.NoFocus)
        self.sl_lo.valueChanged.connect(lambda *_: self._on_index_changed(self.sl_lo))
        g.addWidget(self.sl_lo, 1, 1)
        self.lbl_lo = QLabel("—"); self.lbl_lo.setStyleSheet("font-family:monospace; font-size:11px;")
        g.addWidget(self.lbl_lo, 2, 0, 1, 2)

        g.addWidget(QLabel("Fit stop"), 3, 0)
        self.sl_hi = QSlider(Qt.Horizontal); self.sl_hi.setRange(0, 0)
        self.sl_hi.setFocusPolicy(Qt.NoFocus)
        self.sl_hi.valueChanged.connect(lambda *_: self._on_index_changed(self.sl_hi))
        g.addWidget(self.sl_hi, 3, 1)
        self.lbl_hi = QLabel("—"); self.lbl_hi.setStyleSheet("font-family:monospace; font-size:11px;")
        g.addWidget(self.lbl_hi, 4, 0, 1, 2)

        g.addWidget(QLabel("Attenuation (dB)"), 5, 0)
        self.sp_atten = QDoubleSpinBox(); self.sp_atten.setRange(-200, 0); self.sp_atten.setDecimals(1)
        self.sp_atten.setValue(float(settings.get("analysis.attenuation_db", -70)))
        self.sp_atten.valueChanged.connect(lambda *_: self._refit_live())
        g.addWidget(self.sp_atten, 5, 1)
        g.addWidget(QLabel("Smoothing σ"), 6, 0)
        self.sp_sigma = QDoubleSpinBox(); self.sp_sigma.setRange(0, 20); self.sp_sigma.setDecimals(1)
        self.sp_sigma.valueChanged.connect(lambda *_: self._refit_live())
        g.addWidget(self.sp_sigma, 6, 1)
        self.btn_reset = QPushButton("Reset range (full)"); self.btn_reset.clicked.connect(self._reset_range)
        g.addWidget(self.btn_reset, 7, 0, 1, 2)
        L.addWidget(cg)

        eg = QGroupBox("Export"); e = QVBoxLayout(eg)
        self.lbl_export = QLabel("Open a database to set the export location.")
        self.lbl_export.setWordWrap(True)
        self.lbl_export.setStyleSheet("font-size:11px;")
        e.addWidget(self.lbl_export)
        # The fitting parameters (…_fitting_parameters.csv) are always written.
        # The per-power trace CSV (frequency · mag · phase · fitted magnitude) is
        # optional — tick this to also save the data + fit alongside the params.
        self.chk_export_trace = QCheckBox("Also save trace data + fit (per-power CSV)")
        self.chk_export_trace.setChecked(bool(settings.get("analysis.export_trace_data", True)))
        self.chk_export_trace.toggled.connect(
            lambda v: settings.set("analysis.export_trace_data", bool(v)))
        e.addWidget(self.chk_export_trace)
        self.btn_export = QPushButton("Save this fit  (Ctrl+W)"); self.btn_export.clicked.connect(self._primary_export)
        e.addWidget(self.btn_export)
        self.btn_export_all = QPushButton("Fit && export all powers")
        self.btn_export_all.clicked.connect(self._secondary_export)
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
        self.view = ResonatorFitView(stacked=True)
        self.view.set_region_movable(False)   # range is controlled by the sliders
        R.addWidget(self.view, 1)
        split.addWidget(right); split.setStretchFactor(1, 1)

        self.statusBar().showMessage("Ready")
        self._set_enabled(False)

    def _set_enabled(self, on):
        for w in (self.cmb_power, self.btn_prev, self.btn_next, self.sl_lo, self.sl_hi,
                  self.sp_atten, self.sp_sigma, self.btn_reset,
                  self.btn_export, self.btn_export_all, self.chk_export_trace):
            w.setEnabled(on)

    def _install_hotkeys(self):
        # Ctrl+W is a modifier combo, safe as a normal shortcut
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._export_current)
        # n / p / arrows are single keys that focus widgets (the run list, the
        # combo) would otherwise swallow for type-ahead, so catch them with an
        # application-level filter while this window is active.
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and self.isActiveWindow():
            fw = QApplication.focusWidget()
            typing = isinstance(fw, (QLineEdit, QTextEdit))
            in_spin = isinstance(fw, QAbstractSpinBox)
            k = event.key()
            if not typing:
                if k in (Qt.Key_N,):
                    self._next_power(); return True
                if k in (Qt.Key_P,):
                    self._prev_power(); return True
                if not in_spin and k == Qt.Key_Right:
                    self._nudge_slider(+1); return True
                if not in_spin and k == Qt.Key_Left:
                    self._nudge_slider(-1); return True
        return super().eventFilter(obj, event)

    def closeEvent(self, ev):
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        super().closeEvent(ev)

    def _nudge_slider(self, step):
        """Left/Right arrows move the frequency-index slider the user last touched."""
        sl = self._last_slider or self.sl_lo
        if sl is not None and sl.isEnabled():
            sl.setValue(sl.value() + step)

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
        # wideband scans (very dense sweeps) get data/image export and no fit
        try:
            first = aio.trace_at(self._loaded, 0)[0]
            self._is_wide = int(np.asarray(first).size) >= 10000
        except Exception:
            self._is_wide = False
        self._apply_mode()
        self.cmb_power.blockSignals(True); self.cmb_power.clear()
        for pw in self._loaded["powers"]:
            self.cmb_power.addItem("n/a" if pw != pw else f"{pw:g} dBm")
        self.cmb_power.blockSignals(False)
        self._keep_range = False        # fresh run → auto-pick a window for the first power
        self.cmb_power.setCurrentIndex(0)
        self._on_power(0)

    def _apply_mode(self):
        wide = self._is_wide
        self.btn_export.setText("Export data (CSV)" if wide else "Save this fit  (Ctrl+W)")
        self.btn_export_all.setText("Export image (PNG)" if wide else "Fit && export all powers")
        # fit-only controls are meaningless for a wideband scan
        for w in (self.sl_lo, self.sl_hi, self.sp_atten, self.sp_sigma, self.btn_reset,
                  self.chk_export_trace):
            w.setEnabled(not wide)
        self.view.set_region_visible(not wide)

    def _trace(self):
        i = max(self.cmb_power.currentIndex(), 0)
        ld = self._loaded
        f, mag, phase = aio.trace_at(ld, i)
        return ld["powers"][i], f, mag, phase

    def _on_power(self, _idx):
        if not self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        self.view.set_data(freq, mag, phase)
        n = int(freq.size)
        self.sl_lo.blockSignals(True); self.sl_hi.blockSignals(True)
        self.sl_lo.setRange(0, max(n - 2, 0))
        self.sl_hi.setRange(1, max(n - 1, 1))
        if self._is_wide:
            i0, i1 = 0, max(n - 1, 1)
        elif self._keep_range:
            # preserve the user's chosen window when stepping through powers
            i0 = min(self.sl_lo.value(), n - 2)
            i1 = min(max(self.sl_hi.value(), i0 + 1), n - 1)
        else:
            # first power of a run → auto-pick a sensible window so a fit shows
            auto = fit_notch_auto(freq, s21_from_mag_phase(mag, phase),
                                  gaussian_sigma=self.sp_sigma.value())
            i0, i1 = self._indices_from_crop(freq, auto.get("auto_crop"))
            self._keep_range = True
        self.sl_lo.setValue(i0); self.sl_hi.setValue(i1)
        self.sl_lo.blockSignals(False); self.sl_hi.blockSignals(False)
        if n >= 2:
            self.view.set_range_hz(freq[i0], freq[i1], emit=False)
            self._update_range_labels(freq, i0, i1)
        self.lbl_head.setText(f"{self._loaded['name']}  ·  "
                              + ("single trace" if pw != pw else f"{pw:g} dBm"))
        if self._is_wide:
            self.view.set_fit({})
            self.lbl_metrics.setText(f"Wideband scan · {n} points · S21 magnitude "
                                     "(no fit — use Export data / Export image).")
        else:
            self._refit_live()

    def _update_range_labels(self, freq, i0, i1):
        self.lbl_lo.setText(f"start  idx {i0} / {freq.size-1}   ({freq[i0]/1e9:.6f} GHz)")
        self.lbl_hi.setText(f"stop   idx {i1} / {freq.size-1}   ({freq[i1]/1e9:.6f} GHz)")

    def _indices_from_crop(self, freq, crop):
        n = int(freq.size)
        if not crop or n < 2:
            return 0, max(n - 1, 0)
        lo, hi = min(crop), max(crop)
        i0 = int(np.searchsorted(freq, lo, side="left"))
        i1 = int(np.searchsorted(freq, hi, side="right")) - 1
        i0 = max(0, min(i0, n - 2))
        i1 = max(i0 + 1, min(i1, n - 1))
        return i0, i1

    def _on_index_changed(self, which=None):
        if which is not None:
            self._last_slider = which
        if not self._loaded or self._is_wide:
            return
        pw, freq, mag, phase = self._trace()
        n = int(freq.size)
        if n < 2:
            return
        i0 = min(self.sl_lo.value(), n - 2)
        i1 = min(max(self.sl_hi.value(), i0 + 1), n - 1)
        # keep the two sliders from crossing
        if self.sl_hi.value() != i1:
            self.sl_hi.blockSignals(True); self.sl_hi.setValue(i1); self.sl_hi.blockSignals(False)
        if self.sl_lo.value() != i0:
            self.sl_lo.blockSignals(True); self.sl_lo.setValue(i0); self.sl_lo.blockSignals(False)
        self._keep_range = True
        self.view.set_range_hz(freq[i0], freq[i1], emit=False)
        self._update_range_labels(freq, i0, i1)
        self._refit_live()

    def _next_power(self):
        if self.cmb_power.count() > 1:
            self.cmb_power.setCurrentIndex((self.cmb_power.currentIndex() + 1) % self.cmb_power.count())

    def _prev_power(self):
        if self.cmb_power.count() > 1:
            self.cmb_power.setCurrentIndex((self.cmb_power.currentIndex() - 1) % self.cmb_power.count())

    def _reset_range(self):
        if not self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        n = int(freq.size)
        self.sl_lo.blockSignals(True); self.sl_hi.blockSignals(True)
        self.sl_lo.setValue(0); self.sl_hi.setValue(max(n - 1, 1))
        self.sl_lo.blockSignals(False); self.sl_hi.blockSignals(False)
        self._keep_range = True
        if n >= 2:
            self.view.set_range_hz(freq[0], freq[-1], emit=False)
            self._update_range_labels(freq, 0, n - 1)
        self._refit_live()

    # ------------------------------------------------------------------
    # Live fit
    # ------------------------------------------------------------------

    def _refit_live(self):
        if not self._loaded or self._is_wide:
            return
        pw, freq, mag, phase = self._trace()
        n = int(freq.size)
        if n >= 2:
            i0 = max(0, min(self.sl_lo.value(), n - 2))
            i1 = max(i0 + 1, min(self.sl_hi.value(), n - 1))
            crop = (float(freq[i0]), float(freq[i1]))
        else:
            crop = None
        fit = fit_notch(freq, s21_from_mag_phase(mag, phase),
                        crop_hz=crop, gaussian_sigma=self.sp_sigma.value())
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
            self.lbl_metrics.setText("fit failed — adjust the index range.  " + fit.get("error", ""))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _measurement_type(self):
        return "multi_power" if len(self._loaded.get("powers", [])) > 1 else "single_power"

    def _primary_export(self):
        if self._is_wide:
            self._export_wide_csv()
        else:
            self._export_current()

    def _secondary_export(self):
        if self._is_wide:
            self._export_wide_png()
        else:
            self._export_all_powers()

    def _export_wide_csv(self):
        if not self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        try:
            d = aio.export_wideband_csv(self.export_dir, self._loaded["name"], freq, mag, phase)
            self.statusBar().showMessage(f"Saved wideband data → {d}", 6000)
        except Exception as ex:
            QMessageBox.critical(self, "Export error", str(ex))

    def _export_wide_png(self):
        if not self._loaded:
            return
        import os
        path = os.path.join(self.export_dir, aio._safe_filename(self._loaded["name"] + "_S21.png"))
        try:
            self.view.export_magnitude_png(path)
            self.statusBar().showMessage(f"Saved S21 magnitude image → {path}", 6000)
        except Exception as ex:
            QMessageBox.critical(self, "Image export error", str(ex))

    def _export_current(self):
        if not self._loaded or "_fit" not in self._loaded:
            return
        pw, freq, mag, phase = self._trace()
        fit = self._loaded["_fit"]
        atten = self.sp_atten.value()
        i0, i1 = self.sl_lo.value(), self.sl_hi.value()
        pidx = max(self.cmb_power.currentIndex(), 0)
        chip_dbm = (pw + atten) if pw == pw else float("nan")
        save_trace = self.chk_export_trace.isChecked()
        try:
            if save_trace:
                aio.export_trace(self.export_dir, self.base_name, self._loaded["name"],
                                 pw, freq, mag, phase, fit)
            d = aio.append_metrics(self.export_dir, self.base_name,
                                   self._loaded.get("run_id"), self._loaded["name"],
                                   pidx, i0, i1, chip_dbm, fit, self._measurement_type())
            msg = (f"Saved fit parameters + trace data → {d}" if save_trace
                   else f"Saved fit parameters → {d}")
            self.statusBar().showMessage(msg, 6000)
        except Exception as ex:
            QMessageBox.critical(self, "Export error", str(ex))

    def _export_all_powers(self):
        if not self._loaded:
            return
        atten = self.sp_atten.value(); settings.set("analysis.attenuation_db", atten)
        i0v, i1v = self.sl_lo.value(), self.sl_hi.value()
        mtype = self._measurement_type()
        save_trace = self.chk_export_trace.isChecked()
        powers = self._loaded["powers"]
        n = 0
        # highest power first (matches the standard export ordering)
        for idx in sorted(range(len(powers)), key=lambda k: -powers[k]):
            pw, freq, mag, phase = (powers[idx], *aio.trace_at(self._loaded, idx))
            m = int(freq.size)
            if m >= 2:
                i0 = max(0, min(i0v, m - 2)); i1 = max(i0 + 1, min(i1v, m - 1))
                crop = (float(freq[i0]), float(freq[i1]))
            else:
                i0, i1, crop = 0, max(m - 1, 0), None
            fit = fit_notch(freq, s21_from_mag_phase(mag, phase), crop_hz=crop,
                            gaussian_sigma=self.sp_sigma.value())
            chip_dbm = (pw + atten) if pw == pw else float("nan")
            add_photons(fit, chip_dbm)
            try:
                if save_trace:
                    aio.export_trace(self.export_dir, self.base_name, self._loaded["name"],
                                     pw, freq, mag, phase, fit)
                aio.append_metrics(self.export_dir, self.base_name,
                                   self._loaded.get("run_id"), self._loaded["name"],
                                   idx, i0, i1, chip_dbm, fit, mtype)
                n += 1
            except Exception:
                logger.exception("export failed @ %s", pw)
        tail = " (params + trace data)" if save_trace else " (params only)"
        self.statusBar().showMessage(f"Exported {n} power row(s) → {self.base_name}.csv{tail}", 6000)
