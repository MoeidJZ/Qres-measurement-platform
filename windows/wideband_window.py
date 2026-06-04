"""
windows/wideband_window.py
==========================
Step 1 of the measurement workflow: the wideband scan.

* Parameters seed from saved defaults and are persisted on Run (so they become
  the new defaults).
* Optional reference-temperature wait: pick which reference temperature gates
  the run, the target, and rely on the +100% band. "Run now" starts immediately
  (skipping/short-circuiting the wait); "Stop" hands manual control back at any
  time.
* The result renders in an embedded pyqtgraph plot: hover reads the exact
  frequency, click toggles resonance picks. "Confirm picks" hands the chosen
  frequencies to the span picker (Phase 5).
"""

from __future__ import annotations

import logging

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QDoubleSpinBox, QSpinBox, QCheckBox, QComboBox, QPushButton,
    QTextEdit, QSplitter,
)
from PyQt5.QtCore import Qt, pyqtSignal

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.measure_workers import WidebandWorker, format_temp_label
from windows.widgets.spectrum_plot import SpectrumPlot
from core import theme

logger = logging.getLogger(__name__)
MAX_POINTS = 100001


class WidebandWindow(QMainWindow):
    # emitted on Confirm picks: list of frequencies in Hz, plus result dict
    picksConfirmed = pyqtSignal(list, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("1 · Wideband Scan")
        self.setMinimumSize(1040, 700)
        self._worker = None
        self._last_result = {}
        self._build_ui()
        self._load_defaults()
        instrument_manager.on_busy_changed(self._on_busy)
        self._on_busy(instrument_manager.busy)

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        self.setCentralWidget(root)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split, 1)

        # ---- left: controls -------------------------------------------
        left = QWidget()
        L = QVBoxLayout(left)
        L.setContentsMargins(12, 12, 12, 12)

        grp = QGroupBox("Sweep parameters")
        g = QGridLayout(grp)
        self.sp_start = QDoubleSpinBox(); self.sp_start.setRange(0, 50); self.sp_start.setDecimals(6); self.sp_start.setSuffix(" GHz")
        self.sp_stop = QDoubleSpinBox(); self.sp_stop.setRange(0, 50); self.sp_stop.setDecimals(6); self.sp_stop.setSuffix(" GHz")
        self.sp_points = QSpinBox(); self.sp_points.setRange(2, MAX_POINTS)
        self.sp_ifbw = QSpinBox(); self.sp_ifbw.setRange(1, 1_000_000); self.sp_ifbw.setSuffix(" Hz")
        self.sp_power = QDoubleSpinBox(); self.sp_power.setRange(-90, 30); self.sp_power.setDecimals(2); self.sp_power.setSuffix(" dBm")
        self.chk_avg = QCheckBox("Averaging")
        self.sp_avg = QSpinBox(); self.sp_avg.setRange(1, 100000)
        g.addWidget(QLabel("Start"), 0, 0); g.addWidget(self.sp_start, 0, 1)
        g.addWidget(QLabel("Stop"), 1, 0); g.addWidget(self.sp_stop, 1, 1)
        g.addWidget(QLabel(f"Points (≤{MAX_POINTS})"), 2, 0); g.addWidget(self.sp_points, 2, 1)
        g.addWidget(QLabel("IF bandwidth"), 3, 0); g.addWidget(self.sp_ifbw, 3, 1)
        g.addWidget(QLabel("Power"), 4, 0); g.addWidget(self.sp_power, 4, 1)
        g.addWidget(self.chk_avg, 5, 0); g.addWidget(self.sp_avg, 5, 1)
        self.lbl_step = QLabel("")
        self.lbl_step.setStyleSheet(f"color:{theme.hx('muted')};")
        g.addWidget(self.lbl_step, 6, 0, 1, 2)
        L.addWidget(grp)
        for w in (self.sp_start, self.sp_stop, self.sp_points):
            w.valueChanged.connect(self._update_step_label)

        # ---- ref-temp wait --------------------------------------------
        rgrp = QGroupBox("Reference-temperature wait (optional)")
        rg = QGridLayout(rgrp)
        self.chk_wait = QCheckBox("Wait until reference temperature is in band")
        rg.addWidget(self.chk_wait, 0, 0, 1, 3)
        self.cmb_ref = QComboBox()
        self.cmb_ref.addItems(instrument_manager.reference_options() or ["(no fridge)"])
        rg.addWidget(QLabel("Reference"), 1, 0); rg.addWidget(self.cmb_ref, 1, 1, 1, 2)
        self.sp_target = QDoubleSpinBox(); self.sp_target.setRange(0, 400); self.sp_target.setDecimals(3)
        self.cmb_unit = QComboBox(); self.cmb_unit.addItems(["mK", "K"])
        rg.addWidget(QLabel("Target"), 2, 0); rg.addWidget(self.sp_target, 2, 1); rg.addWidget(self.cmb_unit, 2, 2)
        self.sp_poll = QSpinBox(); self.sp_poll.setRange(1, 1440); self.sp_poll.setSuffix(" min")
        rg.addWidget(QLabel("Check every"), 3, 0); rg.addWidget(self.sp_poll, 3, 1)
        note = QLabel("Fires when measured ≤ 2× target (+100% band). "
                      "You can Run now or Stop at any time.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{theme.hx('muted')}; font-size:11px;")
        rg.addWidget(note, 4, 0, 1, 3)
        L.addWidget(rgrp)

        # ---- buttons ---------------------------------------------------
        brow = QHBoxLayout()
        self.btn_run = QPushButton("Run sweep")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(lambda: self._start(wait=False))
        brow.addWidget(self.btn_run)
        self.btn_wait = QPushButton("Wait && run")
        self.btn_wait.clicked.connect(lambda: self._start(wait=True))
        brow.addWidget(self.btn_wait)
        L.addLayout(brow)
        brow2 = QHBoxLayout()
        self.btn_now = QPushButton("Run now")
        self.btn_now.clicked.connect(self._run_now)
        self.btn_now.setEnabled(False)
        brow2.addWidget(self.btn_now)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        brow2.addWidget(self.btn_stop)
        L.addLayout(brow2)

        self.btn_load = QPushButton("Load from database…  (use a saved wideband scan)")
        self.btn_load.clicked.connect(self._load_db)
        L.addWidget(self.btn_load)

        self.lbl_status = QLabel("Idle.")
        self.lbl_status.setStyleSheet(f"color:{theme.hx('subtext')};")
        L.addWidget(self.lbl_status)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(140)
        L.addWidget(self.log)
        L.addStretch()
        split.addWidget(left)

        # ---- right: plot + pick confirm -------------------------------
        right = QWidget()
        R = QVBoxLayout(right)
        R.setContentsMargins(12, 12, 12, 12)
        self.plot = SpectrumPlot(allow_picking=True)
        self.plot.picksChanged.connect(self._on_picks_changed)
        R.addWidget(self.plot, 1)
        crow = QHBoxLayout()
        self.lbl_picks = QLabel("Picks: 0")
        crow.addWidget(self.lbl_picks)
        crow.addStretch()
        self.btn_clear = QPushButton("Clear picks")
        self.btn_clear.clicked.connect(self.plot.clear_picks)
        crow.addWidget(self.btn_clear)
        self.btn_confirm = QPushButton("Confirm picks  →  Span picker")
        self.btn_confirm.setObjectName("success")
        self.btn_confirm.setEnabled(False)
        self.btn_confirm.clicked.connect(self._confirm)
        crow.addWidget(self.btn_confirm)
        R.addLayout(crow)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        self.statusBar().showMessage("Idle")

    # ------------------------------------------------------------------

    def _load_defaults(self):
        b = settings.block("pna")
        self.sp_start.setValue(float(b.get("start_ghz", 4.0)))
        self.sp_stop.setValue(float(b.get("stop_ghz", 7.0)))
        self.sp_points.setValue(int(b.get("points", MAX_POINTS)))
        self.sp_ifbw.setValue(int(b.get("if_bw", 1000)))
        self.sp_power.setValue(float(b.get("power_dbm", -30.0)))
        self.chk_avg.setChecked(bool(b.get("avg_enabled", False)))
        self.sp_avg.setValue(int(b.get("averages", 1)))

        w = settings.block("wideband")
        self.chk_wait.setChecked(bool(w.get("wait_for_ref_temp", False)))
        self.sp_poll.setValue(int(w.get("poll_interval_s", 3600)) // 60)
        # default target/unit from fridge base temperature
        base = instrument_manager.fridge.base_temperature_k if instrument_manager.fridge else 0.015
        target_k = float(w.get("ref_temp_target_k", base or 0.015))
        if target_k < 1.0:
            self.cmb_unit.setCurrentText("mK"); self.sp_target.setValue(target_k * 1000)
        else:
            self.cmb_unit.setCurrentText("K"); self.sp_target.setValue(target_k)
        ref_name = w.get("ref_temp_name", "")
        i = self.cmb_ref.findText(ref_name)
        if i >= 0:
            self.cmb_ref.setCurrentIndex(i)
        self._update_step_label()

    def _update_step_label(self):
        try:
            span = (self.sp_stop.value() - self.sp_start.value()) * 1e9
            n = self.sp_points.value()
            if n > 1 and span > 0:
                self.lbl_step.setText(f"Step ≈ {span/(n-1):,.1f} Hz")
        except Exception:
            pass

    def _target_k(self) -> float:
        v = self.sp_target.value()
        return v / 1000.0 if self.cmb_unit.currentText() == "mK" else v

    def _collect_params(self) -> dict:
        return {
            "start_hz": self.sp_start.value() * 1e9,
            "stop_hz": self.sp_stop.value() * 1e9,
            "points": self.sp_points.value(),
            "if_bw": self.sp_ifbw.value(),
            "power_dbm": self.sp_power.value(),
            "avg_enabled": self.chk_avg.isChecked(),
            "averages": self.sp_avg.value(),
            "trace": "S21",
            "sample_name": instrument_manager.sample_name,
            "inline_attenuation_db": settings.get("pna.inline_attenuation_db", 80),
            "ref_wait": self.chk_wait.isChecked(),
            "ref_label": self.cmb_ref.currentText(),
            "ref_target_k": self._target_k(),
            "ref_margin": 1.0,
            "poll_interval_s": self.sp_poll.value() * 60,
        }

    def _persist(self, p):
        settings.remember("pna", {
            "start_ghz": self.sp_start.value(), "stop_ghz": self.sp_stop.value(),
            "points": self.sp_points.value(), "if_bw": self.sp_ifbw.value(),
            "power_dbm": self.sp_power.value(),
            "avg_enabled": self.chk_avg.isChecked(), "averages": self.sp_avg.value(),
        })
        settings.remember("wideband", {
            "wait_for_ref_temp": self.chk_wait.isChecked(),
            "ref_temp_name": self.cmb_ref.currentText(),
            "ref_temp_target_k": self._target_k(),
            "poll_interval_s": self.sp_poll.value() * 60,
        })

    # ------------------------------------------------------------------

    def _start(self, wait: bool):
        if not instrument_manager.pna_connected():
            self._log("✗ PNA not connected.")
            return
        if instrument_manager.busy:
            self._log("Another measurement is running.")
            return
        p = self._collect_params()
        p["ref_wait"] = wait   # 'Wait & run' -> True, 'Run sweep' -> False
        self._persist(p)
        self._log("Starting wideband sweep…")
        instrument_manager.set_busy(True)
        self.btn_run.setEnabled(False); self.btn_wait.setEnabled(False)
        self.btn_now.setEnabled(wait); self.btn_stop.setEnabled(True)
        self._worker = WidebandWorker(instrument_manager, p)
        self._worker.progress.connect(self._log)
        self._worker.temperature_update.connect(self._on_temp)
        self._worker.countdown.connect(self._on_countdown)
        self._worker.sweep_data.connect(self._on_data)
        self._worker.finished.connect(self._on_finished)
        self._worker.aborted.connect(self._on_aborted)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _run_now(self):
        if self._worker:
            self._worker.run_now()
            self.btn_now.setEnabled(False)

    def _stop(self):
        if self._worker:
            self._worker.abort()
            self.btn_stop.setEnabled(False)
            self._log("Stopping… (aborting the PNA sweep)")

    def _on_aborted(self):
        self._log("■ Stopped. Manual control restored.")
        self._reset_buttons()

    # ------------------------------------------------------------------

    def _on_temp(self, t_k: float):
        import math
        txt = "unreadable" if math.isnan(t_k) else format_temp_label(t_k)
        self.lbl_status.setText(f"Reference: {txt}")

    def _on_countdown(self, secs: int):
        m, s = divmod(secs, 60)
        self.statusBar().showMessage(f"Next reference check in {m:d}m {s:02d}s")

    def _on_data(self, freq_ghz, mag_db):
        self.plot.set_data(freq_ghz, mag_db)

    def _on_finished(self, result: dict):
        self._last_result = result
        self.plot.set_data(result["freq_ghz"], result["mag_db"])
        self.plot.set_title(result.get("meas_name", ""))
        self._log(f"✓ Done. Run ID {result.get('run_id')}. "
                  "Click the trace to pick resonances.")
        self._reset_buttons()

    def _load_db(self):
        if instrument_manager.busy:
            return
        from windows.dialogs import QualityRunPicker
        from core import analysis_io as aio
        dlg = QualityRunPicker(self, single=True, name_filter="_Wide_",
                               title="Load a wideband scan from database")
        if not (dlg.exec_() and dlg.result_value):
            return
        db_path, run_ids = dlg.result_value
        rid = run_ids[0]
        try:
            ld = aio.load_run(db_path, rid)
        except Exception as e:
            self._log(f"✗ Could not load run {rid}: {e}"); return
        freq = ld["freq"]; mag = ld["mag"][0]
        self._last_result = {
            "freq_ghz": freq / 1e9, "mag_db": mag, "run_id": rid,
            "meas_name": ld["name"], "temp_k": float("nan"),
            "sample_name": (ld["name"].split("_")[0] if ld.get("name") else ""),
        }
        self.plot.set_data(freq / 1e9, mag)
        self.plot.set_title(ld["name"])
        self._log(f"Loaded wideband run #{rid} ({ld['name']}). "
                  "Click the trace to pick resonances, then Confirm → Span picker.")

    def _on_error(self, tb: str):
        self._log("✗ Error:\n" + tb.splitlines()[-1])
        logger.error(tb)
        self._reset_buttons()

    def _reset_buttons(self):
        instrument_manager.set_busy(False)
        self.btn_run.setEnabled(True); self.btn_wait.setEnabled(True)
        self.btn_now.setEnabled(False); self.btn_stop.setEnabled(False)

    # ------------------------------------------------------------------

    def _on_picks_changed(self, freqs_hz):
        self.lbl_picks.setText(f"Picks: {len(freqs_hz)}")
        self.btn_confirm.setEnabled(len(freqs_hz) > 0 and not instrument_manager.busy)

    def _confirm(self):
        freqs = self.plot.picked_frequencies_hz()
        if not freqs:
            return
        # remember for the next phase
        settings.set("resonator_picks_hz", freqs)
        self.picksConfirmed.emit(freqs, self._last_result)
        self._log(f"Confirmed {len(freqs)} resonance(s). → Span picker (Phase 5).")

    def _on_busy(self, busy: bool):
        # While a run is in progress, lock the parameter form but keep Stop/Run-now.
        for w in (self.btn_run, self.btn_wait):
            w.setEnabled(not busy and instrument_manager.pna_connected())
        self.btn_load.setEnabled(not busy)
        self.btn_confirm.setEnabled(
            not busy and len(self.plot.picked_frequencies_hz()) > 0)

    def _log(self, msg: str):
        self.log.append(msg)
        logger.info(msg)
