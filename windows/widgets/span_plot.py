"""
windows/widgets/span_plot.py
============================
Zoomed single-resonator view used by the span picker. Shows a slice of the
existing wideband data around a resonance and a draggable region whose two edges
set the measurement start/stop frequency. Hover reports the exact frequency.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import pyqtSignal, Qt

pg.setConfigOptions(antialias=True)
from core import theme


class SpanPlot(QWidget):
    regionChanged = pyqtSignal(float, float)   # start_hz, stop_hz

    def __init__(self, parent=None):
        super().__init__(parent)
        self._freq_ghz: Optional[np.ndarray] = None
        self._mag_db: Optional[np.ndarray] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.readout = QLabel("Drag the shaded edges to set start / stop.")
        self.readout.setStyleSheet(f"color:{theme.hx('subtext')}; font-family:monospace;")
        layout.addWidget(self.readout)

        self.plot = pg.PlotWidget()
        theme.style_plot(self.plot)
        self.plot.setLabel("bottom", "Frequency", units="GHz")
        self.plot.setLabel("left", "|S21|", units="dB")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot, 1)

        self.curve = self.plot.plot([], [], pen=pg.mkPen("#89b4fa", width=1))
        self.center_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#a6e3a1", style=Qt.DotLine))
        self.plot.addItem(self.center_line)

        self.region = pg.LinearRegionItem(
            brush=pg.mkBrush(137, 180, 250, 40),
            pen=pg.mkPen("#89b4fa", width=2))
        self.region.setZValue(10)
        self.plot.addItem(self.region)
        self.region.sigRegionChanged.connect(self._on_region)

        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#6c7086", style=Qt.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self._proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved,
                                     rateLimit=60, slot=self._on_move)

    # ------------------------------------------------------------------

    def set_data(self, freq_ghz, mag_db, center_hz: float,
                 region_hz: Optional[Tuple[float, float]] = None):
        self._freq_ghz = np.asarray(freq_ghz, dtype=float)
        self._mag_db = np.asarray(mag_db, dtype=float)
        self.curve.setData(self._freq_ghz, self._mag_db)
        self.center_line.setPos(center_hz / 1e9)
        if self._freq_ghz.size:
            lo, hi = float(self._freq_ghz[0]), float(self._freq_ghz[-1])
            if region_hz is None:
                a, b = lo + (hi - lo) * 0.15, hi - (hi - lo) * 0.15
            else:
                a, b = region_hz[0] / 1e9, region_hz[1] / 1e9
            self.region.setBounds([lo, hi])
            self.region.setRegion([a, b])
            self.plot.setXRange(lo, hi)
            self.plot.enableAutoRange(axis="y")

    def get_span_hz(self) -> Tuple[float, float]:
        lo, hi = self.region.getRegion()
        return float(lo * 1e9), float(hi * 1e9)

    def has_data(self) -> bool:
        return self._freq_ghz is not None and self._freq_ghz.size > 0

    # ------------------------------------------------------------------

    def _on_region(self):
        a, b = self.get_span_hz()
        self.readout.setText(
            f"start = {a:,.0f} Hz   stop = {b:,.0f} Hz   "
            f"span = {(b - a)/1e6:.3f} MHz")
        self.regionChanged.emit(a, b)

    def _on_move(self, evt):
        if self._freq_ghz is None:
            return
        pos = evt[0]
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mouse = vb.mapSceneToView(pos)
        idx = int(np.argmin(np.abs(self._freq_ghz - mouse.x())))
        fx = self._freq_ghz[idx]; fy = self._mag_db[idx]
        self.vline.setPos(fx)
        a, b = self.get_span_hz()
        self.readout.setText(
            f"cursor f = {fx*1e9:,.0f} Hz  |S21| = {fy:.2f} dB    "
            f"span = {(b - a)/1e6:.3f} MHz")
