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
        meas_name = (f"{last_label}_Wide_{t_label}_{f0:.3f}to{f1:.3f}GHz_"
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
            f0 = float(r["fstart_hz"]); f1 = float(r["fstop_hz"])
            self.progress.emit(f"Measuring Res {num} ({f0/1e9:.6f}–{f1/1e9:.6f} GHz)…")
            pna.start(f0); pna.stop(f1); pna.output(1)

            meas_name = (f"{last_label}_Res{num}_Quality_{t_label}_"
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
                "num": num, "center_hz": float(r["center_hz"]),
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
    from core.fitting import fit_notch, s21_from_mag_phase
    pna = im.pna
    num = r["num"]
    f0, f1 = float(r["fstart_hz"]), float(r["fstop_hz"])
    t_k, t_label, sample, last = _meta(im, t_label_override)
    if t_k is None:
        on_progress(f"⚠ Temperature unreadable — labelling this run '{t_label}'; "
                    "measurement continues.")
    powers_asc = [s[0] for s in sorted(schedule, key=lambda s: s[0])]

    meas_name = f"{last}_Res{num}_{tag}_SPD_{t_label}_{f0/1e9:.6f}to{f1/1e9:.6f}GHz"
    exp = load_or_create_experiment(tag, sample_name=sample)
    meas = Measurement(exp=exp, station=im.station, name=meas_name)
    meas.register_parameter(pna.power)
    meas.register_parameter(pna.magnitude, setpoints=(pna.power,))
    meas.register_parameter(pna.phase, setpoints=(pna.power,))
    meas.write_period = 2

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
            fit = fit_notch(freq, s21_from_mag_phase(mag, phase))
            qi_curve.append((float(pw), fit.get("Qi"), fit.get("Qi_err")))
            on_point({"num": num, "power_dbm": float(pw), "mode": "spd",
                      "Qi": fit.get("Qi"), "Qi_err": fit.get("Qi_err"),
                      "fr": fit.get("fr"), "fit_ok": fit.get("ok"),
                      "f_hz": freq, "mag_db": mag, "phase_deg": phase,
                      "reused": False, "temp_k": t_k, "t_label": t_label})
            on_progress(f"  Res {num} @ {pw:g} dBm: "
                        + (f"Qi={fit['Qi']:.3g}" if fit.get('ok') else "fit failed"))
        run_id = ds.run_id
    _safe_pna(im)
    return {"num": num, "mode": "spd", "run_id": run_id,
            "qi_vs_power": qi_curve, "temp_k": t_k, "t_label": t_label}


def measure_resonator_hpd(im, r, schedule, sched_map, points, trace, reject, tag,
                          t_label_override, abort, on_point, on_progress):
    """
    One resonator, HPD (high-power-dependent), powers high->low, per-power run.

    The Keysight qcodes driver does not support hardware *segment* sweeps (its
    magnitude/phase getters validate against a linear point grid), so HPD is done
    as an *adaptive narrow linear sweep*: at each descending power we sweep a
    tight window of a few linewidths around the current resonance with the user's
    point count. As Qi/Ql rise at low power the linewidth shrinks, the window
    shrinks with it, and the points concentrate on the resonance — the same
    benefit a segment sweep would give, but on the reliable driver path. The
    resonance is re-centred from each fit; a Qi jump beyond `reject`x reuses the
    previous seed instead.
    """
    from qcodes.dataset import load_or_create_experiment, Measurement
    from core.fitting import fit_notch, s21_from_mag_phase
    from core.pna_segment import restore_linear_sweep
    pna = im.pna
    num = r["num"]
    orig_f0 = float(r["fstart_hz"]); orig_f1 = float(r["fstop_hz"])
    full_span = max(orig_f1 - orig_f0, 1.0)
    t_k, t_label, sample, last = _meta(im, t_label_override)
    if t_k is None:
        on_progress(f"⚠ Temperature unreadable — labelling this run '{t_label}'; "
                    "measurement continues.")
    powers_desc = [s[0] for s in sorted(schedule, key=lambda s: -s[0])]

    cur_fr = float(r.get("fr") or r["center_hz"])
    cur_Ql = float(r.get("Ql") or 5e4)
    cur_th = float(r.get("theta0") or 0.0)
    trusted_Qi = r.get("Qi")
    qi_curve, run_ids = [], []
    on_progress(f"Res {num} @ {t_label}: HPD power sweep, {len(powers_desc)} powers (high→low)…")

    # make sure we're on a normal linear sweep
    try:
        restore_linear_sweep(pna)
    except Exception:
        pass
    exp = load_or_create_experiment(tag, sample_name=sample)
    # one run_id for the whole resonator; frequency varies per power (each power
    # uses its own adaptive window), so frequency is stored per power.
    meas_name = (f"{last}_Res{num}_{tag}_HPD_{t_label}_"
                 f"{orig_f0/1e9:.6f}to{orig_f1/1e9:.6f}GHz")
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

    def _window_hz():
        # ~±10 linewidths, clamped between 5% and 100% of the resonator's span
        linewidth = cur_fr / max(cur_Ql, 1.0)
        return float(np.clip(20.0 * linewidth, 0.05 * full_span, full_span))

    run_id = None
    with meas.run() as ds:
        for pw in powers_desc:
            if abort.is_set():
                break
            av, bw = sched_map[round(float(pw), 3)]

            window = _window_hz()
            f0 = cur_fr - window / 2.0
            f1 = cur_fr + window / 2.0
            if f0 < orig_f0:
                f0, f1 = orig_f0, orig_f0 + window
            if f1 > orig_f1:
                f0, f1 = orig_f1 - window, orig_f1
            f0 = max(f0, orig_f0); f1 = min(f1, orig_f1)

            pna.start(f0); pna.stop(f1)
            pna.points(int(points)); pna.if_bandwidth(int(bw))
            pna.averages_enabled(1 if av > 1 else 0); pna.averages(int(av))
            pna.power(float(pw)); pna.output(1)
            if not trigger_and_wait(pna, abort, av):
                break
            freq = np.linspace(f0, f1, int(points))
            mag = np.array(pna.magnitude(), float)
            phase = np.array(pna.phase(), float)
            if freq.size != mag.size:
                freq = np.linspace(f0, f1, mag.size)

            idx = np.arange(mag.size, dtype=float)
            ds.add_result(("power", float(pw)), ("point", idx),
                          ("frequency", freq), ("mag", mag), ("phase", phase))

            fit = fit_notch(freq, s21_from_mag_phase(mag, phase))
            reused = False
            if fit.get("ok"):
                new_Qi = fit["Qi"]
                if trusted_Qi and max(new_Qi, trusted_Qi) / max(min(new_Qi, trusted_Qi), 1e-9) > reject:
                    reused = True
                    on_progress(f"  Res {num} @ {pw:g} dBm: Qi={new_Qi:.3g} jumped >"
                                f"{reject:g}× vs {trusted_Qi:.3g}; reusing previous Ql/θ0.")
                else:
                    cur_fr, cur_Ql, cur_th = fit["fr"], fit["Ql"], fit.get("theta0", cur_th)
                    trusted_Qi = new_Qi
                qi_curve.append((float(pw), new_Qi, fit.get("Qi_err")))
            else:
                reused = True
                qi_curve.append((float(pw), None, None))
                on_progress(f"  Res {num} @ {pw:g} dBm: fit failed; reusing previous seed.")

            on_point({"num": num, "power_dbm": float(pw), "mode": "hpd",
                      "Qi": fit.get("Qi"), "Qi_err": fit.get("Qi_err"),
                      "fr": fit.get("fr"), "Ql": fit.get("Ql"),
                      "theta0": fit.get("theta0"), "fit_ok": fit.get("ok"),
                      "f_hz": freq, "mag_db": mag, "phase_deg": phase,
                      "reused": reused, "fallback_spd": False,
                      "window_hz": window, "temp_k": t_k, "t_label": t_label})
        run_id = ds.run_id

    _safe_pna(im)
    qi_curve.sort(key=lambda t: t[0])
    return {"num": num, "mode": "hpd", "run_id": run_id, "run_ids": [run_id],
            "qi_vs_power": qi_curve, "temp_k": t_k, "t_label": t_label}


def _measure_one(im, r, schedule, sched_map, params, t_label, abort, on_point, on_progress):
    """Dispatch SPD/HPD for one resonator."""
    points = int(params["points"]); trace = params.get("trace", "S21")
    tag = params.get("tag", "PowerDep")
    if params.get("mode") == "hpd":
        return measure_resonator_hpd(im, r, schedule, sched_map, points, trace,
                                     float(params.get("qi_reject_factor", 7.0)),
                                     tag, t_label, abort, on_point, on_progress)
    return measure_resonator_spd(im, r, schedule, sched_map, points, trace,
                                 tag, t_label, abort, on_point, on_progress)


# ===========================================================================
# Power-dependent worker
# ===========================================================================

class PowerWorker(QThread):
    """Power-dependent sweep (SPD low->high single run, or HPD high->low per-power)."""
    progress = pyqtSignal(str)
    point_measured = pyqtSignal(dict)
    res_finished = pyqtSignal(dict)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, instrument_manager, resonators, params, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.resonators = resonators
        self.p = dict(params); self.p.setdefault("tag", "PowerDep")
        self._abort = threading.Event()

    def abort(self):
        self._abort.set()

    def run(self):
        try:
            if self.im.pna is None:
                raise RuntimeError("PNA is not connected.")
            schedule = sorted(self.p["schedule"], key=lambda s: s[0])
            sched_map = {round(float(pw), 3): (int(av), int(bw)) for pw, av, bw in schedule}
            results = []
            for r in self.resonators:
                if self._abort.is_set():
                    break
                res = _measure_one(self.im, r, schedule, sched_map, self.p, None,
                                   self._abort, self.point_measured.emit, self.progress.emit)
                results.append(res)
                self.res_finished.emit(res)
            self.finished.emit(results)
        except Exception:
            self.error.emit(traceback.format_exc())


# ===========================================================================
# Temperature- (and power-) dependent worker
# ===========================================================================

class TemperatureWorker(QThread):
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

    def __init__(self, instrument_manager, resonators, params, parent=None):
        super().__init__(parent)
        self.im = instrument_manager
        self.resonators = resonators
        self.p = dict(params); self.p.setdefault("tag", "TempPowerDep")
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
        if im.pna is None:
            raise RuntimeError("PNA is not connected.")
        fridge = im.fridge
        if fridge is None or not fridge.is_connected():
            raise RuntimeError("Fridge is not connected — temperature control unavailable.")

        temps = list(p["temperatures_k"])
        schedule = sorted(p["schedule"], key=lambda s: s[0])
        sched_map = {round(float(pw), 3): (int(av), int(bw)) for pw, av, bw in schedule}
        all_results = []

        for T in temps:
            if self._abort.is_set():
                break
            # ---- set & VERIFY the controller setpoint ---------------------
            self.progress.emit(f"Setting target temperature {format_temp_label(T)} (verifying)…")
            try:
                confirmed = fridge.set_target_temperature(
                    T, tol_k=float(p.get("target_tol_k", 1e-4)))
            except Exception as e:
                self.error.emit(f"Target temperature not verified at "
                                f"{format_temp_label(T)}: {e}")
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
                res = _measure_one(im, r, schedule, sched_map, p, t_label,
                                   self._abort, self.point_measured.emit, self.progress.emit)
                res["target_k"] = float(T)
                temp_results.append(res)
            self.temp_finished.emit({"target_k": float(T), "t_label": t_label,
                                     "results": temp_results})
            all_results.append({"target_k": float(T), "results": temp_results})

        self.finished.emit(all_results)
