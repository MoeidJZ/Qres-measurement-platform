"""
windows/welcome_window.py
=========================
First screen. Explains the platform and asks which cryostat is in use.
Selecting one creates the (not-yet-connected) fridge backend and opens the
connection window.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QComboBox,
)
from PyQt5.QtCore import Qt

from core.instrument_manager import instrument_manager
from core.settings import settings
from core import theme


FRIDGES = [
    ("proteox", "Oxford Proteox", "Dilution refrigerator · base ≈ 15 mK",
     "Uses the oiDECS / DECS-VISA driver."),
    ("teslatron", "Oxford Teslatron", "VTI cryostat · base ≈ 1.5 K",
     "Uses the Mercury iTC driver."),
    ("manual", "Other / Manual", "No automated fridge control",
     "PNA + analysis only; temperature entered manually for labels."),
]


class WelcomeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QRes Platform")
        self.setMinimumSize(760, 560)
        self._connection_window = None
        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(14)
        layout.setContentsMargins(40, 36, 40, 36)
        self.setCentralWidget(root)

        title = QLabel("🔬  QRes Platform")
        title.setStyleSheet(f"font-size: 26px; font-weight: bold; color:{theme.hx('accent')};")
        trow = QHBoxLayout()
        trow.addWidget(title); trow.addStretch()
        trow.addWidget(QLabel("Theme:"))
        self.cmb_theme = QComboBox(); self.cmb_theme.addItems(["Dark", "Bright"])
        from core.theme import current_mode
        self.cmb_theme.setCurrentIndex(0 if current_mode() == "dark" else 1)
        self.cmb_theme.currentIndexChanged.connect(self._change_theme)
        trow.addWidget(self.cmb_theme)
        layout.addLayout(trow)

        blurb = QLabel(
            "Measure and analyze superconducting resonators seamlessly — from a "
            "wideband scan, through power- and temperature-dependent sweeps, to "
            "Qi fitting — all in one guided workflow while you stay in control at "
            "every step."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color:{theme.hx('subtext')}; font-size: 13px;")
        layout.addWidget(blurb)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        prompt = QLabel("Which system are you using?")
        prompt.setStyleSheet("font-size: 15px; font-weight: bold; padding-top:6px;")
        layout.addWidget(prompt)

        last = settings.get("app.last_fridge", "")
        for kind, name, sub, note in FRIDGES:
            layout.addWidget(self._fridge_card(kind, name, sub, note, highlight=(kind == last)))

        layout.addStretch()
        help_row = QHBoxLayout()
        btn_tm = QPushButton("Tutorial: Measurement")
        btn_tm.clicked.connect(lambda: self._tutorial("measurement"))
        btn_ta = QPushButton("Tutorial: Analysis")
        btn_ta.clicked.connect(lambda: self._tutorial("analysis"))
        help_row.addWidget(btn_tm); help_row.addWidget(btn_ta); help_row.addStretch()
        layout.addLayout(help_row)
        foot = QLabel("Your last selections and parameters are remembered between sessions.")
        foot.setStyleSheet(f"color:{theme.hx('muted')}; font-size:11px;")
        layout.addWidget(foot)

    def _fridge_card(self, kind, name, sub, note, highlight=False):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { border:1px solid %s; border-radius:8px; }" %
            ("#89b4fa" if highlight else "#45475a")
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(16, 12, 16, 12)

        txt = QVBoxLayout()
        n = QLabel(name); n.setStyleSheet(f"font-size:15px; font-weight:bold; color:{theme.hx('text')}; border:none;")
        s = QLabel(sub); s.setStyleSheet(f"color:{theme.hx('subtext')}; border:none;")
        nt = QLabel(note); nt.setStyleSheet(f"color:{theme.hx('muted')}; font-size:11px; border:none;")
        txt.addWidget(n); txt.addWidget(s); txt.addWidget(nt)
        row.addLayout(txt)
        row.addStretch()

        btn = QPushButton("Select" + ("  ✓" if highlight else ""))
        btn.setObjectName("primary")
        btn.setMinimumWidth(120)
        btn.clicked.connect(lambda _=False, k=kind: self._choose(k))
        row.addWidget(btn)
        return frame

    def _choose(self, kind: str):
        instrument_manager.select_fridge(kind)
        # Lazy import to avoid circular import at module load.
        from windows.connection_window import ConnectionWindow
        self._connection_window = ConnectionWindow(on_back=self._show_again)
        self._connection_window.show()
        self.hide()

    def _show_again(self):
        self.show()

    def _tutorial(self, kind):
        from windows.tutorial_window import TutorialWindow
        if not hasattr(self, "_tuts"):
            self._tuts = []
        t = TutorialWindow(kind, self)
        self._tuts.append(t)
        t.show(); t.raise_()

    def _change_theme(self, idx):
        from PyQt5.QtWidgets import QApplication
        from core.theme import set_mode
        set_mode("dark" if idx == 0 else "light", QApplication.instance())
        self._build_ui()   # rebuild so this page repaints in the new theme immediately
