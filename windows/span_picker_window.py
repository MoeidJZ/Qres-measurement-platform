"""
windows/span_picker_window.py
=============================
Step 2 of the workflow: turn the confirmed wideband picks into a curated list
of resonators, each with a chosen frequency span.

Flow
----
* Each pick becomes a resonator, sorted by frequency and labelled Res 1 … Res N
  (lowest → highest). Selecting one zooms the *existing* wideband data to a
  window around it (default 2 MHz, adjustable + Re-show).
* A draggable region sets that resonator's start/stop frequency (kept far enough
  from resonance that |S21| has settled).
* Per resonator: Confirm (accept span), Ignore (keep in list, don't measure),
  or Discard (delete + renumber). Discarding a pick with no visible resonance is
  the intended "this one's a dud" action.
* When done, "Confirm all & continue" persists the curated list (survives
  sessions) and hands it to the quality-assessment step (Phase 6).
"""

from __future__ import annotations

import logging
from typing import List, Dict

import numpy as np
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QDoubleSpinBox, QGroupBox, QSplitter,
)
from PyQt5.QtCore import Qt, pyqtSignal

from core.settings import settings
from windows.widgets.span_plot import SpanPlot
from core import theme

logger = logging.getLogger(__name__)

STATUS_COLORS = {"pending": "#f9e2af", "confirmed": "#a6e3a1", "ignored": "#6c7086"}


