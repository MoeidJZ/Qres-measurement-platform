"""
windows/dialogs.py
==================
Small reusable dialogs used across the connection flow.
"""

from __future__ import annotations

import numpy as np

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


class PowerScheduleDialog(QDialog):
    """
    Pop-out editor for the power schedule: an arbitrary, possibly non-linear list
    of (power dBm, averages, IF bandwidth Hz) rows. Add / remove / duplicate /
    sort rows, and save or load the whole plan as a JSON config that is shared
    between the power- and temperature-dependent steps. On OK, ``result_value``
    is the validated schedule (list of (power, averages, if_bw)).
    """

    def __init__(self, parent, schedule):
        super().__init__(parent)
        self.setWindowTitle("Modify power schedule")
        self.setMinimumSize(560, 480)
        self.result_value = None
        from PyQt5.QtWidgets import (QTableWidget, QTableWidgetItem, QFileDialog,
                                     QMessageBox, QAbstractItemView)
        self._QTableWidgetItem = QTableWidgetItem
        self._QFileDialog = QFileDialog
        self._QMessageBox = QMessageBox

        v = QVBoxLayout(self)
        lab = QLabel("Each row is one measurement point. Powers need not be evenly "
                     "spaced — add a special point (e.g. −60 dBm with its own averaging "
                     "and IF bandwidth) anywhere in the list.")
        lab.setWordWrap(True); lab.setStyleSheet(f"color:{theme.hx('subtext')};")
        v.addWidget(lab)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Power (dBm)", "Averages", "IF bw (Hz)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.addWidget(self.table, 1)
        for row in (schedule or []):
            self._append_row(*row)
        if self.table.rowCount() == 0:
            self._append_row(-30.0, 1, 1000)

        row1 = QHBoxLayout()
        for label, slot in (("Add row", self._add),
                            ("Duplicate", self._duplicate),
                            ("Remove selected", self._remove),
                            ("Sort by power", self._sort),
                            ("Clear", self._clear)):
            btn = QPushButton(label); btn.clicked.connect(slot); row1.addWidget(btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        b_load = QPushButton("Load config…"); b_load.clicked.connect(self._load); row2.addWidget(b_load)
        b_save = QPushButton("Save config…"); b_save.clicked.connect(self._save); row2.addWidget(b_save)
        row2.addStretch()
        v.addLayout(row2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ---- row helpers
    def _append_row(self, p=-30.0, a=1, b=1000):
        r = self.table.rowCount(); self.table.insertRow(r)
        for c, val in enumerate((f"{float(p):g}", str(int(round(float(a)))), str(int(round(float(b)))))):
            self.table.setItem(r, c, self._QTableWidgetItem(val))

    def _add(self):
        self._append_row()

    def _duplicate(self):
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r < 0:
            return self._append_row()
        vals = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(3)]
        self.table.insertRow(r + 1)
        for c, val in enumerate(vals):
            self.table.setItem(r + 1, c, self._QTableWidgetItem(val))

    def _remove(self):
        rows = sorted({ix.row() for ix in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for r in rows:
            self.table.removeRow(r)

    def _clear(self):
        self.table.setRowCount(0)

    def _sort(self):
        sched = self._read(validate=False)
        sched.sort(key=lambda t: t[0])
        self._fill(sched)

    def _fill(self, sched):
        self.table.setRowCount(0)
        for row in sched:
            self._append_row(*row)

    # ---- read / validate
    def _read(self, validate=True):
        out, bad = [], []
        for i in range(self.table.rowCount()):
            try:
                p = float(self.table.item(i, 0).text())
                a = int(np.clip(int(round(float(self.table.item(i, 1).text()))), 1, 100000))
                b = int(np.clip(int(round(float(self.table.item(i, 2).text()))), 1, 15000000))
                out.append((round(p, 3), a, b))
            except Exception:
                bad.append(i + 1)
        if validate and bad:
            raise ValueError(f"Rows {', '.join(map(str, bad))} have invalid numbers.")
        return out

    def _load(self):
        from core.schedule_io import load_schedule
        path, _ = self._QFileDialog.getOpenFileName(self, "Load power schedule",
                                                    "", "JSON config (*.json);;All files (*)")
        if not path:
            return
        try:
            self._fill(load_schedule(path))
        except Exception as e:
            self._QMessageBox.warning(self, "Load failed", str(e))

    def _save(self):
        from core.schedule_io import save_schedule
        try:
            sched = self._read(validate=True)
        except Exception as e:
            self._QMessageBox.warning(self, "Cannot save", str(e)); return
        path, _ = self._QFileDialog.getSaveFileName(self, "Save power schedule",
                                                    "power_schedule.json", "JSON config (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            save_schedule(path, sched)
        except Exception as e:
            self._QMessageBox.warning(self, "Save failed", str(e))

    def _accept(self):
        try:
            sched = self._read(validate=True)
        except Exception as e:
            self._QMessageBox.warning(self, "Invalid schedule", str(e)); return
        if not sched:
            self._QMessageBox.warning(self, "Invalid schedule", "The schedule is empty."); return
        self.result_value = sched
        self.accept()


class ResonatorSpanDialog(QDialog):
    """
    Per-resonator frequency-span editor. Each resonator's span is shown in kHz;
    editing it changes only the start/stop frequencies (the centre stays fixed).
    On OK, ``result_value`` maps resonator number -> span in Hz.
    """

    def __init__(self, parent, resonators):
        super().__init__(parent)
        self.setWindowTitle("Edit per-resonator span (kHz)")
        self.setMinimumSize(520, 420)
        self.result_value = None
        from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem, QMessageBox
        self._QMessageBox = QMessageBox
        self._res = resonators

        v = QVBoxLayout(self)
        lab = QLabel("Adjust the measurement span for each resonator. Only the start "
                     "and stop frequencies change — the centre frequency is preserved.")
        lab.setWordWrap(True); lab.setStyleSheet(f"color:{theme.hx('subtext')};")
        v.addWidget(lab)

        self.table = QTableWidget(len(resonators), 3)
        self.table.setHorizontalHeaderLabels(["Resonator", "Center (GHz)", "Span (kHz)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, r in enumerate(resonators):
            center = float(r.get("center_hz", 0.5 * (r["fstart_hz"] + r["fstop_hz"])))
            span_hz = abs(float(r["fstop_hz"]) - float(r["fstart_hz"]))
            it_num = QTableWidgetItem(f"Res {r.get('num')}"); it_num.setFlags(Qt.ItemIsEnabled)
            it_c = QTableWidgetItem(f"{center/1e9:.6f}"); it_c.setFlags(Qt.ItemIsEnabled)
            it_s = QTableWidgetItem(f"{span_hz/1e3:.3f}")
            self.table.setItem(i, 0, it_num)
            self.table.setItem(i, 1, it_c)
            self.table.setItem(i, 2, it_s)
        v.addWidget(self.table, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _accept(self):
        spans = {}
        bad = []
        for i, r in enumerate(self._res):
            try:
                span_khz = float(self.table.item(i, 2).text())
                if span_khz <= 0:
                    raise ValueError
                spans[r.get("num")] = span_khz * 1e3
            except Exception:
                bad.append(i + 1)
        if bad:
            self._QMessageBox.warning(self, "Invalid span",
                                      f"Rows {', '.join(map(str, bad))} have an invalid span.")
            return
        self.result_value = spans
        self.accept()
