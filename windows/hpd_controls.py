"""
windows/hpd_controls.py
=======================
Shared HPD pieces for the power and temperature windows:

* ``HPDStartControls`` – "HPD from ≤ X dBm" + "check seed fits before the
  sweep (timeout N min)". Powers above X are measured with ordinary linear
  sweeps (high → low); HPD takes over at X and is seeded from the fit at the
  lowest linear power.
* ``update_sweep_column`` – the "Sweep" column of the power table
  (Linear / HPD per row). Clicking a cell in that column sets X to that row.
* ``confirm_hpd_run`` – the warning shown before a run starts.
* ``HPDSeedCheckDialog`` – shown once, right after Run: every resonator's
  linear fit at its seed power, with a "Use HPD" tick per resonator. After
  "Start sweep" the run continues unattended. No answer before the countdown
  ends → every resonator stays linear.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

SWEEP_COL = 3


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

class HPDStartControls(QWidget):
    """Row of controls: HPD start power, seed-check checkbox, timeout."""
    changed = pyqtSignal()

    def __init__(self, block: dict, parent=None):
        super().__init__(parent)
        g = QGridLayout(self); g.setContentsMargins(0, 0, 0, 0)
        self.sp_start = QDoubleSpinBox(); self.sp_start.setRange(-90, 30)
        self.sp_start.setDecimals(2); self.sp_start.setSuffix(" dBm")
        self.sp_start.setValue(float(block.get("hpd_start_dbm", -30)))
        self.sp_start.setToolTip(
            "Powers ABOVE this value are measured with ordinary linear sweeps "
            "(high → low), so Kerr-distorted high-power fits never feed the HPD.\n"
            "From this power down, HPD (segment sweep) is used, seeded from the "
            "lowest linear power's fit.\nTip: click a row in the 'Sweep' column to set it.")
        self.chk_confirm = QCheckBox("Check seed fits before the sweep")
        self.chk_confirm.setChecked(bool(block.get("hpd_confirm_seed", True)))
        self.chk_confirm.setToolTip(
            "Right after Run, every selected resonator is measured once (linear) at "
            "its seed power and all fits are shown in one window.\nTick which "
            "resonators may use HPD and start the sweep; it then runs unattended.\n"
            "If nobody answers within the timeout, every resonator stays LINEAR.")
        self.sp_timeout = QSpinBox(); self.sp_timeout.setRange(1, 240)
        self.sp_timeout.setSuffix(" min"); self.sp_timeout.setValue(int(block.get("hpd_confirm_min", 10)))
        self.sp_timeout.setToolTip("How long the seed-check window waits before the run "
                                   "continues with linear sweeps only.")
        g.addWidget(QLabel("HPD from ≤"), 0, 0); g.addWidget(self.sp_start, 0, 1)
        g.addWidget(self.chk_confirm, 0, 2); g.addWidget(QLabel("timeout"), 0, 3)
        g.addWidget(self.sp_timeout, 0, 4)
        self.sp_start.valueChanged.connect(lambda *_: self.changed.emit())
        self.chk_confirm.toggled.connect(lambda *_: self._sync())
        self._sync()

    def _sync(self):
        self.sp_timeout.setEnabled(self.chk_confirm.isChecked())

    def start_dbm(self) -> float:
        return float(self.sp_start.value())

    def set_start_dbm(self, v: float):
        self.sp_start.setValue(float(v))

    def params(self) -> dict:
        return {"hpd_start_dbm": self.start_dbm(),
                "confirm_seed": self.chk_confirm.isChecked(),
                "seed_confirm_timeout_s": 60.0 * self.sp_timeout.value()}

    def remember(self) -> dict:
        return {"hpd_start_dbm": self.start_dbm(),
                "hpd_confirm_seed": self.chk_confirm.isChecked(),
                "hpd_confirm_min": self.sp_timeout.value()}

    def setEnabled(self, on: bool):          # keep timeout tied to the checkbox
        super().setEnabled(on)
        if on:
            self._sync()


# ---------------------------------------------------------------------------
# Table column
# ---------------------------------------------------------------------------

def setup_sweep_column(table: QTableWidget):
    table.setColumnCount(4)
    table.setHorizontalHeaderLabels(["Power (dBm)", "Averages", "IF bw (Hz)", "Sweep"])


def update_sweep_column(table: QTableWidget, hpd_mode: bool, start_dbm: float):
    """Fill the read-only 'Sweep' column from the HPD-start power."""
    for i in range(table.rowCount()):
        it0 = table.item(i, 0)
        try:
            pw = float(it0.text()) if it0 else None
        except ValueError:
            pw = None
        if not hpd_mode:
            txt, col = "Linear", None
        elif pw is None:
            txt, col = "", None
        elif pw <= start_dbm + 1e-9:
            txt, col = "HPD", QColor("#a6e3a1")
        else:
            txt, col = "Linear", QColor("#89b4fa")
        it = QTableWidgetItem(txt)
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        it.setTextAlignment(Qt.AlignCenter)
        if col is not None:
            it.setForeground(col)
        if hpd_mode:
            it.setToolTip("Click to start HPD at this power")
        table.setItem(i, SWEEP_COL, it)


def linear_and_hpd_powers(schedule, start_dbm):
    powers = sorted({float(s[0]) for s in schedule}, reverse=True)
    lin = [p for p in powers if p > start_dbm + 1e-9]
    hpd = [p for p in powers if p <= start_dbm + 1e-9]
    return lin, hpd


# ---------------------------------------------------------------------------
# Pre-run warning
# ---------------------------------------------------------------------------

def confirm_hpd_run(parent, schedule, ctrl: HPDStartControls,
                    custom_schedules: Optional[List] = None) -> bool:
    """Warn before an HPD run; True to proceed."""
    start = ctrl.start_dbm()
    lin, hpd = linear_and_hpd_powers(schedule, start)
    fmt = lambda ps: ", ".join(f"{p:g}" for p in ps)
    if not hpd:
        body = (f"No power in the table is at or below the HPD start ({start:g} dBm), "
                "so this run will use linear sweeps only.")
    elif not lin:
        body = (f"Every power is at or below the HPD start ({start:g} dBm), so HPD runs at every "
                "power, seeded from the resonator's Quality-step fit (or one linear seed "
                "sweep if there is none). There is no seed check in this case.\n\n"
                "Check that this seed fit is good. If the Quality fit was taken in the "
                "Kerr regime, set the HPD start lower so a few linear powers come first.")
    else:
        body = (f"Linear sweeps at: {fmt(lin)} dBm\n"
                f"HPD (segment sweep) at: {fmt(hpd)} dBm\n\n"
                f"The HPD is seeded from the fit at {min(lin):g} dBm (the lowest linear "
                "power). If that fit is bad, for example because the resonance is still "
                "Kerr-distorted there, the HPD points will be placed in the wrong spot.")
        if ctrl.chk_confirm.isChecked():
            body += (f"\n\nSeed check: right after you start, every selected resonator is "
                     f"measured once at {min(lin):g} dBm and the fits are shown in one window. "
                     "Tick the resonators that may use HPD, then the sweep runs unattended. "
                     f"If nobody answers within {ctrl.sp_timeout.value()} min, all "
                     "resonators stay linear.")
        else:
            body += (f"\n\nThe seed check is OFF: the run will seed HPD from the "
                     f"{min(lin):g} dBm fit without asking. Check beforehand that every "
                     f"selected resonator fits well at {min(lin):g} dBm.")
    if custom_schedules:
        body += ("\n\nSome resonators have their own power table; the same HPD start "
                 "power applies to them.")
    box = QMessageBox(QMessageBox.Warning, "Check the HPD seed powers", body,
                      QMessageBox.Ok | QMessageBox.Cancel, parent)
    box.button(QMessageBox.Ok).setText("Start run")
    box.setDefaultButton(QMessageBox.Cancel)
    return box.exec_() == QMessageBox.Ok


# ---------------------------------------------------------------------------
# Seed check window (all resonators at once)
# ---------------------------------------------------------------------------

def _fmt_q(q):
    if q is None or not np.isfinite(q):
        return "—"
    if q >= 1e6:
        return f"{q/1e6:.3f}M"
    if q >= 1e3:
        return f"{q/1e3:.2f}k"
    return f"{q:.0f}"


class HPDSeedCheckDialog(QDialog):
    """
    Non-modal. Emits ``answered`` with {"action": "start", "use": {key: bool}}
    or {"action": "abort"}. Closing it with the window button counts as
    "start with every resonator linear". ``close_without_answer`` is called when
    the worker timed out or the run was stopped.
    """
    answered = pyqtSignal(dict)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.items: List[dict] = list(info.get("items", []))
        self._done = False
        self.setWindowTitle("HPD seed check — confirm the fits before the power sweep")
        self.setModal(False)
        self.setMinimumSize(1000, 600)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        V = QVBoxLayout(self)
        hs = info.get("hpd_start_dbm")
        head = QLabel(
            f"Each resonator was measured once with a linear sweep at its seed power. "
            f"HPD (from <b>{hs:g} dBm</b> down) will place its points using that fit. "
            "Untick any resonator whose fit does not follow the data (e.g. a "
            "Kerr-distorted line); it will be measured with linear sweeps at every power.")
        head.setWordWrap(True); V.addWidget(head)

        split = QSplitter(Qt.Horizontal)
        left = QWidget(); LV = QVBoxLayout(left); LV.setContentsMargins(0, 0, 0, 0)
        self.lst = QListWidget()
        for it in self.items:
            row = QListWidgetItem(self._item_text(it))
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            if it["ok"]:
                row.setCheckState(Qt.Checked)
            else:
                row.setCheckState(Qt.Unchecked)
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
                row.setForeground(QColor("#f38ba8"))
            row.setData(Qt.UserRole, it["key"])
            self.lst.addItem(row)
        self.lst.currentRowChanged.connect(self._show)
        self.lst.itemChanged.connect(lambda *_: self._update_counts())
        LV.addWidget(self.lst, 1)
        brow = QHBoxLayout()
        b_all = QPushButton("All HPD"); b_none = QPushButton("All linear")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none.clicked.connect(lambda: self._set_all(False))
        brow.addWidget(b_all); brow.addWidget(b_none)
        LV.addLayout(brow)
        split.addWidget(left)

        right = QWidget(); RV = QVBoxLayout(right); RV.setContentsMargins(0, 0, 0, 0)
        plots = QHBoxLayout()
        self.pl_circle = pg.PlotWidget(title="Complex plane")
        self.pl_circle.setAspectLocked(True); self.pl_circle.showGrid(x=True, y=True, alpha=0.3)
        self.pl_mag = pg.PlotWidget(title="|S21|")
        self.pl_mag.setLabel("bottom", "f − f_center", units="Hz")
        self.pl_mag.setLabel("left", "dB"); self.pl_mag.showGrid(x=True, y=True, alpha=0.3)
        plots.addWidget(self.pl_circle, 1); plots.addWidget(self.pl_mag, 1)
        RV.addLayout(plots, 1)
        self.lbl_fit = QLabel(""); self.lbl_fit.setWordWrap(True); RV.addWidget(self.lbl_fit)
        split.addWidget(right)
        left.setMinimumWidth(300)
        split.setStretchFactor(0, 1); split.setStretchFactor(1, 3)
        split.setSizes([320, 680])
        V.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.lbl_count = QLabel(""); self.lbl_count.setStyleSheet("font-weight:bold;")
        bottom.addWidget(self.lbl_count); bottom.addStretch()
        self.lbl_sel = QLabel("")
        bottom.addWidget(self.lbl_sel)
        self.btn_start = QPushButton("Start power sweep"); self.btn_start.setObjectName("primary")
        self.btn_abort = QPushButton("Abort run"); self.btn_abort.setObjectName("danger")
        self.btn_start.clicked.connect(self._start)
        self.btn_abort.clicked.connect(lambda: self._answer({"action": "abort"}))
        bottom.addWidget(self.btn_start); bottom.addWidget(self.btn_abort)
        V.addLayout(bottom)

        self._remaining = int(info.get("timeout_s", 600))
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(1000)
        self._tick(first=True)
        self._update_counts()
        if self.items:
            self.lst.setCurrentRow(0)

    # ---------------------------------------------------------------- display
    @staticmethod
    def _item_text(it):
        if it["ok"]:
            return f"{it['label']} @ {it['power_dbm']:g} dBm   Qi={_fmt_q(it.get('Qi'))}"
        return f"{it['label']} @ {it['power_dbm']:g} dBm   not usable"

    def _show(self, row):
        if not (0 <= row < len(self.items)):
            return
        it = self.items[row]
        self.pl_circle.clear(); self.pl_mag.clear()
        fc = 0.5 * (it.get("fstart_hz", 0) + it.get("fstop_hz", 0))
        if it.get("f_hz") is not None:
            self.pl_mag.plot(np.asarray(it["f_hz"]) - fc, np.asarray(it["mag_db"]),
                             pen=None, symbol="o", symbolSize=3,
                             symbolBrush=pg.mkBrush(137, 180, 250, 160), symbolPen=None)
        if it.get("z_raw") is not None and it.get("z_sim") is not None:
            z = np.asarray(it["z_raw"]); zs = np.asarray(it["z_sim"]); ff = np.asarray(it["fit_f_hz"])
            self.pl_circle.plot(z.real, z.imag, pen=None, symbol="o", symbolSize=3,
                                symbolBrush=pg.mkBrush(137, 180, 250, 160), symbolPen=None)
            self.pl_circle.plot(zs.real, zs.imag, pen=pg.mkPen("#f38ba8", width=2))
            self.pl_mag.plot(ff - fc, 20 * np.log10(np.abs(zs) + 1e-30),
                             pen=pg.mkPen("#f38ba8", width=2))
        if it["ok"]:
            fr = it.get("fr")
            self.pl_mag.addItem(pg.InfiniteLine(fr - fc, angle=90,
                                                pen=pg.mkPen("#f9e2af", style=Qt.DashLine)))
            nr = it.get("noise_over_radius")
            self.lbl_fit.setText(
                f"<b>{it['label']}</b> @ {it['power_dbm']:g} dBm:  fr = {fr/1e9:.9f} GHz  ·  "
                f"Ql = {_fmt_q(it.get('Ql'))}  ·  Qi = {_fmt_q(it.get('Qi'))} ± "
                f"{_fmt_q(it.get('Qi_err'))}  ·  |Qc| = {_fmt_q(it.get('absQc'))}"
                + (f"  ·  noise/radius = {nr:.3f}" if nr is not None and np.isfinite(nr) else ""))
        else:
            self.lbl_fit.setText(f"<b>{it['label']}</b> @ {it['power_dbm']:g} dBm cannot seed HPD: "
                                 f"{it.get('error') or 'fit failed'}. It will be measured "
                                 "with linear sweeps at every power.")

    def _set_all(self, on):
        for i in range(self.lst.count()):
            row = self.lst.item(i)
            if self.items[i]["ok"]:
                row.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _selection(self) -> Dict[str, bool]:
        return {self.lst.item(i).data(Qt.UserRole):
                (self.lst.item(i).checkState() == Qt.Checked) for i in range(self.lst.count())}

    def _update_counts(self):
        sel = self._selection()
        n = sum(sel.values())
        self.lbl_sel.setText(f"HPD: {n}   linear: {len(sel) - n}   ")

    def _tick(self, first=False):
        if not first:
            self._remaining -= 1
        m, s = divmod(max(self._remaining, 0), 60)
        self.lbl_count.setText(f"No answer in {m}:{s:02d} → every resonator stays LINEAR")
        if self._remaining <= 0:
            self._timer.stop()

    # ---------------------------------------------------------------- answers
    def _start(self):
        self._answer({"action": "start", "use": self._selection()})

    def _answer(self, d: dict):
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.answered.emit(d)
        self.close()

    def close_without_answer(self, reason: str = ""):
        self._done = True
        self._timer.stop()
        self.close()

    def closeEvent(self, ev):
        if not self._done:                  # window closed by the user = all linear
            self._done = True
            self._timer.stop()
            self.answered.emit({"action": "start", "use": {}})
        super().closeEvent(ev)


class SeedDialogHost:
    """
    Mix into a window whose worker has ``seed_precheck`` / ``seed_precheck_closed``
    signals and ``answer_seed``. Call ``self._connect_seed_dialog(worker)``
    after creating the worker.
    """

    def _connect_seed_dialog(self, worker):
        self._seed_dlg = None
        worker.seed_precheck.connect(lambda info: self._show_seed_dialog(worker, info))
        worker.seed_precheck_closed.connect(self._close_seed_dialog)

    def _show_seed_dialog(self, worker, info):
        self._close_seed_dialog("replaced")
        dlg = HPDSeedCheckDialog(info, self)
        dlg.answered.connect(worker.answer_seed)
        dlg.answered.connect(self._log_seed_answer)
        self._seed_dlg = dlg
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        try:
            from PyQt5.QtWidgets import QApplication
            QApplication.alert(self, 0)
        except Exception:
            pass

    def _log_seed_answer(self, d):
        if d.get("action") == "abort":
            self._log("  Seed check: abort run.")
            return
        use = d.get("use", {})
        hpd = [k.split("|")[-1] for k, v in use.items() if v]
        self._log(f"  Seed check: HPD for {len(hpd)} resonator(s)"
                  + (f" (Res {', '.join(hpd)})" if hpd else "")
                  + f", linear for {len(use) - len(hpd)}.")

    def _close_seed_dialog(self, reason=""):
        dlg = getattr(self, "_seed_dlg", None)
        if dlg is not None:
            try:
                dlg.close_without_answer(reason)
            except RuntimeError:
                pass
            self._seed_dlg = None
            if reason == "timeout":
                self._log("  Seed check timed out — all resonators stay linear.")
