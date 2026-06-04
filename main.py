"""
QRes Platform — entry point.

Launches the welcome window (fridge selection), which leads into the connection
window and then the measurement platform.

Place these driver files alongside main.py (or anywhere on PYTHONPATH):
    Proteox.py        (oiDECS driver)        — for Proteox
    MercuryITC.py     (MercuryiTC driver)    — for Teslatron
    circuit.py        (Probst fitter)        — for analysis
"""

import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication

from core.theme import apply_theme
from windows.welcome_window import WelcomeWindow


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    apply_theme(app)
    win = WelcomeWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
