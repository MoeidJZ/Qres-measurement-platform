"""
windows/pna_window.py
=====================
PNA parameter control. Lets the user view and set:
  averaging state + count, start/stop frequency, number of points
  (capped at the hardware max of 100001), IF bandwidth, RF power, RF output
  on/off, measurement trace, and continuous-trigger (auto-sweep) mode.

Rules:
* All values seed from the persistent settings ('pna' block) and, after Apply,
  are written back so they become the new defaults.
* "Read from PNA" syncs the fields from the live instrument.
* Apply / Read / Safe-state are **disabled while a measurement is running**
  (``instrument_manager.busy``) — parameters must not change mid-sweep.
* The whole window is greyed if the PNA is not connected.
"""

from __future__ import annotations

import logging

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QDoubleSpinBox, QSpinBox, QCheckBox, QComboBox, QPushButton, QGroupBox,
    QFrame,
)

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.control_workers import PNAReadWorker, PNAWriteWorker, PNASafeStateWorker
from core import theme

logger = logging.getLogger(__name__)

TRACES = ["S21", "S11", "S12", "S22"]
MAX_POINTS = 100001


class PNAWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PNA Parameters")
        self.setMinimumWidth(460)
        self._read_worker = None
        self._write_worker = None
        self._safe_worker = None
        self._build_ui()
        self._load_from_settings()
        # react to busy changes (lock during measurements)
        instrument_manager.on_busy_changed(self._on_busy)
        self._on_busy(instrument_manager.busy)

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.setCentralWidget(root)

        self.lbl_conn = QLabel()
        layout.addWidget(self.lbl_conn)

        grp = QGroupBox("Sweep")
        grid = QGridLayout(grp)
        r = 0

        self.sp_start = QDoubleSpinBox(); self.sp_start.setRange(0, 50); self.sp_start.setDecimals(6); self.sp_start.setSuffix(" GHz")
        self.sp_stop = QDoubleSpinBox(); self.sp_stop.setRange(0, 50); self.sp_stop.setDecimals(6); self.sp_stop.setSuffix(" GHz")
        grid.addWidget(QLabel("Start"), r, 0); grid.addWidget(self.sp_start, r, 1)
        grid.addWidget(QLabel("Stop"), r, 2); grid.addWidget(self.sp_stop, r, 3); r += 1

        self.sp_points = QSpinBox(); self.sp_points.setRange(1, MAX_POINTS)
        self.sp_ifbw = QSpinBox(); self.sp_ifbw.setRange(1, 1_000_000); self.sp_ifbw.setSuffix(" Hz")
        grid.addWidget(QLabel(f"Points (max {MAX_POINTS})"), r, 0); grid.addWidget(self.sp_points, r, 1)
        grid.addWidget(QLabel("IF bandwidth"), r, 2); grid.addWidget(self.sp_ifbw, r, 3); r += 1

        self.sp_power = QDoubleSpinBox(); self.sp_power.setRange(-90, 30); self.sp_power.setDecimals(2); self.sp_power.setSuffix(" dBm")
        self.cmb_trace = QComboBox(); self.cmb_trace.addItems(TRACES)
        grid.addWidget(QLabel("Power"), r, 0); grid.addWidget(self.sp_power, r, 1)
        grid.addWidget(QLabel("Trace"), r, 2); grid.addWidget(self.cmb_trace, r, 3); r += 1
        layout.addWidget(grp)

        grp2 = QGroupBox("Averaging & Trigger")
        g2 = QGridLayout(grp2)
        self.chk_avg = QCheckBox("Averaging enabled")
        self.sp_avg = QSpinBox(); self.sp_avg.setRange(1, 100000)
        g2.addWidget(self.chk_avg, 0, 0)
        g2.addWidget(QLabel("Averages"), 0, 1); g2.addWidget(self.sp_avg, 0, 2)
        self.chk_continuous = QCheckBox("Continuous trigger (auto-sweep)")
        self.chk_output = QCheckBox("RF output ON")
        g2.addWidget(self.chk_continuous, 1, 0)
        g2.addWidget(self.chk_output, 1, 1, 1, 2)
        layout.addWidget(grp2)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color:{theme.hx('muted')};")
        layout.addWidget(self.lbl_status)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine); layout.addWidget(sep)

        btns = QHBoxLayout()
        self.btn_read = QPushButton("Read from PNA")
        self.btn_read.clicked.connect(self._read)
        btns.addWidget(self.btn_read)
        self.btn_safe = QPushButton("Safe state (-80 dBm, RF off)")
        self.btn_safe.clicked.connect(self._safe_state)
        btns.addWidget(self.btn_safe)
        btns.addStretch()
        self.btn_apply = QPushButton("Apply to PNA")
        self.btn_apply.setObjectName("primary")
        self.btn_apply.clicked.connect(self._apply)
        btns.addWidget(self.btn_apply)
        layout.addLayout(btns)

    # ------------------------------------------------------------------

    def _load_from_settings(self):
        b = settings.block("pna")
        self.sp_start.setValue(float(b.get("start_ghz", 4.0)))
        self.sp_stop.setValue(float(b.get("stop_ghz", 7.0)))
        self.sp_points.setValue(int(b.get("points", MAX_POINTS)))
        self.sp_ifbw.setValue(int(b.get("if_bw", 1000)))
        self.sp_power.setValue(float(b.get("power_dbm", -30.0)))
        self.sp_avg.setValue(int(b.get("averages", 1)))
        self.chk_avg.setChecked(bool(b.get("avg_enabled", False)))
        self.chk_continuous.setChecked(bool(b.get("trigger_continuous", False)))
        idx = self.cmb_trace.findText(str(b.get("trace", "S21")))
        if idx >= 0:
            self.cmb_trace.setCurrentIndex(idx)

    def _collect(self) -> dict:
        return {
            "start_ghz": self.sp_start.value(),
            "stop_ghz": self.sp_stop.value(),
            "points": self.sp_points.value(),
            "if_bw": self.sp_ifbw.value(),
            "power_dbm": self.sp_power.value(),
            "averages": self.sp_avg.value(),
            "avg_enabled": self.chk_avg.isChecked(),
            "trigger_continuous": self.chk_continuous.isChecked(),
            "output": self.chk_output.isChecked(),
            "trace": self.cmb_trace.currentText(),
        }

    # ------------------------------------------------------------------

    def _apply(self):
        if not instrument_manager.pna_connected():
            self.lbl_status.setText("PNA not connected.")
            return
        params = self._collect()
        # Persist as new defaults immediately.
        settings.remember("pna", params)
        self.btn_apply.setEnabled(False)
        self.lbl_status.setText("Applying…")
        self._write_worker = PNAWriteWorker(instrument_manager.pna, params)
        self._write_worker.done.connect(self._on_apply_done)
        self._write_worker.start()

    def _on_apply_done(self, ok: bool, msg: str):
        self.btn_apply.setEnabled(not instrument_manager.busy)
        self.lbl_status.setText("✓ Applied and saved as defaults." if ok else f"✗ {msg}")

    def _read(self):
        if not instrument_manager.pna_connected():
            self.lbl_status.setText("PNA not connected.")
            return
        self.btn_read.setEnabled(False)
        self.lbl_status.setText("Reading from PNA…")
        self._read_worker = PNAReadWorker(instrument_manager.pna)
        self._read_worker.result.connect(self._on_read)
        self._read_worker.error.connect(lambda tb: self._on_read({}))
        self._read_worker.start()

    def _on_read(self, vals: dict):
        if "start_ghz" in vals: self.sp_start.setValue(float(vals["start_ghz"]))
        if "stop_ghz" in vals: self.sp_stop.setValue(float(vals["stop_ghz"]))
        if "points" in vals: self.sp_points.setValue(int(vals["points"]))
        if "if_bw" in vals: self.sp_ifbw.setValue(int(vals["if_bw"]))
        if "power_dbm" in vals: self.sp_power.setValue(float(vals["power_dbm"]))
        if "averages" in vals: self.sp_avg.setValue(int(vals["averages"]))
        if "avg_enabled" in vals: self.chk_avg.setChecked(bool(vals["avg_enabled"]))
        if "output" in vals: self.chk_output.setChecked(bool(vals["output"]))
        self.btn_read.setEnabled(not instrument_manager.busy)
        self.lbl_status.setText("Synced from PNA." if vals else "Read failed (see log).")

    def _safe_state(self):
        if not instrument_manager.pna_connected():
            return
        self.btn_safe.setEnabled(False)
        self._safe_worker = PNASafeStateWorker(instrument_manager.pna)
        self._safe_worker.done.connect(
            lambda ok, m: (self.lbl_status.setText(m),
                           self.btn_safe.setEnabled(not instrument_manager.busy)))
        self._safe_worker.start()

    # ------------------------------------------------------------------

    def _on_busy(self, busy: bool):
        connected = instrument_manager.pna_connected()
        editable = connected and not busy
        for w in (self.btn_apply, self.btn_read, self.btn_safe):
            w.setEnabled(editable)
        if not connected:
            self.lbl_conn.setText("⚠ PNA not connected — connect it on the connection screen.")
            self.lbl_conn.setStyleSheet(f"color:{theme.hx('danger')};")
        elif busy:
            self.lbl_conn.setText("🔒 Measurement running — parameters locked.")
            self.lbl_conn.setStyleSheet(f"color:{theme.hx('warn')};")
        else:
            self.lbl_conn.setText("✓ PNA connected.")
            self.lbl_conn.setStyleSheet(f"color:{theme.hx('success')};")
