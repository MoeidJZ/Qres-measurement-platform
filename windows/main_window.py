"""
windows/main_window.py
======================
The measurement platform shell. Hosts the dockable Instrument Control panel and
the PNA Parameters window, provides a toolbar to (re)open them, and exposes the
workflow steps. Measurement-launching buttons grey out while a run is in
progress (instrument_manager.busy); the live fridge knobs in the dock stay
usable at all times, by design.

The individual workflow windows (wideband, span picker, quality, power,
temperature, analysis) are filled in by later phases; the buttons are present
and correctly enabled/disabled now.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QToolBar, QAction,
)
from PyQt5.QtCore import Qt

from core.instrument_manager import instrument_manager
from windows.instrument_control_dock import InstrumentControlDock
from windows.pna_window import PNAWindow
from core import theme


class MainPlatformWindow(QMainWindow):
    def __init__(self, on_back=None):
        super().__init__()
        self.on_back = on_back
        self.setWindowTitle("QRes Platform — Measurement")
        self.setMinimumSize(960, 640)

        self._dock = None
        self._pna_window = None
        self._wideband_window = None
        self._span_picker = None
        self._quality_window = None
        self._power_window = None
        self._temperature_window = None
        self._analysis_window = None
        self._tutorials = []
        self._measurement_buttons = []   # greyed while busy

        self._build_toolbar()
        self._build_ui()
        self._open_dock()

        instrument_manager.on_busy_changed(self._on_busy)
        self._on_busy(instrument_manager.busy)

    # ------------------------------------------------------------------

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        act_ctrl = QAction("Instrument Control", self)
        act_ctrl.triggered.connect(self._open_dock)
        tb.addAction(act_ctrl)

        self.act_pna = QAction("PNA Parameters", self)
        self.act_pna.triggered.connect(self._open_pna)
        tb.addAction(self.act_pna)

        tb.addSeparator()
        from PyQt5.QtWidgets import QComboBox, QLabel as _QL
        from core.theme import current_mode, set_mode
        tb.addWidget(_QL(" Theme: "))
        self._cmb_theme = QComboBox(); self._cmb_theme.addItems(["Dark", "Bright"])
        self._cmb_theme.setCurrentIndex(0 if current_mode() == "dark" else 1)
        self._cmb_theme.currentIndexChanged.connect(self._switch_theme)
        tb.addWidget(self._cmb_theme)

        tb.addSeparator()
        act_help_m = QAction("Tutorial: Measurement", self)
        act_help_m.triggered.connect(lambda: self._open_tutorial("measurement"))
        tb.addAction(act_help_m)
        act_help_a = QAction("Tutorial: Analysis", self)
        act_help_a.triggered.connect(lambda: self._open_tutorial("analysis"))
        tb.addAction(act_help_a)

        tb.addSeparator()
        act_back = QAction("← Connection", self)
        act_back.triggered.connect(self._go_back)
        tb.addAction(act_back)

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        self.setCentralWidget(root)

        title = QLabel("Measurement Platform")
        title.setStyleSheet(f"font-size:18px; font-weight:bold; color:{theme.hx('accent')};")
        layout.addWidget(title)

        summary = QGroupBox("Session")
        s = QVBoxLayout(summary)
        s.addWidget(QLabel(
            f"Fridge: {instrument_manager.fridge_kind or '—'} "
            f"({'connected' if instrument_manager.fridge_connected() else 'not connected'})"))
        s.addWidget(QLabel(f"PNA: {'connected' if instrument_manager.pna_connected() else 'not connected'}"))
        s.addWidget(QLabel(f"Sample: {instrument_manager.sample_name or '—'}"))
        s.addWidget(QLabel(f"Database: {instrument_manager.db_path or '—'}"))
        layout.addWidget(summary)

        steps = QGroupBox("Workflow")
        g = QVBoxLayout(steps)
        # (key, label, needs_pna, phase_ready, handler)
        specs = [
            ("wideband", "1 · Wideband scan + pick resonances", True, True, self._open_wideband),
            ("span", "2 · Per-resonator span picker", True, True, self._open_span_picker),
            ("quality", "3 · Quality assessment (Qi fit)", True, True, self._open_quality),
            ("power", "4 · Power-dependent sweep", True, True, self._open_power),
            ("temperature", "5 · Temperature-dependent sweep", True, True, self._open_temperature),
            ("analysis", "Analysis (Probst Qi vs power)", False, True, self._open_analysis),
        ]
        self._step_buttons = {}
        for key, label, needs_pna, ready, handler in specs:
            b = QPushButton(label + ("" if ready else "   (coming soon)"))
            b.setProperty("needs_pna", needs_pna)
            b.setEnabled(ready)
            if handler is not None:
                b.clicked.connect(handler)
            g.addWidget(b)
            self._measurement_buttons.append(b)
            self._step_buttons[key] = b
        layout.addWidget(steps)

        layout.addStretch()
        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------
    # Dock / PNA window management
    # ------------------------------------------------------------------

    def _open_dock(self):
        if self._dock is None:
            self._dock = InstrumentControlDock(self)
            self.addDockWidget(Qt.RightDockWidgetArea, self._dock)
        self._dock.show()
        self._dock.raise_()

    def _open_pna(self):
        if not instrument_manager.pna_connected():
            self.statusBar().showMessage("PNA not connected — connect it first.", 4000)
            return
        if self._pna_window is None:
            self._pna_window = PNAWindow(self)
        self._pna_window.show()
        self._pna_window.raise_()

    def _open_wideband(self):
        if not instrument_manager.pna_connected():
            self.statusBar().showMessage("PNA not connected — connect it first.", 4000)
            return
        if self._wideband_window is None:
            from windows.wideband_window import WidebandWindow
            self._wideband_window = WidebandWindow(self)
            self._wideband_window.picksConfirmed.connect(self._on_picks_confirmed)
        self._wideband_window.show()
        self._wideband_window.raise_()

    def _on_picks_confirmed(self, freqs_hz, result):
        win = self._ensure_span_picker()
        win.load(freqs_hz, result)
        win.show(); win.raise_()

    def _ensure_span_picker(self):
        if self._span_picker is None:
            from windows.span_picker_window import SpanPickerWindow
            self._span_picker = SpanPickerWindow(self)
            self._span_picker.resonatorsReady.connect(self._on_resonators_ready)
        return self._span_picker

    def _open_span_picker(self):
        win = self._ensure_span_picker()
        if win.list.count() == 0:
            self.statusBar().showMessage(
                "No resonators yet — run a wideband scan and confirm picks first.", 5000)
        win.show(); win.raise_()

    def _on_resonators_ready(self, confirmed):
        win = self._ensure_quality()
        win.load(confirmed)
        win.show(); win.raise_()

    def _ensure_quality(self):
        if self._quality_window is None:
            from windows.quality_window import QualityWindow
            self._quality_window = QualityWindow(self)
            self._quality_window.resonatorsForPower.connect(self._on_resonators_for_power)
        return self._quality_window

    def _open_quality(self):
        win = self._ensure_quality()
        if not win._res:
            self.statusBar().showMessage(
                "No resonators yet — finish the span picker, or use "
                "“Load from database” on the quality page.", 6000)
        win.show(); win.raise_()

    def _on_resonators_for_power(self, chosen):
        win = self._ensure_power()
        win.load(chosen)
        win.show(); win.raise_()

    def _ensure_power(self):
        if self._power_window is None:
            from windows.power_window import PowerWindow
            self._power_window = PowerWindow(self)
            self._power_window.resonatorsForTemperature.connect(self._on_resonators_for_temp)
        return self._power_window

    def _open_power(self):
        win = self._ensure_power()
        if not win._resonators:
            self.statusBar().showMessage(
                "No resonators selected yet — run the quality step and continue.", 5000)
        win.show(); win.raise_()

    def _on_resonators_for_temp(self, chosen):
        win = self._ensure_temperature()
        win.load(chosen)
        win.show(); win.raise_()

    def _ensure_temperature(self):
        if self._temperature_window is None:
            from windows.temperature_window import TemperatureWindow
            self._temperature_window = TemperatureWindow(self)
        return self._temperature_window

    def _open_temperature(self):
        win = self._ensure_temperature()
        if not win._resonators:
            self.statusBar().showMessage(
                "No resonators yet — run the power step and continue, or finish quality first.", 5000)
        win.show(); win.raise_()

    def _open_analysis(self):
        if self._analysis_window is None:
            from windows.analysis_window import AnalysisWindow
            self._analysis_window = AnalysisWindow(self)
        self._analysis_window.show(); self._analysis_window.raise_()

    def _open_tutorial(self, kind):
        from windows.tutorial_window import TutorialWindow
        t = TutorialWindow(kind, self)
        self._tutorials.append(t)
        t.show(); t.raise_()

    def _switch_theme(self, idx):
        from PyQt5.QtWidgets import QApplication, QMessageBox
        from core.theme import set_mode
        set_mode("dark" if idx == 0 else "light", QApplication.instance())
        self.statusBar().showMessage(
            "Theme applied. Windows already open will adopt it when reopened.", 5000)

    # ------------------------------------------------------------------

    def _on_busy(self, busy: bool):
        # Measurement-launch buttons grey out while running; the control dock
        # stays live, and Analysis stays usable (it reads the live db).
        self.act_pna.setEnabled(not busy)
        for key, b in self._step_buttons.items():
            ready = "(coming soon)" not in b.text()
            if key == "analysis":
                b.setEnabled(ready)            # always available
            else:
                b.setEnabled(ready and not busy)
        self.statusBar().showMessage("Busy — measurement running" if busy else "Ready")

    def _go_back(self):
        if callable(self.on_back):
            self.on_back()
        self.close()
