"""
windows/dialogs.py
==================
Small reusable dialogs used across the connection flow.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox,
    QPushButton, QDialogButtonBox, QDoubleSpinBox,
)
from PyQt5.QtCore import Qt
from core import theme


class AddressEditDialog(QDialog):
    """
    Edit an instrument address after a failed connection, with the option to
    make the change permanent (persisted to settings) or session-only.

    Returns (new_address, make_permanent) via ``result_value`` after exec_().
    """

    def __init__(self, title: str, current_address: str, parent=None,
                 ask_permanent: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.result_value = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Connection failed. You can edit the address and try again."
        ))

        row = QHBoxLayout()
        row.addWidget(QLabel("Address:"))
        self.edit = QLineEdit(current_address)
        row.addWidget(self.edit)
        layout.addLayout(row)

        self.chk_permanent = QCheckBox(
            "Make this the permanent default (otherwise only for this session)"
        )
        self.chk_permanent.setVisible(ask_permanent)
        layout.addWidget(self.chk_permanent)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply && Retry")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        self.result_value = (self.edit.text().strip(), self.chk_permanent.isChecked())
        self.accept()


class ProteoxConfirmDialog(QDialog):
    """
    Modal dialog shown while the oiDECS driver waits at its ``input("> ")``.

    The operator dismisses any DECS error popup, then clicks Confirm. Confirm is
    the only way out (no close button) so the driver can't be left hanging.
    """

    def __init__(self, prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Proteox / DECS Connection")
        self.setModal(True)
        self.setMinimumWidth(480)
        # Prevent closing via the window 'X' — must press Confirm.
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        msg = QLabel(
            "<b>Proteox is connecting.</b><br><br>"
            "If a DECS error popup appeared (e.g. magnet disconnected), dismiss "
            "it now in the DECS application.<br><br>"
            "When the popup is cleared — or if none appeared — click "
            "<b>Confirm</b> to complete the connection."
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)
        if prompt:
            hint = QLabel(prompt)
            hint.setStyleSheet(f"color:{theme.hx('muted')};")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        btn = QPushButton("Confirm")
        btn.setObjectName("primary")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)


class QualityRunPicker(QDialog):
    """
    Pick a .db and select which Quality runs to load. Returns
    ``result_value = (db_path, [run_id, ...])`` after exec_(), or None if
    cancelled. Used to bypass live wideband/quality and to load single
    resonators into the power / temperature steps.
    """

    def __init__(self, parent=None, single: bool = False,
                 name_filter: str = "_Quality_",
                 title: str = "Load quality runs from database"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 460)
        self.result_value = None
        self._single = single
        self._name_filter = name_filter
        self._db_path = ""

        from PyQt5.QtWidgets import QListWidget, QListWidgetItem, QFileDialog
        self._QListWidgetItem = QListWidgetItem
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.lbl = QLabel("No database chosen.")
        btn_open = QPushButton("Choose .db…"); btn_open.clicked.connect(self._choose_db)
        top.addWidget(btn_open); top.addWidget(self.lbl, 1)
        v.addLayout(top)

        hint = ("Select one run." if single else
                "Tick the Quality runs to load. Each becomes a resonator you can "
                "fit and confirm.")
        lab = QLabel(hint); lab.setStyleSheet(f"color:{theme.hx('subtext')};")
        v.addWidget(lab)

        self.list = QListWidget(); v.addWidget(self.list, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._QFileDialog = QFileDialog

    def _choose_db(self):
        from core.settings import settings
        start = settings.get("app.last_db_path", "")
        path, _ = self._QFileDialog.getOpenFileName(
            self, "Open QCoDeS database", start, "QCoDeS DB (*.db)")
        if not path:
            return
        self._db_path = path
        settings.set("app.last_db_path", path)
        self.lbl.setText(path)
        self._populate()

    def _populate(self):
        from core import analysis_io as aio
        self.list.clear()
        try:
            runs = [r for r in aio.list_runs(self._db_path)
                    if self._name_filter in (r.get("name") or "")]
        except Exception as e:
            self.lbl.setText(f"Read error: {e}"); return
        if not runs:
            self.list.addItem(self._QListWidgetItem(
                f"(no runs matching '{self._name_filter}' in this database)"))
            return
        for r in runs:
            it = self._QListWidgetItem(f"#{r['run_id']}  {r['name']}")
            it.setData(Qt.UserRole, r["run_id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.list.addItem(it)

    def _accept(self):
        ids = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            rid = it.data(Qt.UserRole)
            if rid is not None and it.checkState() == Qt.Checked:
                ids.append(int(rid))
        if self._db_path and ids:
            if self._single:
                ids = ids[:1]
            self.result_value = (self._db_path, ids)
            self.accept()
        else:
            self.reject()


class ReSpanDialog(QDialog):
    """
    Re-choose the span for a single resonator (used by the quality page's
    Re-measure). Shows that resonator's most recent trace with a draggable
    region; the region's midpoint becomes the new centre. After OK,
    ``result_value`` = dict with center_hz / fstart_hz / fstop_hz / span_mhz.
    """

    def __init__(self, parent, resonator: dict):
        super().__init__(parent)
        self.setWindowTitle(f"Re-measure Res {resonator.get('num')} — choose new span")
        self.setMinimumSize(720, 480)
        self.result_value = None
        self._r = resonator
        from windows.widgets.span_plot import SpanPlot
        import numpy as np

        v = QVBoxLayout(self)
        lab = QLabel("Drag the shaded region around the resonance, or type a span and "
                     "press “Re-view”. The centre follows the region.")
        lab.setStyleSheet(f"color:{theme.hx('subtext')};"); lab.setWordWrap(True)
        v.addWidget(lab)

        srow = QHBoxLayout(); srow.addWidget(QLabel("Span"))
        self.sp_span = QDoubleSpinBox(); self.sp_span.setRange(0.01, 1000)
        self.sp_span.setDecimals(3); self.sp_span.setSuffix(" MHz")
        cur_span = float(resonator.get("span_mhz",
                         abs(resonator["fstop_hz"] - resonator["fstart_hz"]) / 1e6))
        self.sp_span.setValue(cur_span); srow.addWidget(self.sp_span)
        btn_review = QPushButton("Re-view"); btn_review.clicked.connect(self._review)
        srow.addWidget(btn_review); srow.addStretch()
        self.lbl_center = QLabel(""); srow.addWidget(self.lbl_center)
        v.addLayout(srow)

        self.plot = SpanPlot(); v.addWidget(self.plot, 1)
        self.plot.regionChanged.connect(self._on_region)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self._np = np
        self._center = float(resonator.get("center_hz",
                             0.5 * (resonator["fstart_hz"] + resonator["fstop_hz"])))
        self._f0 = float(resonator["fstart_hz"]); self._f1 = float(resonator["fstop_hz"])
        self._show()

    def _trace(self):
        f = self._r.get("f_hz"); m = self._r.get("mag_db")
        if f is None or m is None or not len(f):
            return self._np.array([]), self._np.array([])
        return self._np.asarray(f, float) / 1e9, self._np.asarray(m, float)

    def _show(self):
        fz, mz = self._trace()
        self.lbl_center.setText(f"center {self._center/1e9:.6f} GHz")
        if fz.size:
            self.plot.set_data(fz, mz, self._center, region_hz=(self._f0, self._f1))
        else:
            self.plot.set_data([], [], self._center)

    def _review(self):
        half = self.sp_span.value() / 2.0 * 1e6
        self._f0 = self._center - half
        self._f1 = self._center + half
        self._show()

    def _on_region(self, start_hz, stop_hz):
        self._f0, self._f1 = float(start_hz), float(stop_hz)
        self._center = 0.5 * (self._f0 + self._f1)
        self.sp_span.blockSignals(True)
        self.sp_span.setValue((self._f1 - self._f0) / 1e6)
        self.sp_span.blockSignals(False)
        self.lbl_center.setText(f"center {self._center/1e9:.6f} GHz")

    def _accept(self):
        f0, f1 = min(self._f0, self._f1), max(self._f0, self._f1)
        if f1 - f0 <= 0:
            self.reject(); return
        self.result_value = {
            "center_hz": 0.5 * (f0 + f1),
            "fstart_hz": f0, "fstop_hz": f1, "span_mhz": (f1 - f0) / 1e6,
        }
        self.accept()
