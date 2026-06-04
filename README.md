# QRes Platform

**A guided desktop application for measuring and analysing superconducting microwave resonators in dilution / variable-temperature cryostats.**

QRes Platform combines instrument control, a step-by-step measurement workflow, and a full circle-fit analysis suite into a single PyQt5 application. It drives a Keysight PNA-X vector network analyser together with an Oxford cryostat, walks you from a broad wideband scan all the way to temperature- and power-dependent quality-factor measurements, and lets you fit and extract physics (internal quality factor, coupling, photon number) from the data it collects — or from any previously saved run.

---

## Table of contents

- [Highlights](#highlights)
- [Tech stack](#tech-stack)
- [Supported hardware](#supported-hardware)
- [The guided measurement workflow](#the-guided-measurement-workflow)
- [Analysis suite](#analysis-suite)
- [Physics & fitting](#physics--fitting)
- [Data, naming & exports](#data-naming--exports)
- [Project structure](#project-structure)
- [Installation](#installation)
- [Running the app](#running-the-app)
- [Configuration & settings](#configuration--settings)
- [Tips & troubleshooting](#tips--troubleshooting)
- [Acknowledgements](#acknowledgements)
- [License](#license)

---

## Highlights

- **End-to-end guided workflow** — wideband scan → resonance picking → span selection → quality assessment → power-dependent → temperature + power-dependent, each as its own focused window with sensible gating between steps.
- **Real instrument control** — Keysight PNA-X (N5245A / N52xx family) over QCoDeS, plus Oxford **Proteox** and **Teslatron** cryostats, or a **Manual** fridge mode for offline / dry runs.
- **Interruptible sweeps** — every long sweep can be stopped cleanly mid-acquisition without leaving the instrument in a bad state.
- **Authoritative circle-fit analysis** — uses the full Probst `resonator_tools` notch-port fitter for diameter-corrected internal/coupling quality factors with error estimates and χ².
- **Live feedback** — quality-factor-versus-power plots update after every point, complete with error bars; circle / magnitude / phase panels update in real time while you drag a fit region.
- **Persistent, self-describing data** — all sweeps are written to a QCoDeS SQLite database with descriptive run names (resonator, temperature, power, frequency band) and one-click CSV export.
- **Light & dark themes**, persistent user settings, and built-in tutorials for both the measurement and analysis sides.

---

## Tech stack

| Component | Role |
|---|---|
| **PyQt5** | Desktop GUI framework |
| **pyqtgraph** | Fast, interactive plotting (spectra, fit panels, live Qi curves) |
| **QCoDeS** | Instrument abstraction, station, and SQLite measurement database |
| **qcodes_contrib_drivers** | Additional instrument drivers |
| **NumPy / SciPy** | Numerics and signal processing |
| **matplotlib** | Required by the bundled circle-fit library |
| **resonator_tools** (Probst circle fit) | Notch-port circle-fit engine (bundled) |

Tested with **Python 3.9–3.12**.

---

## Supported hardware

### Cryostats (fridges)

- **Oxford Proteox** dilution refrigerator (via the `oiDECS` driver). Mixing-chamber control temperature is used for run labelling.
- **Oxford Teslatron** (via `MercuryiTC`). Sample / VTI loop temperature is used for control and labelling.
- **Manual mode** — no fridge hardware required; you set/record the temperature yourself. Ideal for bench testing, replaying data, or working without a cryostat.

Each backend exposes a common interface: read temperature, set a target temperature with read-back verification, and wait until the temperature is stable within a tolerance before measuring.

### Vector network analyser

- **Keysight PNA-X**, e.g. **N5245A**, through the QCoDeS `KeysightPNABase` / `N52xx` driver.
- The platform configures the instrument for efficient **binary** trace transfer (`FORM REAL,32`) and reads magnitude/phase as binary blocks.
- **Driver-native, abortable triggering**: sweeps are triggered through the driver's own sweep-mode parameters and polled for completion so a *Stop* request is honoured within a fraction of a second — without desynchronising the VISA session.
- **Firmware-quirk handling**: tolerant value mapping for parameters such as `averages_enabled` that some firmware reports as `+0` / `+1`.

> The application connects to instruments through a central instrument manager that owns the fridge, the PNA, the QCoDeS station, and the database, and broadcasts a "busy" state so the UI stays consistent during long operations.

---

## The guided measurement workflow

The main window presents the workflow as numbered steps. You can run it straight through, or jump into any step using data loaded from a previous database run.

### 1. Connect instruments

Choose your fridge type and PNA address, connect, and the platform builds a QCoDeS station and opens (or creates) the measurement database. A dockable **Instrument Control** panel and a **PNA Parameters** window give you direct, low-level access whenever you need it.

### 2. Wideband scan

Run a broad frequency sweep to locate resonances across a wide span.

- Click directly on the spectrum to **pick resonances**.
- The sweep is **interruptible** — press *Stop* at any time.
- Don't have time to re-scan? **Load from database** to pull a previous wideband trace and pick resonances from it without touching the instrument.

### 3. Span selection

For each picked resonance, set the frequency window you want to study.

- Drag a shaded region around the resonance; the **centre automatically realigns** to the middle of the region you choose.
- Type an exact span (e.g. `500 kHz` or `1 MHz`) and press **Re-view** to recentre and zoom — the span you type is the span you get.

### 4. Quality assessment

Measure and circle-fit each resonator individually, with a clear per-resonator state machine (`unmeasured → measured → fitted → confirmed / ignored`).

- A **three-panel stacked view** shows the complex-plane circle (top, aspect-locked), magnitude (centre), and phase (bottom), with a synchronised draggable fit region.
- Read out `fr`, `Qi`, `Ql`, `Qc` at a glance.
- Per-resonator actions: **Run fit**, **Confirm**, **Ignore**, **Delete**, and **Re-measure**.
  - **Re-measure** sends you to a span panel for *that resonator only* — choose a new span around its latest trace, confirm, and it re-sweeps just that resonator while leaving the others untouched.
  - **Delete** frees its resonator number for reuse; **Ignore** reserves and skips it.
- The platform **auto-selects the first measured resonator** so you can analyse incrementally while the rest of the sweep continues.
- **Continue** to the power step unlocks only once every resonator is confirmed or ignored (with at least one confirmed).
- You can also **Load from database** to bring in previously saved quality runs.

### 5. Power-dependent measurement

Sweep each confirmed resonator over a range of powers and watch the internal quality factor evolve. Two acquisition strategies are available:

- **SPD (Standard Power Dependence)** — powers low → high, all stored in a **single run** sharing one frequency axis.
- **HPD (High-resolution Power Dependence)** — powers high → low using an **adaptive narrow linear sweep**. At each power the window auto-shrinks to a few linewidths around the resonance (clamped between 5 % and 100 % of the original span), concentrating your points exactly where they matter as the resonance sharpens at low power. The resonance is re-centred from each fit; a Qi jump beyond a configurable factor reuses the previous seed. All powers are stored in a **single run**.

Both modes provide:

- An **editable per-power schedule** of averaging count and IF bandwidth.
- Adjustable point count and a **Qi-reject factor**.
- A **live Qi-vs-power plot with error bars** that updates after every point.
- **Load resonator(s) from the database** to seed a power sweep from an existing quality run.

### 6. Temperature + power-dependent measurement

Repeat the power-dependent measurement at a series of set-point temperatures.

- Define a temperature vector in **mK or K**, with a stability wait before each point.
- Each (resonator, temperature) combination is saved as its own run, labelled with the target temperature.
- Same SPD / HPD controls and the same **live Qi-vs-power error-bar plot**.

> **Robust temperature labelling:** if the cryostat temperature can't be read, the measurement still runs — it's labelled with a `?mK` placeholder and a warning is logged, rather than failing. (This is true across wideband, quality, power, and temperature runs.)

---

## Analysis suite

Open the analysis window on **any** `.db` file — data taken with this platform or compatible runs from elsewhere — and fit it interactively.

- Pick a database and a run; choose an export folder and base name.
- **Real-time circle fit**: drag the fit region on the magnitude/phase plots and the complex-plane fit updates live (throttled for smoothness).
- **Per-power navigation** for multi-power runs, including HPD runs whose frequency window differs per power.
- **Attenuation / photon calibration**: enter the line attenuation (negative dB, *added* to the VNA power to give on-chip power) and read the intra-resonator photon number for the current fit.
- **Keyboard shortcuts** for fast review: next/previous power, next/previous run, and save.
- **Exports**:
  - Export the current fit's trace, or every power in the run, as CSV (frequency, magnitude, phase, and the simulated fit).
  - Append fitted metrics — including phase `phi_rad` and photon number — to a cumulative `*_fitting_parameters.csv`.
  - Filenames are sanitised so they are always valid on Windows.

---

## Physics & fitting

### Circle fit (notch port)

QRes Platform uses the full **Probst `resonator_tools` circle-fit** library (bundled with the project) in its notch-port configuration. For each resonance it reports:

- **`fr`** — resonance frequency
- **`Qi`** (diameter-corrected internal quality factor, `Qi_dia_corr`)
- **`Qc`** — coupling quality factor (`absQc`, with diameter-corrected `Qc_dia_corr` available)
- **`Ql`** — loaded quality factor
- **`phi0`, `theta0`** — impedance-mismatch / rotation angles
- **Uncertainties** on `Qi`, `Qc`, `Ql`, `fr`, plus the fit **χ²**

The complex-plane panel plots the **raw** measured S21 together with the simulated circle, so you can see exactly how well the model tracks the data.

### Photon number

On-chip power is computed from the VNA power and a (negative) line attenuation. The intra-resonator photon number is obtained directly from the fitted port:

```
k_c = 2π·fr / Qc      (coupling rate)
k_i = 2π·fr / Qi      (internal loss rate)
n̄   = 4·k_c / ( 2π·ħ·fr·(k_c + k_i)² ) · P_chip
```

following the standard expression for a notch-coupled resonator (see, e.g., Baity *et al.*, *Phys. Rev. Research* **6**, 013329, 2024).

---

## Data, naming & exports

- All sweeps are saved to a **QCoDeS SQLite database**, so runs are self-contained and re-loadable.
- **Descriptive run names** encode everything you need to find a measurement later, for example:

  ```
  <sample>_Res3_Quality_15.0mK_5.230000to5.232000GHz_-30dBm
  <sample>_Res3_PowerDep_SPD_15.0mK_5.230000to5.232000GHz
  <sample>_Res3_PowerDep_HPD_15.0mK_5.230000to5.232000GHz
  ```

  Names include the resonator index, the run type (Wide / Quality / SPD / HPD), the temperature label, and the start/stop frequency band.
- **Power-dependent runs are consolidated into a single run id** per resonator (per temperature), with each power's trace stored inside that one run — including HPD runs where each power uses its own frequency window.
- **CSV exports** contain frequency, magnitude, phase, and the simulated fit; metrics exports include quality factors with errors, `phi_rad`, and photon number.

---

## Project structure

```
qres_platform/
├── main.py                     # Application entry point
├── check_env.py                # Pre-flight environment / dependency check
├── requirements.txt
│
├── circuit.py                  # Probst resonator_tools circle-fit library
├── circlefit.py                #   (bundled fitter — required)
├── calibration.py
├── utilities.py
│
├── Proteox.py                  # Oxford Proteox cryostat driver (oiDECS)
├── MercuryITC.py               # Oxford Teslatron driver (MercuryiTC)
│
├── core/
│   ├── settings.py             # Persistent JSON settings
│   ├── theme.py                # Light/dark themes & pyqtgraph styling
│   ├── fridge.py               # Fridge backends (Proteox / Teslatron / Manual)
│   ├── instrument_manager.py   # Central owner of fridge + PNA + station + DB
│   ├── connect_workers.py      # Threaded connection workers
│   ├── control_workers.py      # Threaded instrument-control workers
│   ├── measure_workers.py      # Wideband / Quality / Power / Temperature workers
│   ├── pna_segment.py          # Sweep helpers
│   ├── fitting.py              # Circle-fit wrapper, photon number, formatting
│   └── analysis_io.py          # Run loading, trace access, CSV export
│
└── windows/
    ├── welcome_window.py
    ├── connection_window.py
    ├── main_window.py
    ├── instrument_control_dock.py
    ├── pna_window.py
    ├── wideband_window.py
    ├── span_picker_window.py
    ├── quality_window.py
    ├── power_window.py
    ├── temperature_window.py
    ├── analysis_window.py
    ├── tutorial_window.py
    ├── dialogs.py
    └── widgets/
        ├── spectrum_plot.py
        ├── span_plot.py
        ├── fit_plot.py
        └── resonator_fit_view.py
```

---

## Installation

1. **Clone the repository**

   ```bash
   git clone <your-repo-url>
   cd qres_platform
   ```

2. **Create a virtual environment** (recommended)

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

   This includes PyQt5, pyqtgraph, qcodes, qcodes_contrib_drivers, numpy, scipy, and matplotlib.

4. **Check your environment** (verifies that the bundled fitter files and dependencies are present)

   ```bash
   python check_env.py
   ```

> **Hardware note:** controlling a real PNA-X requires a working VISA backend (e.g. Keysight IO Libraries / NI-VISA) on the measurement PC. For the Proteox backend, the local `decs_visa` path must be configured on that PC. You can explore the entire UI and the analysis suite without any instruments by selecting **Manual** fridge mode and loading existing data.

---

## Running the app

```bash
python main.py
```

The welcome screen lets you pick a fridge type and start the guided workflow, or jump straight to the analysis suite. Built-in tutorials are available from the toolbar for both the measurement and analysis sides.

---

## Configuration & settings

- **Persistent settings** are stored as JSON (on Windows, under `%APPDATA%\QResPlatform\settings.json`) and remember things like your last addresses, theme, and window choices.
- **Theme** — switch between **Light** (default) and **Dark** at any time from the toolbar; all plots restyle to match.
- **PNA Parameters** and the **Instrument Control** dock give direct access to start/stop, points, IF bandwidth, power, averaging, and output state.

---

## Tips & troubleshooting

- **A sweep won't stop instantly** — *Stop* is honoured at the end of the current poll cycle (a fraction of a second); the instrument is then placed in a safe state (output reduced, sweep held).
- **Temperature shows `?mK`** — the fridge temperature couldn't be read; the run still completes and is labelled with the placeholder. Check the fridge connection if you expected a real value.
- **HPD windows look too narrow** at very low power — the adaptive window scales with the fitted linewidth; if your resonance frequency drifts more than expected, widen the span at the span-selection step.
- **`check_env.py` reports a missing fitter file** — make sure `circuit.py`, `circlefit.py`, `calibration.py`, and `utilities.py` are present in the project root.
- **No PyQt5 / VISA on a headless machine** — use **Manual** mode and the analysis suite to work with data offline.

---

## Acknowledgements

- Circle-fit analysis is built on the **`resonator_tools`** library by S. Probst *et al.* (notch-port fitting and diameter correction).
- Instrument control is built on **[QCoDeS](https://qcodes.github.io/Qcodes/)** and **qcodes_contrib_drivers**.
- Photon-number conventions follow standard notch-resonator treatments (e.g. Baity *et al.*, *Phys. Rev. Research* **6**, 013329, 2024).

## References
 
1. S. Probst, F. B. Song, P. A. Bushev, A. V. Ustinov, and M. Weides, "Efficient and robust analysis of complex scattering data under noise in microwave resonators," *Review of Scientific Instruments* **86**, 024706 (2015). https://doi.org/10.1063/1.4907935
2. M. S. Khalil, M. J. A. Stoutimore, F. C. Wellstood, and K. D. Osborn, "An analysis method for asymmetric resonator transmission applied to superconducting devices," *Journal of Applied Physics* **111**, 054510 (2012). https://doi.org/10.1063/1.3692073
3. P. G. Baity, C. Maclean, V. Seferai, J. Bronstein, Y. Shu, T. Hemakumara, and M. Weides, "Circle fit optimization for resonator quality factor measurements: Point redistribution for maximal accuracy," *Physical Review Research* **6**, 013329 (2024). https://doi.org/10.1103/PhysRevResearch.6.013329
---
