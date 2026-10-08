"""
core/measure_workers.py
=======================
Measurement worker threads. Phase 4 adds the wideband sweep with the optional
reference-temperature wait.

WidebandWorker behaviour
------------------------
* If ``ref_wait`` is set, poll the chosen reference temperature once per
  ``poll_interval_s`` (default 3600 s). Start the sweep when the measured
  temperature is at or below ``ref_target_k * (1 + ref_margin)`` — i.e. the
  "+100% band": for a 50 mK target it fires anywhere from base up to 100 mK.
* The user is never trapped by the wait: ``run_now()`` skips straight to the
  sweep, and ``abort()`` stops cleanly and hands manual control back.
* The sweep configures the PNA from the params, runs it, emits the magnitude
  trace immediately for the live plot, saves magnitude+phase to the QCoDeS db,
  then returns the PNA to a safe low-heat-load state.

HPD (homophasal point distribution)
-----------------------------------
``measure_resonator_hpd`` now uses a real PNA *segment sweep* over the
resonator's full chosen span (the span is no longer shrunk).  At every power
the frequency points are redistributed so they are equidistant in resonance
phase (Baity et al., PRR 6, 013329 (2024), Sec. IV), using fr and Ql from the
previous (higher) power's fit.  See core/pna_segment.py.
"""

from __future__ import annotations

import math
import time
import threading
import traceback

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal


def format_temp_label(t_k) -> str:
    if t_k is None or (isinstance(t_k, float) and math.isnan(t_k)):
        return "?mK"            # temperature unreadable — placeholder, run still proceeds
    if t_k < 1.0:
        return f"{t_k * 1000:.1f}mK"
    return f"{t_k:.3f}K"


def trigger_and_wait(pna, abort_event, navg: int = 1, poll: float = 0.15) -> bool:
    """
    Trigger one (optionally averaged) sweep through the qcodes driver's own
    parameters — exactly the way the driver's run_sweep() does — but poll
    ``sweep_mode`` ourselves so Stop is honoured within ~poll seconds, and
    disable ``auto_sweep`` so the subsequent magnitude/phase reads simply return
    the data we just took (no re-trigger, and no driver-internal blocking wait).

    Using the driver parameters (not raw status SCPI) avoids desyncing the VISA
    session against the binary FORM REAL,32 data format — mixing raw *OPC/*ESR?
    polling with the driver's reads is what produced the
    'ascii codec can't decode byte 0xa2' error. Returns True if the sweep
    completed, False if aborted. Falls back to the stock blocking sweep only if
    the driver path raises.
    """
    try:
        try:
            pna.auto_sweep(False)
        except Exception:
            pass
        n = int(navg) if navg else 1
        if n > 1:
            try:
                pna.reset_averages()
            except Exception:
                pass
            try:
                pna.group_trigger_count(n)
            except Exception:
                pass
            pna.sweep_mode("GRO")
        else:
            pna.sweep_mode("SING")
        while True:
            if abort_event.is_set():
                try:
                    pna.write("ABOR")
                    pna.sweep_mode("HOLD")
                except Exception:
                    pass
                return False
            try:
                if str(pna.sweep_mode()).strip().upper().startswith("HOLD"):
                    return True
            except Exception:
                pass        # transient read — keep polling, stay abortable
            time.sleep(poll)
    except Exception:
        try:
            pna.traces.tr1.run_sweep()
        except Exception:
            raise
        return not abort_event.is_set()


def safe_pna(pna):
    """RF off + low power, immediately (no blocking sweep)."""
    try:
        pna.write("ABOR")
    except Exception:
        pass
    try:
        pna.power(-80)
        pna.output(0)
    except Exception:
        pass


def ensure_linear_sweep(pna):
    """Make sure a previous HPD (segment) run didn't leave the PNA in SEGM mode."""
    try:
        from core.pna_segment import restore_linear_sweep
        if str(pna.sweep_type()).upper().startswith("SEGM"):
            restore_linear_sweep(pna)
    except Exception:
        pass


