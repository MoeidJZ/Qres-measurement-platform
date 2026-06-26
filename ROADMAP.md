# QRes Platform — build roadmap

Refactor/extension of the existing `ProteoxS_MicrowaveMeasurements` app
(PyQt5 + QCoDeS) into the full step-by-step resonator platform.

Run: `pip install -r requirements.txt`, drop `Proteox.py`, `MercuryITC.py`,
`circuit.py` next to `main.py`, then `python main.py`.

## Phase 1 — DONE
- `core/settings.py` — persistent JSON defaults (`settings.remember(...)` after
  every Run makes the entered values the new defaults).
- `core/fridge.py` — common abstraction over Proteox (`oiDECS`), Teslatron
  (`MercuryiTC`) and a Manual/no-fridge backend. Reference-temperature selection;
  **verified** `set_target_temperature()` (fixes the notebook target mismatch);
  abort/timeout-aware `wait_until_stable()`; Proteox `confirm_callback` for the
  DECS popup.
- `core/instrument_manager.py` — owns fridge + PNA + station + db; `busy` flag
  with listeners; `update_address(which, addr, make_permanent)`.

## Phase 2 — DONE
- `core/theme.py`, `core/connect_workers.py`, `windows/dialogs.py`,
  `windows/welcome_window.py`, `windows/connection_window.py`,
  `windows/main_window.py`, `main.py`.
- Welcome → fridge select → connection (Proteox confirm dialog, Teslatron
  address-edit-retry, optional PNA with ask-to-persist, DB + sample) → platform.

## Phase 3 — DONE
- `core/control_workers.py` — threaded channel + PNA I/O workers.
- `windows/instrument_control_dock.py` — dockable + detachable fridge control,
  manual Refresh, per-channel Apply; live read/write at all times.
- `windows/pna_window.py` — full PNA parameter control; persists as defaults;
  locked while busy; greyed if PNA absent.
- `windows/main_window.py` — toolbar to (re)open dock + PNA window; workflow
  buttons grey while busy.

## Phase 4 — DONE
- `core/measure_workers.py` — `WidebandWorker`: optional reference-temperature
  wait (hourly poll, +100% band, Run-now / Stop overrides), PNA configure +
  sweep, live magnitude emit, save magnitude+phase to db, safe power-down.
- `windows/widgets/spectrum_plot.py` — embedded pyqtgraph |S21| plot: hover
  crosshair with exact-frequency readout; click toggles resonance pick markers.
- `windows/wideband_window.py` — params seed/persist; ref-temp controls (pick
  reference, target+unit, poll interval); Run / Wait&run / Run-now / Stop;
  embedded plot; Confirm picks → emits frequencies for the span picker.
- `windows/main_window.py` — wideband step enabled and wired.

## Phase 5 — DONE
- `windows/widgets/span_plot.py` — zoom view with a draggable `LinearRegionItem`
  (start/stop edges) + hover frequency readout; slices existing wideband data.
- `windows/span_picker_window.py` — picks become Res 1…N (lowest→highest); per
  resonator: adjustable view window + Re-show, draggable span, and Confirm /
  Ignore / Discard; live summary; "Confirm all & continue" persists the curated
  list (survives sessions) and emits the confirmed set for the quality step.
- `windows/main_window.py` — wideband picksConfirmed → span picker; step 2 wired
  and reopenable.

## Phase 6 — DONE
- `core/fitting.py` — `fit_notch()` wrapper over the project's notch-port
  circlefit (auto-fit; optional fit-window crop + smoothing); returns
  fr/Qi/Qc/Ql + simulated overlay. Verified against circuit.py on synthetic
  data (Qi/Qc/Ql within 0.1%).
- `core/measure_workers.py` — `QualityWorker`: single high-power, 1-average sweep
  per confirmed resonator over its span; saves each; emits data for inline fit.
- `windows/widgets/fit_plot.py` — raw |S21| + fit overlay + draggable crop
  region + hover.
- `windows/quality_window.py` — runs the sweep, auto-fits each resonator, lets
  the user drag the crop region and Re-fit to correct bad fits, tick good ones,
  and continue to the power-dependent step with the selected set.
- `windows/main_window.py` — span picker → quality wired; step 3 reopenable.

## Phase 7a — DONE (power-dependent)
- `core/pna_segment.py` — `build_hpd_segments()` (auto segment count; ~54% of
  points land within ±Δfr/2) + SCPI to program segment sweep, read the
  non-uniform stimulus, and restore linear sweep.
- `core/fitting.py` — now returns `Ql` and `theta0` (carried from Phase 6).
- `core/measure_workers.py` — `PowerWorker`: SPD (linear, low→high, one
  multi-power run) and HPD (segment, high→low, per-power run, table rebuilt from
  the running fit seeded by Phase 6; Qi-jump >reject× reuses previous Ql/θ0;
  linear fallback if segment SCPI fails).
- `windows/power_window.py` — power vector, editable averaging/IFBW table with
  rule-interpolation, SPD/HPD toggle, Qi-reject factor, live Qi-vs-power plot,
  continue → temperature.
- `windows/main_window.py` — quality → power wired; step 4 enabled.

## Phase 7b — TODO (temperature-dependent)
- Verified-target-temperature sweep using `fridge.set_target_temperature()` +
  `wait_until_stable()`; per-temperature power/averaging/IFBW; uses the same
  SPD/HPD machinery.

## Phase 7b — DONE (temperature- & power-dependent)
- `core/measure_workers.py` — per-resonator SPD/HPD routines refactored into
  shared functions; `TemperatureWorker`: for each target T it sets the setpoint
  with **read-back verification** (fixes the setpoint/target mismatch), waits for
  stability (abort/timeout aware), then runs a full SPD/HPD power sweep. Each
  temperature gets its own run id(s); the temperature is encoded in run names.
- `windows/temperature_window.py` — temperature vector (mK/K, reversible),
  stability criterion, the same power-sweep + averaging-rule controls (avg 1–15,
  IFbw 1–1000 Hz), resonator selection, live Qi-vs-power per (resonator·T).
- `windows/power_window.py` — averaging clamped to 1–15, IF bw to 1–1000 Hz;
  HPD point count adjustable via the Points field (segments auto-derive).
- `windows/main_window.py` — power → temperature wired; step 5 enabled.

## Phase 8 — DONE (analysis + packaging)
- `core/analysis_io.py` — list runs (concurrency-safe sqlite read of the live or
  any .db), load SPD multi-power / HPD / quality / generic layouts, parse
  Res/T/P from run names, and export traces as `<base>_<Res>_<T>_<P>.csv` +
  append fit params to `<base>_fitting_parameters.csv`.
- `windows/analysis_window.py` — Open .db (prompts per-db export folder + base
  name), browse/refresh runs, per-power circle fit with adjustable crop + Re-fit
  + smoothing, fr/Qi/Qc/Ql readout, export this fit or all powers. Usable while a
  measurement writes to the same db.
- `windows/tutorial_window.py` — Measurement and Analysis how-to guides
  (welcome page + Help toolbar).
- `qres_platform.spec`, `BUILD.md` — PyInstaller one-folder build + run guide.
- `windows/main_window.py` — analysis step enabled (stays usable while busy);
  Help toolbar tutorials.

## All phases complete ✅

## Packaging to .exe (Phase 8 preview)
`pip install pyinstaller`, then from the project folder:
`pyinstaller --noconfirm --windowed --name QResPlatform --add-data "circuit.py;." main.py`
(use `:` instead of `;` on Linux/Mac). The DECS-VISA subprocess path in
`Proteox.py` must point at a real interpreter on the target machine.
