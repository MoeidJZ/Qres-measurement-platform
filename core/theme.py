"""
core/theme.py
=============
App styling with a dark and a light theme. The active theme is read from
settings ('app.theme'); the welcome page lets the user switch it. Helpers are
provided for pyqtgraph so plots match the chosen theme (background, axis colour,
and the data/fit/region colours used across the plot widgets).
"""

from __future__ import annotations

from core.settings import settings

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "win_bg": "#1e1e2e", "panel": "#313244", "border": "#45475a",
        "text": "#cdd6f4", "subtext": "#a6adc8", "muted": "#6c7086", "accent": "#89b4fa",
        "accent_text": "#1e1e2e", "danger": "#f38ba8", "success": "#a6e3a1",
        "warn": "#f9e2af", "status_bg": "#181825",
        # plot colours
        "plot_bg": "#1e1e2e", "axis": "#cdd6f4", "grid": 0.25,
        "data": "#89b4fa", "fit": "#f38ba8", "region": (137, 180, 250, 50),
        "center": "#a6e3a1",
    },
    "light": {
        "win_bg": "#f5f5f7", "panel": "#ffffff", "border": "#c8c9cf",
        "text": "#1c1c22", "subtext": "#44474f", "muted": "#7a7d87", "accent": "#1f6feb",
        "accent_text": "#ffffff", "danger": "#d62728", "success": "#1a7f37",
        "warn": "#b8860b", "status_bg": "#e7e7ea",
        "plot_bg": "#ffffff", "axis": "#1c1c22", "grid": 0.25,
        "data": "#1f77b4", "fit": "#d62728", "region": (31, 119, 180, 45),
        "center": "#1a7f37",
    },
}


def current_mode() -> str:
    m = settings.get("app.theme", "light")
    return m if m in THEMES else "dark"


def colors() -> dict:
    return THEMES[current_mode()]


def hx(token: str) -> str:
    """Hex string for a semantic colour token in the active theme."""
    c = THEMES[current_mode()]
    return c.get(token, c["text"])


# ---------------------------------------------------------------------------
# Qt stylesheet
# ---------------------------------------------------------------------------

def _stylesheet(c: dict) -> str:
    return f"""
QMainWindow, QDialog, QWidget {{ background-color: {c['win_bg']}; color: {c['text']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 10px;
    padding-top: 8px; font-weight: bold; color: {c['accent']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 6px; }}
QLabel {{ color: {c['text']}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QTextBrowser {{
    background-color: {c['panel']}; border: 1px solid {c['border']}; border-radius: 4px;
    padding: 4px 8px; color: {c['text']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c['accent']}; }}
QPushButton {{ background-color: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']};
    border-radius: 6px; padding: 6px 16px; min-width: 80px; }}
QPushButton:hover {{ border: 1px solid {c['accent']}; }}
QPushButton:disabled {{ color: {c['muted']}; }}
QPushButton#primary {{ background-color: {c['accent']}; color: {c['accent_text']}; font-weight: bold; border: none; }}
QPushButton#danger {{ background-color: {c['danger']}; color: {c['accent_text']}; font-weight: bold; border: none; }}
QPushButton#success {{ background-color: {c['success']}; color: {c['accent_text']}; font-weight: bold; border: none; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 4px; }}
QTabBar::tab {{ background-color: {c['panel']}; color: {c['muted']}; padding: 8px 18px;
    margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }}
QTabBar::tab:selected {{ background-color: {c['border']}; color: {c['text']}; }}
QStatusBar {{ background-color: {c['status_bg']}; color: {c['muted']}; }}
QCheckBox {{ color: {c['text']}; spacing: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 3px;
    border: 1px solid {c['border']}; background: {c['panel']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QTableWidget {{ background-color: {c['panel']}; gridline-color: {c['border']};
    border: 1px solid {c['border']}; border-radius: 4px; }}
QHeaderView::section {{ background-color: {c['border']}; color: {c['text']}; padding: 6px 8px; border: none; }}
QListWidget {{ background-color: {c['panel']}; border: 1px solid {c['border']}; border-radius: 4px; }}
QProgressBar {{ border: 1px solid {c['border']}; border-radius: 4px; text-align: center;
    background-color: {c['panel']}; color: {c['text']}; }}
QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 3px; }}
QSplitter::handle {{ background-color: {c['border']}; }}
QToolBar {{ background: {c['status_bg']}; border: none; }}
QDockWidget::title {{ background: {c['panel']}; padding: 6px; }}
"""


def apply_theme(app) -> None:
    """Apply the active theme to the QApplication and pyqtgraph."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.setApplicationName("QRes Platform")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    app.setStyleSheet(_stylesheet(colors()))
    configure_pyqtgraph()


def set_mode(mode: str, app=None) -> None:
    """Switch theme and (optionally) re-apply to a running app immediately."""
    settings.set("app.theme", mode if mode in THEMES else "dark")
    if app is not None:
        apply_theme(app)


# ---------------------------------------------------------------------------
# pyqtgraph helpers
# ---------------------------------------------------------------------------

def configure_pyqtgraph() -> None:
    try:
        import pyqtgraph as pg
        c = colors()
        pg.setConfigOptions(antialias=True, background=c["plot_bg"], foreground=c["axis"])
    except Exception:
        pass


def style_plot(plot_widget) -> None:
    """Apply theme background + axis colours to a pyqtgraph PlotWidget."""
    c = colors()
    try:
        plot_widget.setBackground(c["plot_bg"])
        for ax in ("left", "bottom", "right", "top"):
            a = plot_widget.getAxis(ax)
            a.setPen(c["axis"])
            try:
                a.setTextPen(c["axis"])
            except Exception:
                pass
    except Exception:
        pass


def pen(kind: str):
    import pyqtgraph as pg
    c = colors()
    return pg.mkPen(c.get(kind, c["data"]), width=2)
