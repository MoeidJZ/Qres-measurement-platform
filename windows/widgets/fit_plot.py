"""
windows/widgets/fit_plot.py
===========================
Plot for the quality step: raw |S21| (dB) with the circlefit overlay and a
draggable region that sets the fit crop window. Hover reads the exact frequency.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import pyqtSignal, Qt

pg.setConfigOptions(antialias=True)
from core import theme


class FitPlot(QWidget):
    cropChanged = pyqtSignal(float, float)   # lo_hz, hi_hz

    def __init__(self, parent=None):
        super().__init__(parent)
        self._f_hz: Optional[np.ndarray] = None
        self._mag_db: Optional[np.ndarray] = None

        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.readout = QLabel("Drag the shaded region to set the fit window, then Re-fit.")
        self.readout.setStyleSheet(f"color:{theme.hx('subtext')}; font-family:monospace;")
        layout.addWidget(self.readout)

        self.plot = pg.PlotWidget()
        theme.style_plot(self.plot)
        self.plot.setLabel("bottom", "Frequency", units="GHz")
        self.plot.setLabel("left", "|S21|", units="dB")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend(offset=(10, 10))
        layout.addWidget(self.plot, 1)

        self.raw = self.plot.plot([], [], pen=pg.mkPen("#89b4fa", width=1), name="data")
        self.fit = self.plot.plot([], [], pen=pg.mkPen("#f38ba8", width=2), name="fit")

        self.region = pg.LinearRegionItem(
            brush=pg.mkBrush(166, 227, 161, 35), pen=pg.mkPen("#a6e3a1", width=1))
        self.region.setZValue(-5)
        self.plot.addItem(self.region)
        self.region.sigRegionChangeFinished.connect(self._on_region)

        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#6c7086", style=Qt.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self._proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved,
                                     rateLimit=60, slot=self._on_move)

    # ------------------------------------------------------------------

    def set_data(self, f_hz, mag_db):
        self._f_hz = np.asarray(f_hz, dtype=float)
        self._mag_db = np.asarray(mag_db, dtype=float)
        self.raw.setData(self._f_hz / 1e9, self._mag_db)
        self.fit.setData([], [])
        if self._f_hz.size:
            lo, hi = float(self._f_hz[0]), float(self._f_hz[-1])
            self.region.setBounds([lo / 1e9, hi / 1e9])
            self.region.setRegion([lo / 1e9, hi / 1e9])
            self.plot.enableAutoRange()

    def set_fit(self, f_sim_hz, mag_sim_db):
        if f_sim_hz is None or mag_sim_db is None:
            self.fit.setData([], [])
            return
        self.fit.setData(np.asarray(f_sim_hz) / 1e9, np.asarray(mag_sim_db))

    def get_crop_hz(self) -> Tuple[float, float]:
        lo, hi = self.region.getRegion()
        return float(lo * 1e9), float(hi * 1e9)

    def set_header(self, text: str):
        self.plot.setTitle(text, color="#cdd6f4", size="10pt")

    # ------------------------------------------------------------------

    def _on_region(self):
        lo, hi = self.get_crop_hz()
        self.cropChanged.emit(lo, hi)

    def _on_move(self, evt):
        if self._f_hz is None:
            return
        pos = evt[0]; vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mouse = vb.mapSceneToView(pos)
        idx = int(np.argmin(np.abs(self._f_hz / 1e9 - mouse.x())))
        fx = self._f_hz[idx]; fy = self._mag_db[idx]
        self.vline.setPos(fx / 1e9)
        self.readout.setText(f"cursor f = {fx:,.0f} Hz   |S21| = {fy:.2f} dB")