class WidebandWorker(QThread):
    progress = pyqtSignal(str)
    temperature_update = pyqtSignal(float)   # Kelvin, nan when unreadable
    countdown = pyqtSignal(int)              # seconds to next ref-temp check
    sweep_data = pyqtSignal(object, object)  # freq_ghz, mag_db
    finished = pyqtSignal(dict)
    aborted = pyqtSignal()                   # emitted when Stop takes effect
    error = pyqtSignal(str)

    def __init__(self, instrument_manager, params: dict, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.p = params
        self._abort = threading.Event()
        self._run_now = threading.Event()

    # control from GUI thread
    def abort(self):
        self._abort.set()

    def run_now(self):
        self._run_now.set()

    # ------------------------------------------------------------------

    def run(self):
        try:
            self._run()
        except Exception:
            self.error.emit(traceback.format_exc())

    def _run(self):
        im, p = self.im, self.p

        if p.get("ref_wait"):
            if not self._wait_for_reference():
                self.progress.emit("Wait aborted — manual control restored.")
                self.aborted.emit()
                return
        if self._abort.is_set():
            self.aborted.emit()
            return

        try:
            t_k = im.label_temperature_k()
        except Exception:
            t_k = None
        self.temperature_update.emit(t_k if t_k is not None else float("nan"))
        t_label = format_temp_label(t_k)
        if t_k is None:
            self.progress.emit(f"⚠ Temperature unreadable — labelling this run '{t_label}'; "
                               "measurement continues.")

        self._sweep(t_k, t_label)

    # ------------------------------------------------------------------

    def _wait_for_reference(self) -> bool:
        """Returns True to proceed with the sweep, False if aborted."""
        im, p = self.im, self.p
        label = p.get("ref_label", "")
        target = float(p.get("ref_target_k", 0.015))
        margin = float(p.get("ref_margin", 1.0))
        poll = int(p.get("poll_interval_s", 3600))
        threshold = target * (1.0 + margin)

        self.progress.emit(
            f"Waiting for {label} ≤ {format_temp_label(threshold)} "
            f"(target {format_temp_label(target)} + {int(margin*100)}% band). "
            f"Checking every {poll // 60} min. You can Run now or Stop any time."
        )
        next_check = time.time()   # check immediately on entry
        while True:
            if self._abort.is_set():
                return False
            if self._run_now.is_set():
                self.progress.emit("Run-now requested — starting sweep.")
                return True
            now = time.time()
            if now >= next_check:
                t = im.read_reference_k(label)
                self.temperature_update.emit(t if t is not None else float("nan"))
                if t is not None and t <= threshold:
                    self.progress.emit(
                        f"✓ {label} = {format_temp_label(t)} ≤ "
                        f"{format_temp_label(threshold)}. Starting sweep…")
                    return True
                shown = format_temp_label(t) if t is not None else "unreadable"
                self.progress.emit(
                    f"{label} = {shown}; not yet in band. "
                    f"Next check in {poll // 60} min.")
                next_check = now + poll
            self.countdown.emit(max(0, int(next_check - now)))
            time.sleep(1.0)

    # ------------------------------------------------------------------

    def _sweep(self, t_k, t_label):
        im, p = self.im, self.p
        pna = im.pna
        if pna is None:
            raise RuntimeError("PNA is not connected.")

        self.progress.emit("Configuring PNA…")
        ensure_linear_sweep(pna)
        pna.averages_enabled(int(bool(p.get("avg_enabled", False))))
        pna.averages(int(p.get("averages", 1)))
        pna.start(float(p["start_hz"]))
        pna.stop(float(p["stop_hz"]))
        pna.power(float(p["power_dbm"]))
        pna.points(int(p["points"]))
        pna.if_bandwidth(int(p["if_bw"]))
        pna.trace(p.get("trace", "S21"))
        pna.output(1)

        sample = p.get("sample_name", "") or "sample"
        last_label = sample.split("_")[-1] if "_" in sample else sample
        f0 = float(p["start_hz"]) / 1e9
        f1 = float(p["stop_hz"]) / 1e9
        atten = int(p.get("inline_attenuation_db", 80))
        chip = str(p.get("chip") or last_label)
        meas_name = (f"{chip}_Wide_{t_label}_{f0:.3f}to{f1:.3f}GHz_"
                     f"-{atten}dBInlineAttenuation")

        from qcodes.dataset import load_or_create_experiment, Measurement
        exp = load_or_create_experiment(experiment_name="Freqscanwide",
                                        sample_name=sample)
        meas = Measurement(exp=exp, station=im.station, name=meas_name)
        meas.register_parameter(pna.power)
        meas.register_parameter(pna.magnitude, setpoints=(pna.power,))
        meas.register_parameter(pna.phase, setpoints=(pna.power,))
        meas.write_period = 2

        if self._abort.is_set():
            self.aborted.emit()
            return

        self.progress.emit("Triggering PNA sweep… (Stop is responsive)")
        freq_axis = np.linspace(float(p["start_hz"]), float(p["stop_hz"]),
                                int(p["points"]))
        if not self._triggered_sweep(pna):
            # aborted mid-sweep
            self._safe_state(pna)
            self.progress.emit("✗ Sweep aborted — PNA set to safe state.")
            self.aborted.emit()
            return

        pows = pna.power()
        phase = pna.phase()
        mag = pna.magnitude()
        mag_arr = np.array(mag, dtype=float)
        self.sweep_data.emit(freq_axis / 1e9, mag_arr)
        self.progress.emit("Sweep data received — saving…")
        with meas.run() as datasaver:
            datasaver.add_result((pna.power, pows),
                                 (pna.magnitude, mag),
                                 (pna.phase, phase))
            run_id = datasaver.run_id
        self.progress.emit(f"✓ Saved to database. Run ID: {run_id}")

        self.progress.emit("Setting PNA to safe state (-80 dBm, RF off)…")
        self._safe_state(pna)

        self.finished.emit({
            "freq_ghz": freq_axis / 1e9,
            "mag_db": np.array(mag, dtype=float),
            "run_id": run_id,
            "temp_k": t_k if t_k is not None else float("nan"),
            "meas_name": meas_name,
            "sample_name": sample,
        })

    # ------------------------------------------------------------------

    def _triggered_sweep(self, pna) -> bool:
        """Interruptible sweep via the shared helper (single implementation)."""
        navg = int(self.p.get("averages", 1)) if self.p.get("avg_enabled") else 1
        return trigger_and_wait(pna, self._abort, navg)

    def _safe_state(self, pna):
        """RF off + low power, without a blocking sweep (immediate)."""
        try:
            pna.write("ABOR")
        except Exception:
            pass
        try:
            pna.power(-80)
            pna.output(0)
        except Exception:
            pass


class QualityWorker(QThread):
    """
    Measure each confirmed resonator once at a (relatively high) power with a
    single average, over that resonator's chosen span, to assess quality.
    Emits per-resonator data so the window can circlefit + display incrementally.
    """
    progress = pyqtSignal(str)
    res_measured = pyqtSignal(dict)
    finished = pyqtSignal(list)
    aborted = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, instrument_manager, resonators: list, params: dict, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.resonators = resonators
        self.p = params
        self._abort = threading.Event()

    def abort(self):
        self._abort.set()

    def run(self):
        try:
            self._run()
        except Exception:
            self.error.emit(traceback.format_exc())

    def _run(self):
        im, p = self.im, self.p
        pna = im.pna
        if pna is None:
            raise RuntimeError("PNA is not connected.")
        from qcodes.dataset import load_or_create_experiment, Measurement

        try:
            t_k = im.label_temperature_k()
        except Exception:
            t_k = None
        t_label = format_temp_label(t_k)
        if t_k is None:
            self.progress.emit(f"⚠ Temperature unreadable — labelling Quality runs '{t_label}'.")
        sample = im.sample_name or "sample"
        last_label = sample.split("_")[-1] if "_" in sample else sample
        power = float(p["power_dbm"])
        results = []

        ensure_linear_sweep(pna)
        pna.averages_enabled(0)
        pna.averages(int(p.get("averages", 1)))
        pna.if_bandwidth(int(p["if_bw"]))
        pna.power(power)
        pna.points(int(p["points"]))
        pna.trace(p.get("trace", "S21"))

        for r in self.resonators:
            if self._abort.is_set():
                break
            num = r.get("num")
            chip = _chip_prefix(r, last_label)
            f0 = float(r["fstart_hz"]); f1 = float(r["fstop_hz"])
            self.progress.emit(f"Measuring {chip} Res {num} ({f0/1e9:.6f}–{f1/1e9:.6f} GHz)…")
            pna.start(f0); pna.stop(f1); pna.output(1)

            meas_name = (f"{chip}_Res{num}_Quality_{t_label}_"
                         f"{f0/1e9:.6f}to{f1/1e9:.6f}GHz_{power:g}dBm")
            exp = load_or_create_experiment(experiment_name="Quality",
                                            sample_name=sample)
            meas = Measurement(exp=exp, station=im.station, name=meas_name)
            meas.register_parameter(pna.power)
            meas.register_parameter(pna.magnitude, setpoints=(pna.power,))
            meas.register_parameter(pna.phase, setpoints=(pna.power,))
            freq_axis = np.linspace(f0, f1, int(p["points"]))
            navg = int(p.get("averages", 1)) if p.get("avg_enabled") else 1
            if not trigger_and_wait(pna, self._abort, navg):
                break    # aborted mid-sweep; don't save a partial run
            mag = pna.magnitude(); phase = pna.phase(); pows = pna.power()
            with meas.run() as ds:
                ds.add_result((pna.power, pows), (pna.magnitude, mag), (pna.phase, phase))
                run_id = ds.run_id
            item = {
                "num": num, "chip": r.get("chip"), "center_hz": float(r["center_hz"]),
                "fstart_hz": f0, "fstop_hz": f1,
                "f_hz": freq_axis,
                "mag_db": np.array(mag, dtype=float),
                "phase_deg": np.array(phase, dtype=float),
                "run_id": run_id, "temp_k": t_k if t_k is not None else float("nan"),
                "power_dbm": power, "span_mhz": r.get("span_mhz"),
            }
            results.append(item)
            self.res_measured.emit(item)
            self.progress.emit(f"✓ Res {num} saved (run {run_id}).")

        safe_pna(pna)
        if self._abort.is_set():
            self.aborted.emit()
        else:
            self.finished.emit(results)


# ===========================================================================
# Shared per-resonator measurement routines (used by Power and Temperature)
# ===========================================================================

def _chip_prefix(r, fallback) -> str:
    """Name prefix for a resonator's runs: its chip name if set, else the
    session sample-name fallback. Keeps multi-chip runs unambiguous."""
    c = r.get("chip")
    return str(c) if c else str(fallback)


def _meta(im, t_label_override=None):
    try:
        t_k = im.label_temperature_k()
    except Exception:
        t_k = None
    sample = im.sample_name or "sample"
    last = sample.split("_")[-1] if "_" in sample else sample
    t_label = t_label_override or format_temp_label(t_k)
    return t_k, t_label, sample, last


def _safe_pna(im):
    safe_pna(im.pna)


def measure_resonator_spd(im, r, schedule, sched_map, points, trace, tag,
                          t_label_override, abort, on_point, on_progress):
    """One resonator, SPD linear sweep, powers low->high, single multi-power run."""
    from qcodes.dataset import load_or_create_experiment, Measurement
    from core.fitting import fit_notch, fit_notch_auto, s21_from_mag_phase
    pna = im.pna
    num = r["num"]
    f0, f1 = float(r["fstart_hz"]), float(r["fstop_hz"])
    t_k, t_label, sample, last = _meta(im, t_label_override)
    if t_k is None:
        on_progress(f"⚠ Temperature unreadable — labelling this run '{t_label}'; "
                    "measurement continues.")
    powers_asc = [s[0] for s in sorted(schedule, key=lambda s: s[0])]

    chip = _chip_prefix(r, last)
    meas_name = f"{chip}_Res{num}_{tag}_SPD_{t_label}_{f0/1e9:.6f}to{f1/1e9:.6f}GHz"
    exp = load_or_create_experiment(tag, sample_name=sample)
    meas = Measurement(exp=exp, station=im.station, name=meas_name)
    meas.register_parameter(pna.power)
    meas.register_parameter(pna.magnitude, setpoints=(pna.power,))
    meas.register_parameter(pna.phase, setpoints=(pna.power,))
    meas.write_period = 2

    ensure_linear_sweep(pna)
    pna.start(f0); pna.stop(f1); pna.points(points); pna.trace(trace)
    freq = np.linspace(f0, f1, points)
    qi_curve = []
    on_progress(f"Res {num} @ {t_label}: SPD power sweep, {len(powers_asc)} powers (low→high)…")
    with meas.run() as ds:
        for pw in powers_asc:
            if abort.is_set():
                break
            av, bw = sched_map[round(float(pw), 3)]
            pna.averages_enabled(1 if av > 1 else 0)
            pna.averages(int(av)); pna.if_bandwidth(int(bw))
            pna.power(float(pw)); pna.output(1)
            if not trigger_and_wait(pna, abort, av):
                break    # aborted mid-sweep
            mag = np.array(pna.magnitude(), float)
            phase = np.array(pna.phase(), float)
            ds.add_result((pna.power, pw), (pna.magnitude, mag), (pna.phase, phase))
            fit = fit_notch_auto(freq, s21_from_mag_phase(mag, phase))
            qi_curve.append((float(pw), fit.get("Qi"), fit.get("Qi_err")))
            on_point({"num": num, "chip": r.get("chip"), "power_dbm": float(pw), "mode": "spd",
                      "Qi": fit.get("Qi"), "Qi_err": fit.get("Qi_err"),
                      "fr": fit.get("fr"), "fit_ok": fit.get("ok"),
                      "f_hz": freq, "mag_db": mag, "phase_deg": phase,
                      "reused": False, "temp_k": t_k, "t_label": t_label})
            on_progress(f"  Res {num} @ {pw:g} dBm: "
                        + (f"Qi={fit['Qi']:.3g}" if fit.get('ok') else "fit failed"))
        run_id = ds.run_id
    _safe_pna(im)
    return {"num": num, "chip": r.get("chip"), "mode": "spd", "run_id": run_id,
            "qi_vs_power": qi_curve, "temp_k": t_k, "t_label": t_label}


def noise_to_radius(fit) -> float:
    """
    Background noise (per quadrature, rms) divided by the fitted circle radius,
    from the fit residuals. Below ~0.05 HPD clearly beats SPD; above ~0.1 the
    circle fit itself becomes noise-biased (Qi too high), and HPD more so,
    because its points no longer pile up at the off-resonant point.
    """
    try:
        z = np.asarray(fit["z_raw"], complex); zs = np.asarray(fit["z_sim_raw"], complex)
        if z.size != zs.size or z.size < 8:
            return float("nan")
        p = zs[np.argmax(np.abs(zs - zs[0]))]
        radius = 0.5 * float(np.max(np.abs(zs - p)))
        sigma = float(np.sqrt(np.mean(np.abs(z - zs) ** 2) / 2.0))
        return sigma / radius if radius > 0 else float("nan")
    except Exception:
        return float("nan")


LOW_SNR_RATIO = 0.1


def _linear_seed(pna, f0, f1, points, av, bw, power, abort, fit_fn):
    """One linear sweep over the window to get an fr/Ql seed. Returns fit dict or None."""
    from core.fitting import s21_from_mag_phase
    ensure_linear_sweep(pna)
    pna.start(f0); pna.stop(f1); pna.points(int(points))
    pna.if_bandwidth(int(bw)); pna.averages_enabled(1 if av > 1 else 0); pna.averages(int(av))
    pna.power(float(power)); pna.output(1)
    if not trigger_and_wait(pna, abort, av):
        return None
    mag = np.array(pna.magnitude(), float)
    phase = np.array(pna.phase(), float)
    fit = fit_fn(np.linspace(f0, f1, mag.size), s21_from_mag_phase(mag, phase))
    return fit if fit.get("ok") else None


MIN_SEED_DIAMETER = 0.01     # Ql/|Qc| below this = no clear resonance in the span


def seed_fit_problem(fit, win_lo, win_hi):
    """None if ``fit`` can seed HPD, else a short reason."""
    if not fit.get("ok"):
        return fit.get("error") or "fit failed"
    if not (win_lo <= fit["fr"] <= win_hi) or not fit.get("Ql", 0) > 0:
        return "fitted fr outside the span"
    try:
        d = float(fit["Ql"]) / float(fit["absQc"])
    except Exception:
        d = float("nan")
    if not (d >= MIN_SEED_DIAMETER):
        return (f"no clear resonance (circle diameter Ql/|Qc| = {d:.3g}, "
                f"needs ≥ {MIN_SEED_DIAMETER:g})")
    return None


def _seed_candidates(lin_fits, win_lo, win_hi):
    """Linear-sweep fits usable as an HPD seed, lowest power first."""
    return [pw for pw in sorted(lin_fits)
            if seed_fit_problem(lin_fits[pw]["fit"], win_lo, win_hi) is None]


def _seed_info(r, num, t_label, hpd_start, lin_fits, default_pw, timeout_note=""):
    """Everything the confirmation dialog needs (plain python/numpy, no Qt)."""
    fits = []
    for pw in sorted(lin_fits):
        d = lin_fits[pw]; f = d["fit"]
        fits.append({
            "power_dbm": float(pw), "ok": bool(f.get("ok")),
            "f_hz": d["f_hz"], "mag_db": d["mag_db"], "phase_deg": d["phase_deg"],
            "fit_f_hz": f.get("f_hz"), "z_raw": f.get("z_raw"), "z_sim": f.get("z_sim_raw"),
            "fr": f.get("fr"), "Ql": f.get("Ql"), "Qi": f.get("Qi"), "Qi_err": f.get("Qi_err"),
            "absQc": f.get("absQc"), "noise_over_radius": d.get("nr"),
            "error": f.get("error", ""),
        })
    chip = r.get("chip") or ""
    return {"num": num, "chip": chip,
            "label": f"{chip}_Res{num}" if chip else f"Res {num}",
            "t_label": t_label, "hpd_start_dbm": hpd_start,
            "default_power_dbm": default_pw, "fits": fits,
            "fstart_hz": float(r["fstart_hz"]), "fstop_hz": float(r["fstop_hz"])}


def measure_resonator_hpd(im, r, schedule, sched_map, points, trace, reject, tag,
                          t_label_override, abort, on_point, on_progress,
                          hpd_start_dbm=None, confirm_seed=None, seed_policy=None):
    """
    One resonator, powers high->low, single run.

    * Powers ABOVE ``hpd_start_dbm`` are measured with ordinary linear sweeps
      over the resonator's span (Kerr-distorted high-power fits never feed the
      HPD).  ``hpd_start_dbm=None`` means every power is HPD (old behaviour).
    * At the first power <= ``hpd_start_dbm`` the HPD is seeded from a linear
      fit — by default the lowest linear power.  If ``confirm_seed`` is given,
      the user is shown that fit first and can accept it, pick another linear
      power's fit, keep this resonator linear, or abort.  ``confirm_seed``
      returns {"action": "use"|"linear"|"abort"|"timeout", "power": P}; on
      "timeout" (nobody answered) the resonator continues LINEAR — always safe.
    * The decision is stored in ``seed_policy`` (keyed by chip|num) so a
      temperature sweep asks once per resonator and reuses the answer.
    * HPD powers: homophasal segment sweep over the full span (Baity et al.
      2024, Sec. IV), seeded from the previous HPD power's fit; a Qi jump beyond
      ``reject``x keeps the previous seed. If the PNA refuses a segment table,
      that power falls back to a linear sweep (flagged ``fallback_spd``).
    Every power has the same number of points, so the run keeps the
    per-power-frequency layout analysis_io reads.
    """
    from qcodes.dataset import load_or_create_experiment, Measurement
    from core.fitting import fit_notch_auto, s21_from_mag_phase
    from core.pna_segment import build_hpd_segments, program_hpd, restore_linear_sweep
    pna = im.pna
    num = r["num"]
    key = f"{r.get('chip') or ''}|{num}"
    win_lo = float(r["fstart_hz"]); win_hi = float(r["fstop_hz"])
    span = max(win_hi - win_lo, 1.0)
    points = int(points)
    t_k, t_label, sample, last = _meta(im, t_label_override)
    if t_k is None:
        on_progress(f"⚠ Temperature unreadable — labelling this run '{t_label}'; "
                    "measurement continues.")
    powers_desc = [s[0] for s in sorted(schedule, key=lambda s: -s[0])]
    empty = {"num": num, "chip": r.get("chip"), "mode": "hpd", "run_id": None,
             "run_ids": [], "qi_vs_power": [], "temp_k": t_k, "t_label": t_label}
    if not powers_desc:
        return empty

    def is_hpd_power(pw):
        return hpd_start_dbm is None or float(pw) <= float(hpd_start_dbm) + 1e-9

    lin_powers = [p for p in powers_desc if not is_hpd_power(p)]
    hpd_powers = [p for p in powers_desc if is_hpd_power(p)]
    policy = seed_policy.get(key) if seed_policy is not None else None
    hpd_enabled = bool(hpd_powers)
    if policy and policy.get("action") == "linear" and hpd_enabled:
        hpd_enabled = False
        on_progress(f"Res {num}: HPD off for this resonator (earlier decision: "
                    f"{policy.get('why', 'keep linear')}); all powers linear.")

    pna.trace(trace)

    # ---- seed from the Quality step / a seed sweep (only when no linear
    #      powers precede the HPD block) -------------------------------------
    cur_fr = float(r.get("fr") or r.get("center_hz") or 0.5 * (win_lo + win_hi))
    cur_Ql = float(r["Ql"]) if r.get("Ql") else None
    trusted_Qi = r.get("Qi")
    if hpd_enabled and not lin_powers and cur_Ql is None:
        av0, bw0 = sched_map[round(float(powers_desc[0]), 3)]
        on_progress(f"Res {num}: no Ql seed — one linear sweep at {powers_desc[0]:g} dBm to seed HPD…")
        seed = _linear_seed(pna, win_lo, win_hi, points, av0, bw0, powers_desc[0], abort,
                            fit_notch_auto)
        if abort.is_set():
            _safe_pna(im)
            return empty
        if seed:
            cur_fr, cur_Ql, trusted_Qi = seed["fr"], seed["Ql"], seed["Qi"]
            on_progress(f"  seed: fr={cur_fr/1e9:.6f} GHz, Ql={cur_Ql:.3g}")
        else:
            cur_Ql = 10.0 * cur_fr / span      # assume span ≈ 10 linewidths
            on_progress(f"  seed fit failed; assuming span ≈ 10 linewidths (Ql≈{cur_Ql:.3g})")
    if cur_Ql is None:
        cur_Ql = 10.0 * cur_fr / span

    qi_curve = []
    if hpd_enabled and lin_powers:
        desc = (f"linear at {', '.join(f'{p:g}' for p in lin_powers)} dBm, "
                f"HPD from {hpd_powers[0]:g} dBm down")
    elif hpd_enabled:
        desc = "HPD at every power"
    else:
        desc = "linear at every power"
    on_progress(f"Res {num} @ {t_label}: power sweep high→low, {desc} "
                f"(full {span/1e6:.4g} MHz span, {points} pts)…")

    exp = load_or_create_experiment(tag, sample_name=sample)
    chip = _chip_prefix(r, last)
    meas_name = (f"{chip}_Res{num}_{tag}_HPD_{t_label}_"
                 f"{win_lo/1e9:.6f}to{win_hi/1e9:.6f}GHz")
    meas = Measurement(exp=exp, station=im.station, name=meas_name)
    meas.register_custom_parameter("power", unit="dBm")
    meas.register_custom_parameter("point", paramtype="array")
    meas.register_custom_parameter("frequency", unit="Hz", paramtype="array",
                                   setpoints=("power", "point"))
    meas.register_custom_parameter("mag", unit="dB", paramtype="array",
                                   setpoints=("power", "point"))
    meas.register_custom_parameter("phase", unit="deg", paramtype="array",
                                   setpoints=("power", "point"))
    meas.write_period = 2

    lin_fits = {}               # power -> {"fit", "f_hz", "mag_db", "phase_deg", "nr"}
    transition_done = False
    run_id = None
    try:
        with meas.run() as ds:
            try:
                ds.dataset.add_metadata("hpd_start_dbm",
                                        "all" if hpd_start_dbm is None else f"{hpd_start_dbm:g}")
            except Exception:
                pass
            for pw in powers_desc:
                if abort.is_set():
                    break
                use_hpd = hpd_enabled and is_hpd_power(pw)

                # ---- switching from linear to HPD: choose the seed ---------
                if use_hpd and not transition_done:
                    transition_done = True
                    if lin_fits:
                        decision = _decide_seed(r, num, key, t_label, hpd_start_dbm, lin_fits,
                                                win_lo, win_hi, policy, confirm_seed,
                                                seed_policy, on_progress)
                        if decision["action"] == "abort":
                            on_progress("✗ Run aborted at the HPD seed check.")
                            abort.set()
                            break
                        if decision["action"] == "use":
                            sf = lin_fits[decision["power"]]["fit"]
                            cur_fr, cur_Ql, trusted_Qi = sf["fr"], sf["Ql"], sf["Qi"]
                            on_progress(f"  Res {num}: HPD seeded from the {decision['power']:g} dBm "
                                        f"linear fit (fr={cur_fr/1e9:.6f} GHz, Ql={cur_Ql:.3g}).")
                        else:
                            hpd_enabled = False
                            use_hpd = False
                            on_progress(f"  Res {num}: continuing with LINEAR sweeps "
                                        f"({decision.get('why', 'no usable seed')}).")

                av, bw = sched_map[round(float(pw), 3)]
                pna.if_bandwidth(int(bw))
                pna.averages_enabled(1 if av > 1 else 0); pna.averages(int(av))
                pna.power(float(pw)); pna.output(1)

                # ---- frequency points for this power ------------------------
                plan, fallback, why = None, False, ""
                if use_hpd:
                    try:
                        plan = build_hpd_segments(win_lo, win_hi, cur_fr, cur_Ql, points)
                        if plan.n_points != points:
                            raise ValueError(f"plan has {plan.n_points} points, need {points}")
                        freq = np.asarray(program_hpd(pna, plan), float)
                        if freq.size != points:
                            raise RuntimeError(f"PNA stimulus has {freq.size} points")
                    except Exception as e:
                        fallback, why = True, f"{type(e).__name__}: {e}"
                        try:
                            pna.get_errors()      # don't let stale errors leak into the next power
                        except Exception:
                            pass
                        on_progress(f"  ⚠ Res {num} @ {pw:g} dBm: segment sweep unavailable "
                                    f"({why}); measuring this power with a linear sweep.")
                if not use_hpd or fallback:
                    ensure_linear_sweep(pna)
                    restore_linear_sweep(pna)
                    pna.start(win_lo); pna.stop(win_hi); pna.points(points)
                    freq = np.linspace(win_lo, win_hi, points)

                if not trigger_and_wait(pna, abort, av):
                    break
                mag = np.array(pna.magnitude(), float)
                phase = np.array(pna.phase(), float)
                if freq.size != mag.size:
                    try:
                        freq = np.asarray(pna.stimulus_axis(), float)
                    except Exception:
                        freq = np.linspace(win_lo, win_hi, mag.size)

                idx = np.arange(mag.size, dtype=float)
                ds.add_result(("power", float(pw)), ("point", idx),
                              ("frequency", freq), ("mag", mag), ("phase", phase))

                fit = fit_notch_auto(freq, s21_from_mag_phase(mag, phase))
                reused = False
                nr = noise_to_radius(fit) if fit.get("ok") else float("nan")
                if not use_hpd:
                    # linear power: record the fit; it never updates the HPD seed directly
                    lin_fits[float(pw)] = {"fit": fit, "f_hz": freq, "mag_db": mag,
                                           "phase_deg": phase, "nr": nr}
                    qi_curve.append((float(pw), fit.get("Qi") if fit.get("ok") else None,
                                     fit.get("Qi_err") if fit.get("ok") else None))
                elif fit.get("ok"):
                    new_Qi = fit["Qi"]
                    if nr > LOW_SNR_RATIO:
                        on_progress(f"  ⚠ Res {num} @ {pw:g} dBm: noise/radius = {nr:.2f} — low SNR, "
                                    "the circle fit (HPD especially) can overestimate Qi here. "
                                    "Increase averaging / lower IF bandwidth for this power.")
                    if trusted_Qi and max(new_Qi, trusted_Qi) / max(min(new_Qi, trusted_Qi), 1e-9) > reject:
                        reused = True
                        on_progress(f"  Res {num} @ {pw:g} dBm: Qi={new_Qi:.3g} jumped >"
                                    f"{reject:g}× vs {trusted_Qi:.3g}; keeping previous fr/Ql seed.")
                    elif not (win_lo <= fit["fr"] <= win_hi and fit["Ql"] > 0):
                        reused = True
                        on_progress(f"  Res {num} @ {pw:g} dBm: fitted fr outside the span; "
                                    "keeping previous seed.")
                    else:
                        cur_fr, cur_Ql = fit["fr"], fit["Ql"]
                        trusted_Qi = new_Qi
                    qi_curve.append((float(pw), new_Qi, fit.get("Qi_err")))
                else:
                    reused = True
                    qi_curve.append((float(pw), None, None))
                    on_progress(f"  Res {num} @ {pw:g} dBm: fit failed; keeping previous seed.")

                hpd_used = use_hpd and not fallback
                on_point({"num": num, "chip": r.get("chip"), "power_dbm": float(pw), "mode": "hpd",
                          "sweep": "hpd" if hpd_used else "linear",
                          "Qi": fit.get("Qi"), "Qi_err": fit.get("Qi_err"),
                          "fr": fit.get("fr"), "Ql": fit.get("Ql"),
                          "theta0": fit.get("theta0"), "fit_ok": fit.get("ok"),
                          "f_hz": freq, "mag_db": mag, "phase_deg": phase,
                          "reused": reused, "fallback_spd": fallback, "noise_over_radius": nr,
                          "hpd_segments": (len(plan.segments) if hpd_used else 0),
                          "hpd_uniformity": (plan.uniformity if hpd_used else None),
                          "window_hz": span, "temp_k": t_k, "t_label": t_label})
                if fit.get("ok"):
                    on_progress(f"  Res {num} @ {pw:g} dBm: Qi={fit['Qi']:.3g}"
                                + (f"  (HPD, {len(plan.segments)} segments, "
                                   f"span/linewidth≈{plan.span_over_linewidth:.0f})"
                                   if hpd_used else "  (linear)"))
            run_id = ds.run_id
    finally:
        # leave the PNA on a normal linear sweep of this resonator
        try:
            restore_linear_sweep(pna)
            pna.start(win_lo); pna.stop(win_hi); pna.points(points)
        except Exception:
            pass
        _safe_pna(im)

    qi_curve.sort(key=lambda t: t[0])
    return {"num": num, "chip": r.get("chip"), "mode": "hpd", "run_id": run_id, "run_ids": [run_id],
            "qi_vs_power": qi_curve, "temp_k": t_k, "t_label": t_label}


def _decide_seed(r, num, key, t_label, hpd_start, lin_fits, win_lo, win_hi,
                 policy, confirm_seed, seed_policy, on_progress):
    """
    Pick the linear fit that seeds the HPD block.
    Returns {"action": "use", "power": P} | {"action": "linear", "why": ...} |
            {"action": "abort"}.
    """
    usable = _seed_candidates(lin_fits, win_lo, win_hi)
    default_pw = min(lin_fits)               # lowest linear power
    # an earlier temperature already settled this resonator
    if policy and policy.get("action") == "use":
        want = policy.get("power")
        pw = want if want in usable else (default_pw if default_pw in usable else
                                          (usable[0] if usable else None))
        if pw is None:
            return {"action": "linear", "why": "no linear fit usable at this temperature"}
        if pw != want:
            on_progress(f"  Res {num}: the {want:g} dBm fit you chose earlier is not usable at "
                        f"{t_label}; seeding from {pw:g} dBm instead.")
        return {"action": "use", "power": pw}

    if confirm_seed is None:                 # no confirmation requested
        if default_pw in usable:
            return {"action": "use", "power": default_pw}
        return {"action": "linear", "why": f"the {default_pw:g} dBm fit is not usable"}

    on_progress(f"  ⏸ Res {num}: waiting for you to confirm the HPD seed fit "
                f"(lowest linear power {default_pw:g} dBm)…")
    ans = confirm_seed(_seed_info(r, num, t_label, hpd_start, lin_fits, default_pw)) or {}
    act = ans.get("action", "timeout")
    if act == "abort":
        return {"action": "abort"}
    if act == "use":
        pw = float(ans.get("power", default_pw))
        if pw not in usable:
            on_progress(f"  Res {num}: the {pw:g} dBm fit is not usable; keeping linear.")
            dec = {"action": "linear", "why": f"chosen fit at {pw:g} dBm not usable"}
        else:
            dec = {"action": "use", "power": pw}
    elif act == "linear":
        dec = {"action": "linear", "why": "you chose linear"}
    else:
        dec = {"action": "linear", "why": "no answer within the confirmation time"}
        on_progress(f"  Res {num}: no answer to the seed check — continuing LINEAR (safe).")
    if seed_policy is not None:
        seed_policy[key] = dict(dec)
    return dec


def _sched_for(r, default_schedule):
    """Return (schedule, sched_map) for a resonator — its own custom power table
    if one was assigned (r['_schedule']), else the default table."""
    s = r.get("_schedule") or default_schedule
    s = sorted(s, key=lambda x: x[0])
    m = {round(float(pw), 3): (int(av), int(bw)) for pw, av, bw in s}
    return s, m


def _measure_one(im, r, schedule, sched_map, params, t_label, abort, on_point, on_progress,
                 confirm_seed=None, seed_policy=None):
    """Dispatch SPD/HPD for one resonator."""
    points = int(params["points"]); trace = params.get("trace", "S21")
    tag = params.get("tag", "PowerDep")
    if params.get("mode") == "hpd":
        hs = params.get("hpd_start_dbm")
        return measure_resonator_hpd(im, r, schedule, sched_map, points, trace,
                                     float(params.get("qi_reject_factor", 7.0)),
                                     tag, t_label, abort, on_point, on_progress,
                                     hpd_start_dbm=(None if hs is None else float(hs)),
                                     confirm_seed=confirm_seed, seed_policy=seed_policy)
    return measure_resonator_spd(im, r, schedule, sched_map, points, trace,
                                 tag, t_label, abort, on_point, on_progress)


class _SeedConfirmMixin:
    """
    Up-front HPD seed check, done once before the sweep starts.

    Every selected resonator gets ONE linear sweep at its seed power (the
    lowest power above the HPD start). All fits go to the GUI in one window
    (``seed_precheck`` signal); the user ticks which resonators may use HPD and
    starts the sweep, or aborts. The answer is waited for at most
    ``seed_confirm_timeout_s`` (default 600 s); with no answer every resonator
    stays LINEAR. The decisions go into ``self._seed_policy`` (chip|num ->
    decision), which the power sweep (and every temperature) then follows
    without pausing again. Requires ``seed_precheck`` / ``seed_precheck_closed``
    signals and ``self._abort``.
    """

    def _init_seed_confirm(self):
        self._seed_event = threading.Event()
        self._seed_answer = None
        self._seed_policy = {}

    def answer_seed(self, decision: dict):
        """Called from the GUI thread: {"action": "start", "use": {key: bool}} or {"action": "abort"}."""
        self._seed_answer = dict(decision or {})
        self._seed_event.set()

    def _wait_seed_answer(self, info: dict) -> dict:
        timeout = float(self.p.get("seed_confirm_timeout_s", 600))
        self._seed_answer = None
        self._seed_event.clear()
        self.seed_precheck.emit(dict(info, timeout_s=timeout))
        t0 = time.time()
        while not self._seed_event.wait(0.2):
            if self._abort.is_set():
                self.seed_precheck_closed.emit("aborted")
                return {"action": "abort"}
            if time.time() - t0 >= timeout:
                self.seed_precheck_closed.emit("timeout")
                return {"action": "timeout"}
        return self._seed_answer or {"action": "timeout"}

    def _precheck_seeds(self, default_schedule) -> bool:
        """Run the up-front check. Returns False if the run should stop."""
        p = self.p
        if (p.get("mode") != "hpd" or not p.get("confirm_seed", True)
                or p.get("hpd_start_dbm") is None):
            return True
        from core.fitting import fit_notch_auto, s21_from_mag_phase
        hs = float(p["hpd_start_dbm"])
        points = int(p["points"])
        pna = self.im.pna
        items = []
        todo = []
        for r in self.resonators:
            sch, smap = _sched_for(r, default_schedule)
            lin = [float(s[0]) for s in sch if float(s[0]) > hs + 1e-9]
            has_hpd = any(float(s[0]) <= hs + 1e-9 for s in sch)
            if lin and has_hpd:
                todo.append((r, min(lin), smap))
        if not todo:
            return True
        self.progress.emit(f"HPD seed check: one linear sweep per resonator at its seed power "
                           f"({len(todo)} resonator(s)) before the power sweep…")
        try:
            ensure_linear_sweep(pna)
            pna.trace(p.get("trace", "S21"))
            for r, p_seed, smap in todo:
                if self._abort.is_set():
                    return False
                av, bw = smap[round(p_seed, 3)]
                f0, f1 = float(r["fstart_hz"]), float(r["fstop_hz"])
                pna.start(f0); pna.stop(f1); pna.points(points)
                pna.if_bandwidth(int(bw)); pna.averages_enabled(1 if av > 1 else 0)
                pna.averages(int(av)); pna.power(float(p_seed)); pna.output(1)
                if not trigger_and_wait(pna, self._abort, av):
                    return False
                mag = np.array(pna.magnitude(), float); ph = np.array(pna.phase(), float)
                f = np.linspace(f0, f1, mag.size)
                fit = fit_notch_auto(f, s21_from_mag_phase(mag, ph))
                problem = seed_fit_problem(fit, f0, f1)
                ok = problem is None
                chip = r.get("chip") or ""
                items.append({
                    "key": f"{chip}|{r['num']}", "num": r["num"], "chip": chip,
                    "label": f"{chip}_Res{r['num']}" if chip else f"Res {r['num']}",
                    "power_dbm": p_seed, "ok": ok,
                    "f_hz": f, "mag_db": mag, "phase_deg": ph,
                    "fit_f_hz": fit.get("f_hz"), "z_raw": fit.get("z_raw"),
                    "z_sim": fit.get("z_sim_raw"),
                    "fr": fit.get("fr"), "Ql": fit.get("Ql"), "Qi": fit.get("Qi"),
                    "Qi_err": fit.get("Qi_err"), "absQc": fit.get("absQc"),
                    "noise_over_radius": noise_to_radius(fit) if fit.get("ok") else None,
                    "error": problem or "",
                    "fstart_hz": f0, "fstop_hz": f1,
                })
                self.progress.emit(f"  {items[-1]['label']} @ {p_seed:g} dBm: "
                                   + (f"Qi={fit['Qi']:.3g}, Ql={fit['Ql']:.3g}" if ok
                                      else f"not usable as HPD seed ({problem})"))
        finally:
            safe_pna(pna)

        self.progress.emit("⏸ Waiting for you to confirm the HPD seed fits…")
        ans = self._wait_seed_answer({"items": items, "hpd_start_dbm": hs})
        act = ans.get("action", "timeout")
        if act == "abort":
            self.progress.emit("✗ Run aborted at the HPD seed check.")
            self._abort.set()
            return False
        use = ans.get("use", {}) if act == "start" else {}
        n_hpd = 0
        for it in items:
            if act == "start" and use.get(it["key"]) and it["ok"]:
                self._seed_policy[it["key"]] = {"action": "use", "power": it["power_dbm"]}
                n_hpd += 1
            else:
                why = ("no answer to the seed check" if act != "start"
                       else (f"seed fit not usable: {it['error']}" if not it["ok"]
                             else "unticked in the seed check"))
                self._seed_policy[it["key"]] = {"action": "linear", "why": why}
        if act != "start":
            self.progress.emit("Seed check not answered in time — all resonators stay LINEAR.")
        else:
            self.progress.emit(f"✓ Seed check done: HPD for {n_hpd}, linear for "
                               f"{len(items) - n_hpd} resonator(s). Starting the power sweep.")
        return True


# ===========================================================================
# Power-dependent worker
# ===========================================================================

class PowerWorker(_SeedConfirmMixin, QThread):
    """Power-dependent sweep (SPD low->high single run, or HPD high->low)."""
    progress = pyqtSignal(str)
    point_measured = pyqtSignal(dict)
    res_finished = pyqtSignal(dict)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    seed_precheck = pyqtSignal(object)       # all seed fits -> GUI shows the check window
    seed_precheck_closed = pyqtSignal(str)   # "timeout" / "aborted" -> GUI closes it

    def __init__(self, instrument_manager, resonators, params, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.resonators = resonators
        self.p = dict(params); self.p.setdefault("tag", "PowerDep")
        self._abort = threading.Event()
        self._init_seed_confirm()

    def abort(self):
        self._abort.set()

    def run(self):
        try:
            if self.im.pna is None:
                raise RuntimeError("PNA is not connected.")
            default_schedule = sorted(self.p["schedule"], key=lambda s: s[0])
            results = []
            if not self._precheck_seeds(default_schedule):
                self.finished.emit(results)
                return
            for r in self.resonators:
                if self._abort.is_set():
                    break
                sch, smap = _sched_for(r, default_schedule)
                res = _measure_one(self.im, r, sch, smap, self.p, None,
                                   self._abort, self.point_measured.emit, self.progress.emit,
                                   confirm_seed=None,
                                   seed_policy=self._seed_policy)
                results.append(res)
                self.res_finished.emit(res)
            self.finished.emit(results)
        except Exception:
            self.error.emit(traceback.format_exc())


# ===========================================================================
# Temperature- (and power-) dependent worker
# ===========================================================================

class TemperatureWorker(_SeedConfirmMixin, QThread):
    """
    For each target temperature: set the controller setpoint *with read-back
    verification* (fixes the setpoint/target mismatch), wait for stability, then
    run a full power sweep (SPD or HPD) over the selected resonators. Each
    temperature's measurements get their own run id(s), and the temperature
    label is encoded in every run name.
    """
    progress = pyqtSignal(str)
    temperature_update = pyqtSignal(float)     # measured control temp during wait
    point_measured = pyqtSignal(dict)
    temp_finished = pyqtSignal(dict)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    seed_precheck = pyqtSignal(object)
    seed_precheck_closed = pyqtSignal(str)

    def __init__(self, instrument_manager, resonators, params, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.resonators = resonators
        self.p = dict(params); self.p.setdefault("tag", "TempPowerDep")
        self._abort = threading.Event()
        self._init_seed_confirm()

    def abort(self):
        self._abort.set()

    def run(self):
        try:
            self._run()
        except Exception:
            self.error.emit(traceback.format_exc())

    def _run(self):
        im, p = self.im, self.p
        if im.pna is None:
            raise RuntimeError("PNA is not connected.")
        fridge = im.fridge
        if fridge is None or not fridge.is_connected():
            raise RuntimeError("Fridge is not connected — temperature control unavailable.")

        temps = list(p["temperatures_k"])
        default_schedule = sorted(p["schedule"], key=lambda s: s[0])
        all_results = []
        if not self._precheck_seeds(default_schedule):
            self.finished.emit(all_results)
            return

        for T in temps:
            if self._abort.is_set():
                break
            # ---- set & VERIFY the controller setpoint, with outer retries ---
            self.progress.emit(f"Setting target temperature {format_temp_label(T)} (verifying)…")
            confirmed = None
            last_err = None
            for rnd in range(6):        # 1 initial round of 5 attempts + 5 more rounds
                if self._abort.is_set():
                    break
                try:
                    confirmed = fridge.set_target_temperature(
                        T, tol_k=float(p.get("target_tol_k", 1e-4)))
                    break
                except Exception as e:
                    last_err = e
                    if rnd < 5:
                        self.progress.emit(
                            f"⚠ Controller did not accept {format_temp_label(T)} "
                            f"(round {rnd+1}/6). Waiting 5 s and retrying…")
                        for _ in range(50):     # abort-aware 5 s wait
                            if self._abort.is_set():
                                break
                            time.sleep(0.1)
            if self._abort.is_set():
                break
            if confirmed is None:
                self.error.emit(
                    f"Temperature control failed at {format_temp_label(T)}: the "
                    f"controller could not be set after 6 rounds of retries "
                    f"(5 attempts each, 5 s apart).\n\nLast error: {last_err}")
                return
            self.progress.emit(f"✓ Controller confirmed setpoint {format_temp_label(confirmed)}. "
                               "Waiting for stability…")
            # ---- wait for stability around the *verified* target ----------
            try:
                fridge.wait_until_stable(
                    T,
                    stable_mean_k=float(p.get("stable_mean_k", 0.002)),
                    stable_std_k=float(p.get("stable_std_k", 0.002)),
                    time_between_readings=float(p.get("time_between_readings", 5.0)),
                    window=int(p.get("window", 30)),
                    timeout_s=p.get("timeout_s"),
                    should_abort=self._abort.is_set,
                    on_reading=lambda t: self.temperature_update.emit(t),
                )
            except InterruptedError:
                self.progress.emit("Temperature wait aborted.")
                break
            except TimeoutError as e:
                self.error.emit(str(e))
                return
            self.progress.emit(f"✓ Stable at {format_temp_label(T)}. Running power sweep…")

            # ---- power sweep at this temperature --------------------------
            t_label = format_temp_label(T)     # target-based -> distinct per T
            temp_results = []
            for r in self.resonators:
                if self._abort.is_set():
                    break
                sch, smap = _sched_for(r, default_schedule)
                res = _measure_one(im, r, sch, smap, p, t_label,
                                   self._abort, self.point_measured.emit, self.progress.emit,
                                   confirm_seed=None,
                                   seed_policy=self._seed_policy)
                res["target_k"] = float(T)
                temp_results.append(res)
            self.temp_finished.emit({"target_k": float(T), "t_label": t_label,
                                     "results": temp_results})
            all_results.append({"target_k": float(T), "results": temp_results})

        self.finished.emit(all_results)
