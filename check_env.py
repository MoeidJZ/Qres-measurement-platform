"""
check_env.py — run this first to see exactly what's missing.

    python check_env.py

It checks your Python version, the required packages, and the driver/fitter
files that must sit next to main.py. It prints a clear report and a pip command
for anything missing. It does NOT need any third-party package itself.
"""

import sys
import os
import importlib.util

REQUIRED = [
    # (import name, pip name, why)
    ("PyQt5", "PyQt5", "GUI toolkit"),
    ("pyqtgraph", "pyqtgraph", "interactive plots"),
    ("numpy", "numpy", "arrays"),
    ("scipy", "scipy", "circle-fit math"),
    ("qcodes", "qcodes", "instruments + database"),
    ("qcodes_contrib_drivers", "qcodes_contrib_drivers", "Proteox / DECS-VISA"),
    ("ipywidgets", "ipywidgets", "imported by MercuryITC.py"),
    ("IPython", "ipython", "imported by MercuryITC.py"),
]

# Files that must be next to main.py (this script's folder).
DRIVER_FILES = [
    ("circuit.py", "Probst circle-fit (full) — required for quality & analysis"),
    ("circlefit.py", "Probst circle-fit core — required by circuit.py"),
    ("calibration.py", "Probst calibration — required by circuit.py"),
    ("utilities.py", "Probst utilities (photons, plotting) — required by circuit.py"),
    ("Proteox.py", "Proteox driver — needed only for Proteox"),
    ("MercuryITC.py", "Teslatron driver — needed only for Teslatron"),
]

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ok = True
    print("=" * 64)
    print("QRes Platform — environment check")
    print("=" * 64)

    # Python version
    v = sys.version_info
    pyok = (3, 9) <= (v.major, v.minor) <= (3, 12)
    print(f"\nPython {v.major}.{v.minor}.{v.micro}  "
          f"[{'OK' if pyok else 'WARNING: 3.9–3.12 recommended'}]")
    if not pyok:
        ok = False

    # Packages
    print("\nPackages:")
    missing = []
    for imp, pip_name, why in REQUIRED:
        found = importlib.util.find_spec(imp) is not None
        print(f"  [{'✓' if found else '✗'}] {imp:<24} — {why}")
        if not found:
            missing.append(pip_name)
            ok = False

    # Driver files
    print("\nDriver / fitter files (must sit next to main.py):")
    for fn, why in DRIVER_FILES:
        present = os.path.exists(os.path.join(HERE, fn))
        print(f"  [{'✓' if present else '✗'}] {fn:<16} — {why}")
        if not present and fn in ("circuit.py", "circlefit.py", "calibration.py", "utilities.py"):
            ok = False  # the full Probst fitter is always required

    print("\n" + "=" * 64)
    if missing:
        print("Install the missing packages with:")
        print("   pip install " + " ".join(sorted(set(missing))))
    if ok:
        print("All good — run:  python main.py")
    else:
        print("Resolve the items marked ✗ above, then re-run this check.")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
