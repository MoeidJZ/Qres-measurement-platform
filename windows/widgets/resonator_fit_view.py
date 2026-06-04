"""
windows/widgets/resonator_fit_view.py
=====================================
Probst-style fit view: raw |S21| (dB) and phase (deg) vs frequency, plus the
normalized S21 circle (Im vs Re). Data are drawn as symbols, the fit as a line.
A single frequency region (shown on both the magnitude and phase plots, kept in
sync) selects the fit range; dragging it emits `rangeChanged` (throttled) so the
owner can refit in real time.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel
from PyQt5.QtCore import pyqtSignal, Qt, QTimer

from core import theme

pg.setConfigOptions(antialias=True)


class ResonatorFitView(QWidget):
    rangeChanged = pyqtSignal(float, float)   # lo_hz, hi_hz

    def __init__(self, parent=None, stacked: bool = False):
        super().__init__(parent)
        self._freq: Optional[np.ndarray] = None
        self._sync = False
        c = theme.colors()
        self._c = c

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        self.readout = QLabel("Drag the shaded range to set the fit window.")
        self.readout.setStyleSheet(f"color:{c['muted']}; font-family:monospace;")
        root.addWidget(self.readout)

        # plot widgets
        self.mag = pg.PlotWidget(); theme.style_plot(self.mag)
        self.mag.setLabel("bottom", "Frequency", units="Hz")
        self.mag.setLabel("left", "|S21|", units="dB")
        self.mag.showGrid(x=True, y=True, alpha=c["grid"])

        self.pha = pg.PlotWidget(); theme.style_plot(self.pha)
        self.pha.setLabel("bottom", "Frequency", units="Hz")
        self.pha.setLabel("left", "Phase", units="deg")
        self.pha.showGrid(x=True, y=True, alpha=c["grid"])

        self.cpl = pg.PlotWidget(); theme.style_plot(self.cpl)
        self.cpl.setLabel("bottom", "Re S21")
        self.cpl.setLabel("left", "Im S21")
        self.cpl.showGrid(x=True, y=True, alpha=c["grid"])
        self.cpl.setAspectLocked(True)

        if stacked:
            # complex (square) on top, magnitude centre, phase bottom
            self.cpl.setMinimumHeight(240)
            root.addWidget(self.cpl, 4)
            root.addWidget(self.mag, 3)
            root.addWidget(self.pha, 3)
        else:
            grid = QGridLayout(); grid.setSpacing(6); root.addLayout(grid, 1)
            grid.addWidget(self.mag, 0, 0)
            grid.addWidget(self.pha, 0, 1)
            grid.addWidget(self.cpl, 1, 0, 1, 2)

        sym = dict(pen=None, symbol="o", symbolSize=4,
                   symbolBrush=pg.mkBrush(c["data"]), symbolPen=None)
        self.mag_data = self.mag.plot([], [], **sym)
        self.pha_data = self.pha.plot([], [], **sym)
        self.cpl_data = self.cpl.plot([], [], **sym)
        fitpen = pg.mkPen(c["fit"], width=2)
        self.mag_fit = self.mag.plot([], [], pen=fitpen)
        self.pha_fit = self.pha.plot([], [], pen=fitpen)
        self.cpl_fit = self.cpl.plot([], [], pen=fitpen)

        # synced regions on magnitude + phase
        rb = pg.mkBrush(*c["region"])
        rp = pg.mkPen(c["accent"], width=1)
        self.region_mag = pg.LinearRegionItem(brush=rb, pen=rp); self.region_mag.setZValue(-5)
        self.region_pha = pg.LinearRegionItem(brush=rb, pen=rp); self.region_pha.setZValue(-5)
        self.mag.addItem(self.region_mag); self.pha.addItem(self.region_pha)
        self.region_mag.sigRegionChanged.connect(lambda: self._region_moved(self.region_mag))
        self.region_pha.sigRegionChanged.connect(lambda: self._region_moved(self.region_pha))

        self._timer = QTimer(self); self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._emit_range)

    # ------------------------------------------------------------------

    def set_data(self, freq_hz, mag_db, phase_deg):
        self._freq = np.asarray(freq_hz, float)
        mag = np.asarray(mag_db, float)
        pha = np.asarray(phase_deg, float)
        self.mag_data.setData(self._freq, mag)
        self.pha_data.setData(self._freq, pha)
        self.mag_fit.setData([], []); self.pha_fit.setData([], [])
        self.cpl_data.setData([], []); self.cpl_fit.setData([], [])
        if self._freq.size:
            lo, hi = float(self._freq[0]), float(self._freq[-1])
            self._sync = True
            for reg in (self.region_mag, self.region_pha):
                reg.setBounds([lo, hi]); reg.setRegion([lo, hi])
            self._sync = False
            self.mag.enableAutoRange(); self.pha.enableAutoRange()

    def set_fit(self, fit: dict):
        if not fit or not fit.get("ok"):
            self.mag_fit.setData([], []); self.pha_fit.setData([], [])
            self.cpl_data.setData([], []); self.cpl_fit.setData([], [])
            return
        f = np.asarray(fit["f_sim_hz"], float)
        self.mag_fit.setData(f, np.asarray(fit["mag_sim_db"], float))
        self.pha_fit.setData(f, np.asarray(fit["phase_sim_deg"], float))
        zr = fit.get("z_raw"); zs = fit.get("z_sim_raw")     # raw S21 (Probst style)
        if zr is not None:
            self.cpl_data.setData(np.real(zr), np.imag(zr))
        if zs is not None:
            self.cpl_fit.setData(np.real(zs), np.imag(zs))
        self.cpl.enableAutoRange()

    def get_range_hz(self) -> Tuple[float, float]:
        lo, hi = self.region_mag.getRegion()
        return float(lo), float(hi)

    # alias used by the quality page
    def get_crop_hz(self) -> Tuple[float, float]:
        return self.get_range_hz()

    def reset_range(self):
        if self._freq is None or not self._freq.size:
            return
        lo, hi = float(self._freq[0]), float(self._freq[-1])
        self._sync = True
        for reg in (self.region_mag, self.region_pha):
            reg.setRegion([lo, hi])
        self._sync = False
        self._emit_range()

    # ------------------------------------------------------------------

    def _region_moved(self, src):
        if self._sync:
            return
        lo, hi = src.getRegion()
        self._sync = True
        other = self.region_pha if src is self.region_mag else self.region_mag
        other.setRegion([lo, hi])
        self._sync = False
        self.readout.setText(f"fit range: {lo:,.0f} – {hi:,.0f} Hz "
                             f"({(hi-lo)/1e6:.3f} MHz)")
        self._timer.start()          # throttle live refits

    def _emit_range(self):
        lo, hi = self.get_range_hz()
        self.rangeChanged.emit(lo, hi)
