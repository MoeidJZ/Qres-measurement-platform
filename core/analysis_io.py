"""
core/analysis_io.py
===================
Read QCoDeS runs from a .db (the live one being written, or any other) and
export fit results with the required naming scheme.

Handles both storage layouts produced by this app:
  * SPD multi-power runs  -> 'pna_tr1_magnitude' / 'pna_tr1_phase' with
    setpoints 'pna_power' and 'pna_frequency_axis' (matches the existing
    circuit.analyze_qi_vs_power layout);
  * HPD / quality runs     -> custom 'mag' / 'phase' / 'frequency' / 'power'.

Listing runs is done by reading the SQLite 'runs' table directly (stable across
qcodes versions and safe to do on a database another process is writing, since
SQLite allows concurrent readers). Loading a run's data uses qcodes so the blob
format is parsed correctly; a dedicated connection is used when available so we
never disturb the measurement writer's global database path.
"""

from __future__ import annotations

import os
import re
import sqlite3
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run listing (direct sqlite read; concurrency-safe)
# ---------------------------------------------------------------------------

def list_runs(db_path: str) -> List[Dict]:
    """Return [{run_id, name, timestamp}] newest first, via a read-only query."""
    runs: List[Dict] = []
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        conn = sqlite3.connect(db_path, timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT run_id, name, run_timestamp FROM runs ORDER BY run_id DESC")
        for rid, name, ts in cur.fetchall():
            runs.append({"run_id": int(rid), "name": name or f"run_{rid}",
                         "timestamp": ts})
    except Exception:
        logger.exception("Failed to list runs from %s", db_path)
    finally:
        conn.close()
    return runs


# ---------------------------------------------------------------------------
# Run loading (qcodes)
# ---------------------------------------------------------------------------

def _connect(db_path: str):
    for mod in ("qcodes.dataset.sqlite.database",
                "qcodes.dataset.sqlite.connection",
                "qcodes.dataset"):
        try:
            m = __import__(mod, fromlist=["connect"])
            if hasattr(m, "connect"):
                return m.connect(db_path)
        except Exception:
            continue
    return None


def _load_parameter_data(db_path: str, run_id: int) -> Tuple[dict, str]:
    try:
        from qcodes.dataset import load_by_id
    except Exception:
        from qcodes.dataset.data_set import load_by_id  # type: ignore
    conn = _connect(db_path)
    if conn is not None:
        ds = load_by_id(run_id, conn=conn)
    else:
        # Fallback: point qcodes at this db (only affects reads here).
        from qcodes.dataset import initialise_or_create_database_at
        initialise_or_create_database_at(db_path)
        ds = load_by_id(run_id)
    return ds.get_parameter_data(), getattr(ds, "name", f"run_{run_id}")


def load_run(db_path: str, run_id: int) -> Dict:
    """
    Return a normalised dict:
        {kind, name, powers(list[float]), freq(1d Hz),
         mag(2d [npow,nfreq] dB), phase(2d deg)}
    For single-power runs npow == 1.
    """
    pdata, name = _load_parameter_data(db_path, run_id)

    # ---- SPD multi-power layout (shared frequency axis) ------------
    if "pna_tr1_magnitude" in pdata:
        block = pdata["pna_tr1_magnitude"]
        powers = np.atleast_2d(block["pna_power"])[:, 0]
        mag = np.atleast_2d(block["pna_tr1_magnitude"])
        freq = np.atleast_2d(block["pna_frequency_axis"])[0, :]
        phase = np.atleast_2d(pdata["pna_tr1_phase"]["pna_tr1_phase"])
        freqs = np.tile(np.asarray(freq, float), (mag.shape[0], 1))
        return {"kind": "spd", "name": name, "powers": [float(p) for p in powers],
                "freq": np.asarray(freq, float), "freqs": freqs,
                "mag": np.asarray(mag, float),
                "phase": np.asarray(phase, float), "run_id": run_id}

    # ---- HPD / custom layout (frequency stored per power) ----------
    if "mag" in pdata:
        mb = pdata["mag"]
        mag = np.asarray(mb.get("mag"), float)
        pw = np.asarray(mb.get("power"), float)
        freq = np.asarray(pdata.get("frequency", {}).get("frequency", []), float)
        phase = np.asarray(pdata.get("phase", {}).get("phase", []), float)

        if mag.ndim == 2:                       # gridded (n_powers, n_points)
            mag2d = mag
            npow, npoint = mag2d.shape
            powers = pw[:, 0] if pw.ndim == 2 else pw.reshape(npow, npoint)[:, 0]
            freq2d = (freq if (freq.ndim == 2 and freq.shape == mag2d.shape)
                      else (freq.reshape(npow, npoint) if freq.size == mag2d.size
                            else np.tile(freq.ravel(), (npow, 1))))
            phase2d = (phase if (phase.ndim == 2 and phase.shape == mag2d.shape)
                       else (phase.reshape(npow, npoint) if phase.size == mag2d.size
                             else np.zeros_like(mag2d)))
        else:                                   # flattened — group power-major
            uniq = []
            for p in (pw.tolist() if pw.size else []):
                if not uniq or uniq[-1] != p:
                    uniq.append(float(p))
            npow = max(len(uniq), 1)
            npoint = mag.size // npow
            mag2d = mag.reshape(npow, npoint)
            freq2d = (freq.reshape(npow, npoint) if freq.size == mag.size
                      else np.tile(freq.ravel(), (npow, 1)))
            phase2d = (phase.reshape(npow, npoint) if phase.size == mag.size
                       else np.zeros_like(mag2d))
            powers = np.array(uniq) if uniq else np.arange(npow, dtype=float)
        return {"kind": "hpd", "name": name, "powers": [float(p) for p in powers],
                "freq": freq2d[0], "freqs": freq2d, "mag": mag2d,
                "phase": phase2d, "run_id": run_id}

    # ---- Generic fallback: find first magnitude-like dependent -----
    for dep, block in pdata.items():
        arrs = {k: np.asarray(v, float) for k, v in block.items()}
        mag_key = next((k for k in arrs if "mag" in k.lower()), None)
        f_key = next((k for k in arrs if "freq" in k.lower()), None)
        if mag_key and f_key:
            mag = np.atleast_2d(arrs[mag_key])
            freq = np.atleast_2d(arrs[f_key])
            freq1d = freq[0, :] if freq.ndim == 2 else freq
            freqs = freq if freq.shape == mag.shape else np.tile(freq1d, (mag.shape[0], 1))
            return {"kind": "generic", "name": name,
                    "powers": [float("nan")] * mag.shape[0],
                    "freq": freq1d, "freqs": freqs, "mag": mag,
                    "phase": np.zeros_like(mag), "run_id": run_id}
    raise ValueError(f"Run {run_id}: could not recognise data layout "
                     f"(parameters: {list(pdata.keys())}).")


def trace_at(loaded: Dict, i: int):
    """Return (freq_hz, mag_db, phase_deg) for power index i, handling both
    shared and per-power frequency axes."""
    freqs = loaded.get("freqs")
    if freqs is not None:
        freqs = np.asarray(freqs, float)
        f = freqs[i] if freqs.ndim == 2 else freqs
    else:
        f = np.asarray(loaded["freq"], float)
    return f, np.asarray(loaded["mag"][i], float), np.asarray(loaded["phase"][i], float)


def iter_traces(loaded: Dict):
    """Yield (power, freq_hz, mag_db, phase_deg) for each power in a loaded run."""
    for i, pw in enumerate(loaded["powers"]):
        f, mag, phase = trace_at(loaded, i)
        yield pw, f, mag, phase


# ---------------------------------------------------------------------------
# Name parsing -> resonator / temperature / power tags
# ---------------------------------------------------------------------------

def parse_run_name(name: str) -> Dict[str, str]:
    res = re.search(r"Res(\d+)", name or "")
    pw = re.search(r"(-?\d+(?:\.\d+)?)dBm", name or "")
    temp = re.search(r"_(\d+(?:\.\d+)?(?:mK|K))(?:_|$)", name or "")
    return {
        "res": f"Res{res.group(1)}" if res else "Res",
        "temp": temp.group(1) if temp else "unkT",
        "power": (pw.group(1) + "dBm") if pw else "",
    }


def res_num_from_name(name: str):
    m = re.search(r"Res(\d+)", name or "")
    return int(m.group(1)) if m else None


def quality_runs(db_path: str) -> List[Dict]:
    """Quality runs in a database (newest first), with parsed resonator number."""
    out = []
    for r in list_runs(db_path):
        if "_Quality_" in (r.get("name") or ""):
            rr = dict(r); rr["num"] = res_num_from_name(r["name"])
            out.append(rr)
    return out


def run_as_resonator(db_path: str, run_id: int) -> Dict:
    """Load a (single-power) quality run as a resonator dict ready for fitting."""
    ld = load_run(db_path, run_id)
    freq = np.asarray(ld["freq"], float)
    mag = np.asarray(ld["mag"][0], float)
    phase = np.asarray(ld["phase"][0], float)
    return {
        "num": res_num_from_name(ld["name"]),
        "name": ld["name"], "run_id": run_id,
        "center_hz": float((freq[0] + freq[-1]) / 2.0),
        "fstart_hz": float(min(freq[0], freq[-1])),
        "fstop_hz": float(max(freq[0], freq[-1])),
        "span_mhz": float(abs(freq[-1] - freq[0]) / 1e6),
        "points": int(freq.size),
        "f_hz": freq, "mag_db": mag, "phase_deg": phase,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

import re as _re


def _safe_filename(name: str) -> str:
    """Replace characters illegal in Windows filenames (e.g. ? from an
    unreadable-temperature label) so CSV export never fails."""
    return _re.sub(r'[<>:"/\\|?*]', "", name)


def export_trace(export_dir: str, base_name: str, run_name: str, power_dbm,
                 freq_hz, mag_db, phase_deg, fit: Optional[dict] = None) -> str:
    """
    Write one trace to <base>_<Res>_<T>_<P>.csv (frequency, mag, phase[, sim]).
    Returns the file path.
    """
    os.makedirs(export_dir, exist_ok=True)
    tags = parse_run_name(run_name)
    p_tag = tags["power"] or (f"{power_dbm:g}dBm" if power_dbm == power_dbm else "")
    parts = [base_name, tags["res"], tags["temp"]] + ([p_tag] if p_tag else [])
    fname = _safe_filename("_".join(p for p in parts if p) + ".csv")
    path = os.path.join(export_dir, fname)

    cols = [np.asarray(freq_hz, float), np.asarray(mag_db, float),
            np.asarray(phase_deg, float)]
    header = "frequency_Hz,magnitude_dB,phase_deg"
    if fit and fit.get("ok") and fit.get("mag_sim_db") is not None \
            and len(fit["mag_sim_db"]) == len(freq_hz):
        cols.append(np.asarray(fit["mag_sim_db"], float))
        header += ",magnitude_sim_dB"
    np.savetxt(path, np.column_stack(cols), delimiter=",", header=header,
               comments="")
    return path


METRICS_HEADER = ("run_id,run_name,resonator,temperature,power_dBm,"
                  "fr_Hz,Qi,Qi_err,Qc,Ql,phi_rad,n_photons,ok\n")


def append_metrics(export_dir: str, base_name: str, run_id, run_name,
                   power_dbm, fit: dict) -> str:
    """Append one fit-result row to <base>_fitting_parameters.csv."""
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, f"{base_name}_fitting_parameters.csv")
    tags = parse_run_name(run_name)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(METRICS_HEADER)
        fh.write(",".join(str(x) for x in [
            run_id, run_name, tags["res"], tags["temp"],
            (f"{power_dbm:g}" if power_dbm == power_dbm else ""),
            _fmt(fit.get("fr")), _fmt(fit.get("Qi")), _fmt(fit.get("Qi_err")),
            _fmt(fit.get("Qc")), _fmt(fit.get("Ql")), _fmt(fit.get("phi")),
            _fmt(fit.get("photons")), bool(fit.get("ok")),
        ]) + "\n")
    return path


def _fmt(x):
    return "" if x is None else (f"{x:.8g}" if isinstance(x, (int, float)) else str(x))
