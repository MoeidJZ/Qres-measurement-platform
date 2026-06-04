"""
windows/instrument_control_dock.py
==================================
Always-accessible fridge control panel.

* Implemented as a QDockWidget so it can live docked inside the platform window
  *or* be dragged out into its own floating window (and re-docked), per spec.
* Generic over the backend: it renders one row per ``fridge.channels()`` entry,
  grouped by ``Channel.group``. Read-only channels show a value; settable
  channels also get a setpoint field + Apply.
* Manual "Refresh" button reads all channels (no background polling).
* Read and write remain available at all times (including during measurements),
  because the operator legitimately needs to nudge the needle valve / heaters /
  pressure mid-run. (Measurement-launching buttons elsewhere are what grey out
  when busy — not these live knobs.)

If the dock is closed it can be reopened from the platform window's
"Instrument Control" button.
"""

from __future__ import annotations

import logging

from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QGridLayout,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QDoubleValidator

from core.instrument_manager import instrument_manager
from core.control_workers import ChannelReadWorker, ChannelWriteWorker
from core import theme

logger = logging.getLogger(__name__)


class InstrumentControlDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Instrument Control", parent)
        self.setObjectName("InstrumentControlDock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self._read_worker = None
        self._write_workers = {}
        self._value_labels = {}     # channel_id -> QLabel (current value)
        self._setpoint_edits = {}   # channel_id -> QLineEdit
        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self):
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(10, 10, 10, 10)

        fridge = instrument_manager.fridge
        header = QLabel(f"{(fridge.name if fridge else 'Fridge')} control")
        header.setStyleSheet(f"font-weight:bold; color:{theme.hx('accent')}; font-size:13px;")
        outer.addWidget(header)

        top = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setObjectName("primary")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        self.btn_float = QPushButton("Pop out ⤢")
        self.btn_float.clicked.connect(self._toggle_float)
        top.addWidget(self.btn_float)
        top.addStretch()
        outer.addLayout(top)

        self.lbl_status = QLabel("Press Refresh to read values.")
        self.lbl_status.setStyleSheet(f"color:{theme.hx('muted')};")
        outer.addWidget(self.lbl_status)

        # Scrollable channel area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._grid_host = QVBoxLayout(inner)
        self._grid_host.setSpacing(8)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self._populate_channels()
        self.setWidget(container)

    def _populate_channels(self):
        fridge = instrument_manager.fridge
        if fridge is None:
            self._grid_host.addWidget(QLabel("No fridge selected."))
            return

        # group channels
        groups = {}
        for ch in fridge.channels():
            groups.setdefault(ch.group, []).append(ch)

        for group_name, chans in groups.items():
            box = QFrame()
            box.setFrameShape(QFrame.StyledPanel)
            box.setStyleSheet(f"QFrame{{border:1px solid {theme.hx('border')}; border-radius:6px;}}")
            grid = QGridLayout(box)
            grid.setContentsMargins(10, 8, 10, 8)
            title = QLabel(group_name)
            title.setStyleSheet(f"font-weight:bold; color:{theme.hx('accent')}; border:none;")
            grid.addWidget(title, 0, 0, 1, 4)

            for i, ch in enumerate(chans, start=1):
                name = QLabel(f"{ch.label}")
                name.setStyleSheet("border:none;")
                grid.addWidget(name, i, 0)

                val = QLabel("—")
                val.setStyleSheet(f"border:none; color:{theme.hx('text')}; font-family:monospace;")
                val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                grid.addWidget(val, i, 1)
                self._value_labels[ch.id] = val

                unit = QLabel(ch.unit)
                unit.setStyleSheet(f"border:none; color:{theme.hx('muted')};")
                grid.addWidget(unit, i, 2)

                if ch.settable:
                    edit = QLineEdit()
                    edit.setPlaceholderText("setpoint")
                    edit.setValidator(QDoubleValidator())
                    edit.setMaximumWidth(110)
                    grid.addWidget(edit, i, 3)
                    self._setpoint_edits[ch.id] = edit
                    btn = QPushButton("Apply")
                    btn.setMaximumWidth(80)
                    btn.clicked.connect(lambda _=False, cid=ch.id: self._apply(cid))
                    grid.addWidget(btn, i, 4)

            self._grid_host.addWidget(box)
        self._grid_host.addStretch()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def refresh(self):
        fridge = instrument_manager.fridge
        if fridge is None or not fridge.is_connected():
            self.lbl_status.setText("Fridge not connected.")
            return
        self.btn_refresh.setEnabled(False)
        self.lbl_status.setText("Reading…")
        self._read_worker = ChannelReadWorker(fridge)
        self._read_worker.result.connect(self._on_read)
        self._read_worker.error.connect(self._on_read_error)
        self._read_worker.start()

    def _on_read(self, values: dict):
        import math
        for cid, v in values.items():
            lbl = self._value_labels.get(cid)
            if lbl is not None:
                lbl.setText("—" if (v is None or (isinstance(v, float) and math.isnan(v)))
                            else f"{v:.5g}")
        self.btn_refresh.setEnabled(True)
        self.lbl_status.setText("Updated.")

    def _on_read_error(self, tb: str):
        self.btn_refresh.setEnabled(True)
        self.lbl_status.setText("Read error (see log).")
        logger.error("Control dock read error:\n%s", tb)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _apply(self, channel_id: str):
        fridge = instrument_manager.fridge
        if fridge is None or not fridge.is_connected():
            self.lbl_status.setText("Fridge not connected.")
            return
        edit = self._setpoint_edits.get(channel_id)
        if edit is None or not edit.text().strip():
            self.lbl_status.setText("Enter a setpoint first.")
            return
        try:
            value = float(edit.text())
        except ValueError:
            self.lbl_status.setText("Invalid number.")
            return
        self.lbl_status.setText(f"Setting {channel_id}…")
        w = ChannelWriteWorker(fridge, channel_id, value)
        w.done.connect(self._on_write)
        self._write_workers[channel_id] = w   # keep a ref
        w.start()

    def _on_write(self, channel_id: str, ok: bool, message: str):
        if ok:
            lbl = self._value_labels.get(channel_id)
            if lbl is not None and message not in ("set", ""):
                lbl.setText(message)
            self.lbl_status.setText(f"✓ {channel_id} set.")
        else:
            self.lbl_status.setText(f"✗ {channel_id}: {message}")
        self._write_workers.pop(channel_id, None)

    # ------------------------------------------------------------------

    def _toggle_float(self):
        self.setFloating(not self.isFloating())
        self.btn_float.setText("Re-dock ⤡" if self.isFloating() else "Pop out ⤢")
