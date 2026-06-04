# QRes Platform — quick start (run from source)

This zip contains everything needed to launch the app, including the driver and
fitter files. You only need a Python environment with the packages installed.

## 1. Get a Python environment
Python **3.9–3.12**. A fresh virtual environment is recommended:

**Windows (PowerShell / cmd):**
```bat
cd qres_platform
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
cd qres_platform
python3 -m venv .venv
source .venv/bin/activate
```

## 2. Install the packages
```bash
pip install -r requirements.txt
```

## 3. Check everything is ready (optional but recommended)
```bash
python check_env.py
```
It lists each package and driver file as ✓ or ✗ and prints the exact `pip`
command for anything missing.

## 4. Run
```bash
python main.py
```
The welcome window opens → pick your fridge → connect → measure / analyze.

---

## What's in the folder
```
qres_platform/
  main.py                 ← run this
  check_env.py            ← preflight checker
  requirements.txt
  circuit.py              ← Probst circle-fit (bundled)
  Proteox.py              ← Oxford Proteox driver (bundled)
  MercuryITC.py           ← Teslatron driver (bundled)
  core/                   ← settings, fridge abstraction, workers, fitting, …
  windows/                ← all GUI windows + plot widgets
  ROADMAP.md  BUILD.md  qres_platform.spec   ← docs + .exe packaging
```

## Notes that matter on the measurement PC
- **Proteox**: `Proteox.py` launches the DECS-VISA bridge as a subprocess. Open
  `Proteox.py` and set `decs_visa_path` (near the top) to the real
  `decs_visa.py` on that machine (inside the installed `qcodes_contrib_drivers`).
  The DECS error-popup step is handled in-app: you'll get a Confirm dialog.
- **Teslatron**: set the VISA address in the connection window (editable, with an
  option to save it as the default).
- **No hardware?** Pick **Other / Manual** on the welcome screen to explore the
  UI and the analysis window without instruments.
- Settings (your last-used values become defaults) live in
  `%APPDATA%\QResPlatform\settings.json` (Windows) or `~/.config/QResPlatform/`.

## If a package won't install
- `PyQt5` occasionally needs a recent `pip` (`python -m pip install -U pip`).
- On Apple Silicon, install via the system Python or conda if `PyQt5` wheels are
  unavailable for your Python version.
- `qcodes_contrib_drivers` brings the Proteox/DECS tooling; if you only use
  Teslatron you still need it installed because the Proteox driver is imported
  lazily (it won't run unless you select Proteox).
```
