"""
core/connect_workers.py
=======================
Background workers for connecting to instruments, so the GUI never freezes
during the (sometimes slow) connection handshake.

The Proteox case is special: the oiDECS driver blocks on ``input("> ")`` inside
its constructor so the operator can dismiss a DECS error popup before the driver
finishes registering parameters. There is no console in a GUI, so:

  1. ``FridgeConnectWorker`` runs the connect in a thread.
  2. It passes a ``confirm_callback`` into ``ProteoxBackend.connect``. When the
     driver calls ``input()``, that callback fires *on the worker thread*,
     emits ``request_confirm`` (picked up on the GUI thread to show a modal
     dialog), and then **blocks** on a ``threading.Event``.
  3. When the user clicks "Confirm" in the dialog, the GUI calls
     ``worker.confirm()`` which sets the event, unblocking the driver so it
     proceeds.

This keeps the blocking driver semantics intact while giving the user a clean
in-app confirmation step.
"""

from __future__ import annotations

import threading
import traceback

from PyQt5.QtCore import QThread, pyqtSignal


class FridgeConnectWorker(QThread):
    log = pyqtSignal(str)
    request_confirm = pyqtSignal(str)     # prompt text -> GUI shows a dialog
    done = pyqtSignal(bool, str)          # success, message

    def __init__(self, instrument_manager, connect_kwargs: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.connect_kwargs = dict(connect_kwargs or {})
        self._confirm_event = threading.Event()
        self._prompt = "Dismiss any DECS popup, then confirm."

    # -- called from the GUI thread when the user clicks Confirm -----------
    def confirm(self) -> None:
        self._confirm_event.set()

    # -- runs on the worker thread; blocks until confirm() ------------------
    def _confirm_callback(self) -> None:
        self.log.emit("Waiting for user to confirm DECS popup dismissal…")
        self._confirm_event.clear()
        self.request_confirm.emit(self._prompt)
        self._confirm_event.wait()        # blocks the driver's input() here
        self.log.emit("Confirmation received — finishing connection…")

    def run(self) -> None:
        try:
            kind = self.im.fridge_kind
            if kind == "proteox":
                self.connect_kwargs["confirm_callback"] = self._confirm_callback
            self.log.emit(f"Connecting to {kind or 'fridge'}…")
            self.im.connect_fridge(**self.connect_kwargs)
            self.done.emit(True, "Connected")
        except Exception as e:
            self.done.emit(False, f"{e}\n\n{traceback.format_exc()}")


class PNAConnectWorker(QThread):
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, instrument_manager, address: str, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.address = address

    def run(self) -> None:
        try:
            self.log.emit(f"Connecting to PNA at {self.address}…")
            self.im.connect_pna(self.address)
            self.done.emit(True, "Connected")
        except Exception as e:
            self.done.emit(False, str(e))
