# qres_platform.spec — PyInstaller build spec for QRes Platform
#
# Build (from the project root, with the venv that runs the app activated):
#     pip install pyinstaller
#     pyinstaller qres_platform.spec
#
# Output: dist/QResPlatform/QResPlatform.exe  (one-folder build — recommended
# over one-file so the Proteox DECS-VISA subprocess and the driver .py files sit
# beside the exe and are easy to inspect/replace).
#
# Place these next to main.py before building so they are bundled and importable:
#     circuit.py        (Probst notch-port fitter)
#     MercuryITC.py     (Teslatron driver)        — if you use Teslatron
#     Proteox.py        (oiDECS driver)           — if you use Proteox
#     decs_visa.py, decs_visa_settings.py, and the rest of the DECS-VISA package
#                                                  — if you use Proteox

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Heavy third-party packages that need their data/submodules collected.
for pkg in ("qcodes", "qcodes_contrib_drivers", "pyqtgraph", "scipy"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

# Driver / fitter modules are imported lazily by name, so PyInstaller can't see
# them by static analysis — bundle them explicitly as data and hidden imports.
for mod in ("circuit", "Proteox", "MercuryITC", "decs_visa", "decs_visa_settings"):
    fn = mod + ".py"
    if os.path.exists(fn):
        datas.append((fn, "."))
        hiddenimports.append(mod)

hiddenimports += collect_submodules("qcodes")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="QResPlatform",
    console=False,          # windowed app (no console)
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="QResPlatform",
)