class SpanPickerWindow(QMainWindow):
    resonatorsReady = pyqtSignal(list)   # list of resonator dicts (confirmed)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("2 · Per-Resonator Span Picker")
        self.setMinimumSize(1080, 700)
        self._freq_ghz = np.array([])
        self._mag_db = np.array([])
        self._res: List[Dict] = []
        self._current = -1
        self._build_ui()

    # ------------------------------------------------------------------
    # Population from wideband result
    # ------------------------------------------------------------------

    def load(self, picks_hz: List[float], wideband_result: dict):
        self._freq_ghz = np.asarray(wideband_result.get("freq_ghz", []), dtype=float)
        self._mag_db = np.asarray(wideband_result.get("mag_db", []), dtype=float)
        default_span = float(settings.get("span_picker.default_span_mhz", 2.0))
        self._res = []
        for f in sorted(picks_hz):
            c = float(f)
            self._res.append({
                "center_hz": c,
                "span_mhz": default_span,
                "fstart_hz": c - default_span / 2 * 1e6,
                "fstop_hz": c + default_span / 2 * 1e6,
                "status": "pending",
            })
        self._renumber()
        self._refresh_list()
        if self._res:
            self.list.setCurrentRow(0)

    def _renumber(self):
        self._res.sort(key=lambda r: r["center_hz"])
        for i, r in enumerate(self._res, start=1):
            r["num"] = i

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        self.setCentralWidget(root)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split, 1)

        # left: list + actions
        left = QWidget(); L = QVBoxLayout(left); L.setContentsMargins(10, 10, 10, 10)
        L.addWidget(QLabel("Resonators (lowest → highest)"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_select)
        L.addWidget(self.list, 1)
        act = QGroupBox("This resonator")
        ag = QVBoxLayout(act)
        self.btn_confirm = QPushButton("Confirm span"); self.btn_confirm.setObjectName("success")
        self.btn_confirm.clicked.connect(self._confirm_current)
        self.btn_ignore = QPushButton("Ignore (keep, don't measure)")
        self.btn_ignore.clicked.connect(self._ignore_current)
        self.btn_discard = QPushButton("Discard (delete)"); self.btn_discard.setObjectName("danger")
        self.btn_discard.clicked.connect(self._discard_current)
        for b in (self.btn_confirm, self.btn_ignore, self.btn_discard):
            ag.addWidget(b)
        L.addWidget(act)
        split.addWidget(left)

        # right: zoom plot + span control
        right = QWidget(); R = QVBoxLayout(right); R.setContentsMargins(10, 10, 10, 10)
        self.lbl_head = QLabel("Select a resonator.")
        self.lbl_head.setStyleSheet(f"font-weight:bold; color:{theme.hx('accent')};")
        R.addWidget(self.lbl_head)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("View window:"))
        self.sp_span = QDoubleSpinBox(); self.sp_span.setRange(0.05, 500); self.sp_span.setDecimals(3)
        self.sp_span.setSuffix(" MHz"); self.sp_span.setValue(2.0)
        srow.addWidget(self.sp_span)
        self.btn_reshow = QPushButton("Re-show")
        self.btn_reshow.clicked.connect(self._reshow_current)
        srow.addWidget(self.btn_reshow)
        srow.addStretch()
        R.addLayout(srow)

        self.zoom = SpanPlot()
        self.zoom.regionChanged.connect(self._on_region)
        R.addWidget(self.zoom, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        # bottom: continue
        brow = QHBoxLayout()
        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet(f"color:{theme.hx('subtext')};")
        brow.addWidget(self.lbl_summary)
        brow.addStretch()
        self.btn_continue = QPushButton("Confirm all & continue  →  Quality")
        self.btn_continue.setObjectName("primary")
        self.btn_continue.clicked.connect(self._continue)
        brow.addWidget(self.btn_continue)
        outer.addLayout(brow)

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------

    def _refresh_list(self):
        cur = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for r in self._res:
            item = QListWidgetItem(
                f"Res {r['num']}   {r['center_hz']/1e9:.6f} GHz   [{r['status']}]")
            item.setForeground(_qcolor(STATUS_COLORS.get(r["status"], "#cdd6f4")))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if 0 <= cur < self.list.count():
            self.list.setCurrentRow(cur)
        self._update_summary()

    def _update_summary(self):
        n = len(self._res)
        c = sum(1 for r in self._res if r["status"] == "confirmed")
        ig = sum(1 for r in self._res if r["status"] == "ignored")
        self.lbl_summary.setText(f"{n} resonators · {c} confirmed · {ig} ignored")
        self.btn_continue.setEnabled(c > 0)

    # ------------------------------------------------------------------
    # Selection / zoom
    # ------------------------------------------------------------------

    def _slice(self, center_hz, span_mhz):
        if self._freq_ghz.size == 0:
            return np.array([]), np.array([])
        half = span_mhz / 2 * 1e6
        lo = (center_hz - half) / 1e9
        hi = (center_hz + half) / 1e9
        mask = (self._freq_ghz >= lo) & (self._freq_ghz <= hi)
        return self._freq_ghz[mask], self._mag_db[mask]

    def _on_select(self, row: int):
        self._current = row
        if not (0 <= row < len(self._res)):
            return
        r = self._res[row]
        self.sp_span.setValue(r["span_mhz"])
        fz, mz = self._slice(r["center_hz"], r["span_mhz"])
        self.lbl_head.setText(
            f"Res {r['num']} — center {r['center_hz']/1e9:.6f} GHz  [{r['status']}]")
        if fz.size == 0:
            self.zoom.set_data([], [], r["center_hz"])
            self.lbl_head.setText(self.lbl_head.text() + "  (no wideband data in window)")
            return
        self.zoom.set_data(fz, mz, r["center_hz"],
                           region_hz=(r["fstart_hz"], r["fstop_hz"]))

    def _reshow_current(self):
        if not (0 <= self._current < len(self._res)):
            return
        r = self._res[self._current]
        span_mhz = self.sp_span.value()
        r["span_mhz"] = span_mhz
        half = span_mhz / 2 * 1e6
        f0, f1 = r["center_hz"] - half, r["center_hz"] + half
        # show a little context around the chosen span, but make the selected
        # region exactly the chosen span (so 1 MHz stays 1 MHz)
        fz, mz = self._slice(r["center_hz"], span_mhz * 1.4)
        if fz.size:
            self.zoom.set_data(fz, mz, r["center_hz"], region_hz=(f0, f1))
        r["fstart_hz"], r["fstop_hz"] = f0, f1
        self._refresh_list()

    def _on_region(self, start_hz: float, stop_hz: float):
        if 0 <= self._current < len(self._res):
            r = self._res[self._current]
            r["fstart_hz"] = start_hz
            r["fstop_hz"] = stop_hz
            # realign the centre to the middle of the chosen region, so a
            # subsequent span change re-views around the new centre
            r["center_hz"] = 0.5 * (start_hz + stop_hz)
            r["span_mhz"] = (stop_hz - start_hz) / 1e6
            self.lbl_head.setText(
                f"Res {r['num']} — center {r['center_hz']/1e9:.6f} GHz  [{r['status']}]")

    # ------------------------------------------------------------------
    # Per-resonator actions
    # ------------------------------------------------------------------

    def _confirm_current(self):
        if 0 <= self._current < len(self._res):
            self._res[self._current]["status"] = "confirmed"
            self._refresh_list()

    def _ignore_current(self):
        if 0 <= self._current < len(self._res):
            self._res[self._current]["status"] = "ignored"
            self._refresh_list()

    def _discard_current(self):
        if 0 <= self._current < len(self._res):
            del self._res[self._current]
            self._renumber()
            self._refresh_list()
            if self._res:
                self.list.setCurrentRow(min(self._current, len(self._res) - 1))

    # ------------------------------------------------------------------

    def _continue(self):
        confirmed = [self._clean(r) for r in self._res if r["status"] == "confirmed"]
        if not confirmed:
            return
        # persist full curated list (confirmed + ignored) and the measurable set
        settings.set_list("resonators", [self._clean(r) for r in self._res
                                         if r["status"] != "pending"])
        self.resonatorsReady.emit(confirmed)
        logger.info("Span picker: %d confirmed resonators -> quality step", len(confirmed))

    @staticmethod
    def _clean(r: Dict) -> Dict:
        return {
            "num": int(r["num"]),
            "center_hz": float(r["center_hz"]),
            "fstart_hz": float(min(r["fstart_hz"], r["fstop_hz"])),
            "fstop_hz": float(max(r["fstart_hz"], r["fstop_hz"])),
            "span_mhz": float(r["span_mhz"]),
            "status": r["status"],
        }


def _qcolor(hexstr: str):
    from PyQt5.QtGui import QColor
    return QColor(hexstr)
