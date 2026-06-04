# QRes Platform — build & run

## Run from source (development)
```bat
cd qres_platform
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
:: copy your drivers/fitter next to main.py:
::   circuit.py  MercuryITC.py  Proteox.py  (+ decs_visa.py, decs_visa_settings.py for Proteox)
python main.py
```

## Build a Windows .exe (PyInstaller)
From the project root, in the same environment that runs the app:
```bat
pip install pyinstaller
pyinstaller qres_platform.spec
```
The app appears at `dist\QResPlatform\QResPlatform.exe` (a one-folder build).
Zip the whole `dist\QResPlatform` folder to move it to another machine.

### Why one-folder (not one-file)
The Proteox `Proteox.py` driver launches the DECS-VISA bridge as a **subprocess**
(`decs_visa.py`). In a one-folder build those files sit beside the exe where the
driver can find/launch them; in a one-file build they're unpacked to a temporary
directory each launch, which complicates that subprocess path. One-folder also
lets you drop in an updated `circuit.py`/driver without rebuilding.

### Before building, place beside `main.py`
- `circuit.py` — the Probst notch-port fitter (required for quality/analysis).
- `MercuryITC.py` — Teslatron driver (if used).
- `Proteox.py` + the DECS-VISA files (`decs_visa.py`, `decs_visa_settings.py`, …)
  if used. Make sure the interpreter path the driver uses to spawn `decs_visa.py`
  is valid on the target PC.

### First-run notes
- Windows SmartScreen / antivirus may flag a freshly built unsigned exe; allow it
  or code-sign for distribution.
- Settings are stored in `%APPDATA%\QResPlatform\settings.json` (entered values
  become defaults across runs).
- If an instrument won't connect, edit its address in the connection window and
  optionally save it as the new default.

## Troubleshooting the frozen build
- **`ModuleNotFoundError: circuit` (or a driver)** — the file wasn't beside
  `main.py` at build time; add it and rebuild, or add it to `hiddenimports` in
  `qres_platform.spec`.
- **qcodes/pyqtgraph data missing** — the spec uses `collect_all`; if a submodule
  is still missing, add it to `hiddenimports`.
- **HPD segment sweep errors on the PNA** — segment SCPI spelling varies by
  firmware; adjust `core/pna_segment.py` (the app falls back to a linear sweep and
  logs it, so this never aborts a run).
