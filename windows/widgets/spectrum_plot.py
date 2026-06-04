"""
windows/widgets/spectrum_plot.py
================================
Reusable embedded spectrum plot built on pyqtgraph. Used by the wideband window
(pick resonances) and later by the span picker / quality views.

Features
--------
* |S21| (dB) vs frequency (GHz), fast and zoomable/pannable (native pyqtgraph).
* Hover crosshair that snaps to the nearest data point and reports the exact
  frequency (and magnitude) in a readout label.
* Click to add a pick marker at the nearest data point; click an existing marker
  again (within a small tolerance) to remove it. ``picksChanged`` fires on every
  change; ``picked_frequencies_hz()`` returns the sorted picks.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import pyqtSignal, Qt

pg.setConfigOptions(antialias=True)
from core import theme


class SpectrumPlot(QWidget):
    picksChanged = pyqtSignal(list)   # list of frequencies in Hz

    def __init__(self, allow_picking: bool = True, parent=None):
        super().__init__(parent)
        self.allow_picking = allow_picking
        self._freq_ghz: Optional[np.ndarray] = None
        self._mag_db: Optional[np.ndarray] = None
        self._picked_idx: List[int] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.readout = QLabel("Hover over the trace to read frequency.")
        self.readout.setStyleSheet(f"color:{theme.hx('subtext')}; font-family:monospace;")
        layout.addWidget(self.readout)

        self.plot = pg.PlotWidget()
        theme.style_plot(self.plot)
        self.plot.setLabel("bottom", "Frequency", units="GHz")
        self.plot.setLabel("left", "|S21|", units="dB")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot, 1)

        self.curve = self.plot.plot([], [], pen=pg.mkPen("#89b4fa", width=1))
        self.pick_scatter = pg.ScatterPlotItem(
            size=11, pen=pg.mkPen("#11111b", width=1),
            brush=pg.mkBrush("#f38ba8"), symbol="o")
        self.plot.addItem(self.pick_scatter)

        # crosshair
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#6c7086", style=Qt.DashLine))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen("#6c7086", style=Qt.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)

        self._proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved,
                                     rateLimit=60, slot=self._on_move)
        self.plot.scene().sigMouseClicked.connect(self._on_click)

    # ------------------------------------------------------------------

    def set_data(self, freq_ghz, mag_db):
        self._freq_ghz = np.asarray(freq_ghz, dtype=float)
        self._mag_db = np.asarray(mag_db, dtype=float)
        self.curve.setData(self._freq_ghz, self._mag_db)
        self.clear_picks()
        self.plot.enableAutoRange()

    def clear_picks(self):
        self._picked_idx = []
        self._refresh_picks()

    def set_title(self, text: str):
        self.plot.setTitle(text, color="#cdd6f4", size="10pt")

    # ------------------------------------------------------------------

    def _nearest_index(self, x_ghz: float) -> Optional[int]:
        if self._freq_ghz is None or self._freq_ghz.size == 0:
            return None
        return int(np.argmin(np.abs(self._freq_ghz - x_ghz)))

    def _on_move(self, evt):
        if self._freq_ghz is None:
            return
        pos = evt[0]
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mouse = vb.mapSceneToView(pos)
        idx = self._nearest_index(mouse.x())
        if idx is None:
            return
        fx = self._freq_ghz[idx]
        fy = self._mag_db[idx]
        self.vline.setPos(fx)
        self.hline.setPos(fy)
        self.readout.setText(
            f"f = {fx*1e9:,.0f} Hz  ({fx:.6f} GHz)    |S21| = {fy:.2f} dB"
            f"    picks: {len(self._picked_idx)}")

    def _on_click(self, evt):
        if not self.allow_picking or self._freq_ghz is None:
            return
        if evt.button() != Qt.LeftButton:
            return
        vb = self.plot.getPlotItem().vb
        mouse = vb.mapSceneToView(evt.scenePos())
        idx = self._nearest_index(mouse.x())
        if idx is None:
            return
        # tolerance for "click an existing marker to remove it": a fraction of
        # the visible x-range.
        xr = vb.viewRange()[0]
        tol = (xr[1] - xr[0]) * 0.01
        removed = False
        for pi in list(self._picked_idx):
            if abs(self._freq_ghz[pi] - self._freq_ghz[idx]) <= tol:
                self._picked_idx.remove(pi)
                removed = True
                break
        if not removed:
            self._picked_idx.append(idx)
        self._refresh_picks()

    def _refresh_picks(self):
        self._picked_idx = sorted(set(self._picked_idx))
        if self._freq_ghz is None or not self._picked_idx:
            self.pick_scatter.setData([], [])
        else:
            xs = self._freq_ghz[self._picked_idx]
            ys = self._mag_db[self._picked_idx]
            self.pick_scatter.setData(xs, ys)
        self.picksChanged.emit(self.picked_frequencies_hz())

    # ------------------------------------------------------------------

    def picked_frequencies_hz(self) -> List[float]:
        if self._freq_ghz is None:
            return []
        return [float(self._freq_ghz[i] * 1e9) for i in sorted(self._picked_idx)]

    def set_picks_hz(self, freqs_hz: List[float]):
        if self._freq_ghz is None:
            return
        self._picked_idx = []
        for f in freqs_hz:
            i = self._nearest_index(f / 1e9)
            if i is not None:
                self._picked_idx.append(i)
        self._refresh_picks()
