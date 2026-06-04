"""
windows/connection_window.py
============================
Step 2 of the flow. Connect the chosen fridge, optionally connect the PNA,
choose the QCoDeS database + sample name, then open the measurement platform.

Key behaviours implemented here:
* Proteox connect shows the modal confirm dialog while the driver waits for the
  DECS popup to be dismissed (cross-thread via the worker's request_confirm).
* On any connection failure the user can edit the address and retry; on a
  successful connect with a *changed* address they're asked whether to make it
  the permanent default.
* PNA is optional: the platform opens without it, and PNA-dependent features
  downstream are greyed until it is connected.
"""

from __future__ import annotations

import os
import logging

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QFileDialog, QTextEdit, QFrame, QMessageBox,
)
from PyQt5.QtCore import Qt

from core.instrument_manager import instrument_manager
from core.settings import settings
from core.connect_workers import FridgeConnectWorker, PNAConnectWorker
from windows.dialogs import AddressEditDialog, ProteoxConfirmDialog
from core import theme

logger = logging.getLogger(__name__)

FRIDGE_LABELS = {"proteox": "Oxford Proteox", "teslatron": "Oxford Teslatron",
                 "manual": "Manual / No Fridge"}


class ConnectionWindow(QMainWindow):
    def __init__(self, on_back=None):
        super().__init__()
        self.on_back = on_back
        self.kind = instrument_manager.fridge_kind
        self.setWindowTitle(f"Connect — {FRIDGE_LABELS.get(self.kind, self.kind)}")
        self.setMinimumSize(860, 680)

        self._fridge_worker = None
        self._pna_worker = None
        self._proteox_dialog = None
        self._platform = None
        # remembers whether an edited address differs from the stored default
        self._pna_address = settings.get("network.pna_address")

        self._build_ui()
        self._refresh_states()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(14)
        layout.setContentsMargins(22, 22, 22, 22)
        self.setCentralWidget(root)

        # header
        hdr = QHBoxLayout()
        back = QPushButton("← Back")
        back.setMaximumWidth(90)
        back.clicked.connect(self._go_back)
        hdr.addWidget(back)
        title = QLabel(f"Connect — {FRIDGE_LABELS.get(self.kind, self.kind)}")
        title.setStyleSheet(f"font-size:18px; font-weight:bold; color:{theme.hx('accent')};")
        hdr.addWidget(title)
        hdr.addStretch()
        layout.addLayout(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # ---- Fridge group ----------------------------------------------
        self.grp_fridge = QGroupBox(FRIDGE_LABELS.get(self.kind, "Fridge"))
        fl = QVBoxLayout(self.grp_fridge)
        row = QHBoxLayout()
        self.lbl_fridge_info = QLabel(self._fridge_hint())
        self.lbl_fridge_info.setWordWrap(True)
        row.addWidget(self.lbl_fridge_info)
        row.addStretch()
        self.btn_connect_fridge = QPushButton("Connect")
        self.btn_connect_fridge.setObjectName("primary")
        self.btn_connect_fridge.clicked.connect(self._connect_fridge)
        row.addWidget(self.btn_connect_fridge)
        fl.addLayout(row)
        self.lbl_fridge_status = QLabel("Status: not connected")
        self.lbl_fridge_status.setStyleSheet(f"color:{theme.hx('muted')};")
        fl.addWidget(self.lbl_fridge_status)
        layout.addWidget(self.grp_fridge)

        # ---- PNA group (optional) --------------------------------------
        self.grp_pna = QGroupBox("Keysight PNA (optional)")
        pl = QVBoxLayout(self.grp_pna)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Address:"))
        self.txt_pna_addr = QLineEdit(self._pna_address)
        prow.addWidget(self.txt_pna_addr)
        self.btn_connect_pna = QPushButton("Connect PNA")
        self.btn_connect_pna.clicked.connect(self._connect_pna)
        prow.addWidget(self.btn_connect_pna)
        pl.addLayout(prow)
        note = QLabel("The platform opens without the PNA; measurement features "
                      "stay greyed out until it is connected.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.hx('muted')}; font-size:11px;")
        pl.addWidget(note)
        self.lbl_pna_status = QLabel("Status: not connected")
        self.lbl_pna_status.setStyleSheet(f"color:{theme.hx('muted')};")
        pl.addWidget(self.lbl_pna_status)
        layout.addWidget(self.grp_pna)

        # ---- Database / sample -----------------------------------------
        grp_db = QGroupBox("Session Database & Sample")
        dl = QVBoxLayout(grp_db)
        drow = QHBoxLayout()
        drow.addWidget(QLabel("Database (.db):"))
        self.txt_db = QLineEdit(settings.get("app.last_db_path", ""))
        drow.addWidget(self.txt_db)
        btn_db = QPushButton("Browse…")
        btn_db.clicked.connect(self._browse_db)
        drow.addWidget(btn_db)
        dl.addLayout(drow)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Sample name:"))
        self.txt_sample = QLineEdit(settings.get("app.sample_name", ""))
        srow.addWidget(self.txt_sample)
        dl.addLayout(srow)
        layout.addWidget(grp_db)

        # ---- Continue ---------------------------------------------------
        crow = QHBoxLayout()
        crow.addStretch()
        self.btn_continue = QPushButton("Open Measurement Platform  →")
        self.btn_continue.setObjectName("success")
        self.btn_continue.clicked.connect(self._open_platform)
        crow.addWidget(self.btn_continue)
        layout.addLayout(crow)

        # ---- Log --------------------------------------------------------
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        layout.addWidget(self.log)

        self.statusBar().showMessage("Not connected")

    def _fridge_hint(self) -> str:
        if self.kind == "proteox":
            return ("Connects via the oiDECS / DECS-VISA driver. A confirmation "
                    "step lets you dismiss any DECS error popup before finishing.")
        if self.kind == "teslatron":
            return (f"Mercury iTC at {settings.get('network.teslatron_address')}. "
                    "If it can't connect you can edit the address and retry.")
        return ("Manual mode: no hardware fridge control. Click Connect to enable "
                "the platform; record temperatures by hand for labels.")

    # ------------------------------------------------------------------
    # Logging helper
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self.log.append(msg)
        logger.info(msg)

    # ------------------------------------------------------------------
    # Fridge connection
    # ------------------------------------------------------------------

    def _connect_fridge(self):
        self.btn_connect_fridge.setEnabled(False)
        self.lbl_fridge_status.setText("Status: connecting…")
        kwargs = {}
        if self.kind == "teslatron":
            kwargs["address"] = settings.get("network.teslatron_address")
        self._fridge_worker = FridgeConnectWorker(instrument_manager, kwargs)
        self._fridge_worker.log.connect(self._log)
        self._fridge_worker.request_confirm.connect(self._show_proteox_confirm)
        self._fridge_worker.done.connect(self._fridge_done)
        self._fridge_worker.start()

    def _show_proteox_confirm(self, prompt: str):
        # Modal; Confirm unblocks the worker thread.
        self._proteox_dialog = ProteoxConfirmDialog(prompt, self)
        self._proteox_dialog.exec_()
        if self._fridge_worker is not None:
            self._fridge_worker.confirm()

    def _fridge_done(self, ok: bool, message: str):
        if ok:
            self.lbl_fridge_status.setText("Status: ✓ connected")
            self.lbl_fridge_status.setStyleSheet(f"color:{theme.hx('success')};")
            self._log(f"✓ {FRIDGE_LABELS.get(self.kind)} connected.")
            self.btn_connect_fridge.setText("Reconnect")
            self.btn_connect_fridge.setEnabled(True)
        else:
            self.lbl_fridge_status.setText("Status: connection failed")
            self.lbl_fridge_status.setStyleSheet(f"color:{theme.hx('danger')};")
            self._log(f"✗ Fridge connection failed: {message.splitlines()[0]}")
            self.btn_connect_fridge.setEnabled(True)
            self._offer_fridge_address_edit()
        self._refresh_states()

    def _offer_fridge_address_edit(self):
        # Only Teslatron has a directly-editable VISA address here.
        if self.kind != "teslatron":
            QMessageBox.warning(self, "Connection failed",
                                "Could not connect. Check the DECS-VISA service "
                                "and the decs_visa.py path, then retry.")
            return
        current = settings.get("network.teslatron_address")
        dlg = AddressEditDialog("Teslatron address", current, self)
        if dlg.exec_() and dlg.result_value:
            new_addr, _ = dlg.result_value
            # session-only for now; ask to persist after a successful connect
            instrument_manager.update_address("teslatron", new_addr, make_permanent=False)
            self._pending_teslatron_addr = (new_addr, current != new_addr)
            self._connect_fridge()

    # ------------------------------------------------------------------
    # PNA connection
    # ------------------------------------------------------------------

    def _connect_pna(self):
        addr = self.txt_pna_addr.text().strip()
        self._pna_address = addr
        self.btn_connect_pna.setEnabled(False)
        self.lbl_pna_status.setText("Status: connecting…")
        self._pna_worker = PNAConnectWorker(instrument_manager, addr)
        self._pna_worker.log.connect(self._log)
        self._pna_worker.done.connect(self._pna_done)
        self._pna_worker.start()

    def _pna_done(self, ok: bool, message: str):
        self.btn_connect_pna.setEnabled(True)
        if ok:
            self.lbl_pna_status.setText("Status: ✓ connected")
            self.lbl_pna_status.setStyleSheet(f"color:{theme.hx('success')};")
            self._log("✓ PNA connected.")
            # Address changed vs stored default? Offer to persist.
            stored = settings.get("network.pna_address")
            if self._pna_address and self._pna_address != stored:
                self._ask_make_permanent("pna", self._pna_address)
            else:
                instrument_manager.update_address("pna", self._pna_address,
                                                  make_permanent=False)
        else:
            self.lbl_pna_status.setText("Status: failed")
            self.lbl_pna_status.setStyleSheet(f"color:{theme.hx('danger')};")
            self._log(f"✗ PNA connection failed: {message}")
        self._refresh_states()

    def _ask_make_permanent(self, which: str, address: str):
        resp = QMessageBox.question(
            self, "Save address?",
            f"Connected using a new address:\n\n{address}\n\n"
            "Make this the permanent default for next time?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        instrument_manager.update_address(which, address,
                                          make_permanent=(resp == QMessageBox.Yes))
        if resp == QMessageBox.Yes:
            self._log(f"Saved {which} address as default.")

    # ------------------------------------------------------------------
    # Database / continue
    # ------------------------------------------------------------------

    def _browse_db(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Select or create QCoDeS database",
            settings.get("app.last_db_path", ""), "QCoDeS DB (*.db)")
        if path:
            if not path.endswith(".db"):
                path += ".db"
            self.txt_db.setText(path)

    def _open_platform(self):
        db = self.txt_db.text().strip()
        sample = self.txt_sample.text().strip()
        if not db:
            QMessageBox.warning(self, "Database required",
                                "Please choose a database file before continuing.")
            return
        try:
            instrument_manager.init_database(db)
        except Exception as e:
            QMessageBox.critical(self, "Database error", str(e))
            return
        instrument_manager.set_sample_name(sample)
        try:
            instrument_manager.setup_station()
        except Exception as e:
            self._log(f"⚠ Station setup warning: {e}")

        from windows.main_window import MainPlatformWindow
        self._platform = MainPlatformWindow(on_back=self.show)
        self._platform.show()
        self.hide()

    def _go_back(self):
        if callable(self.on_back):
            self.on_back()
        self.close()

    # ------------------------------------------------------------------
    # Enable/disable based on connection state
    # ------------------------------------------------------------------

    def _refresh_states(self):
        connected = instrument_manager.fridge_connected()
        # Manual mode needs an explicit Connect too (to mark active).
        self.btn_continue.setEnabled(connected)
        if connected:
            self.statusBar().showMessage(
                f"{FRIDGE_LABELS.get(self.kind)} connected"
                + ("  ·  PNA connected" if instrument_manager.pna_connected()
                   else "  ·  PNA not connected"))
