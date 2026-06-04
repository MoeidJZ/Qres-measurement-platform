"""
core/fitting.py
===============
Notch-port circlefit wrapper around the Probst circuit.py. Returns the fit
parameters using Probst's diameter-corrected keys (Qi_dia_corr / absQc /
Qc_dia_corr / Ql / fr / phi0), the raw magnitude/phase/complex arrays for
plotting (data = symbols, fit = line), and the photon number computed by the
port's own get_photons_in_resonator() so the value matches Probst_GUI_V3 exactly.

Reading is tolerant of both the full Probst circuit.py and the reduced copy that
shipped in the measurement zip (different key names), so quality and analysis
fits agree regardless of which file is present.
"""

from __future__ import annotations

from typing import Optional, Dict

import numpy as np

try:
    from scipy.constants import hbar as _HBAR
except Exception:
    _HBAR = 1.054571817e-34


def s21_from_mag_phase(mag_db, phase_deg) -> np.ndarray:
    mag = np.asarray(mag_db, dtype=float)
    ph = np.asarray(phase_deg, dtype=float)
    return 10.0 ** (mag / 20.0) * np.exp(1j * ph * np.pi / 180.0)


def _import_notch_port():
    try:
        from circuit import notch_port
        return notch_port
    except Exception:
        from resonator_tools.circuit import notch_port
        return notch_port


def _g(res: dict, *keys, default=None):
    for k in keys:
        if k in res and res[k] is not None:
            return res[k]
    return default


def fit_notch(f_hz, z, *, crop_hz: Optional[tuple] = None,
              gaussian_sigma: Optional[float] = None) -> Dict:
    """Circlefit a notch resonance; crop_hz=(lo,hi) restricts the fit range."""
    f = np.asarray(f_hz, dtype=float)
    zc = np.asarray(z, dtype=complex)

    if crop_hz is not None:
        lo, hi = min(crop_hz), max(crop_hz)
        m = (f >= lo) & (f <= hi)
        if m.sum() >= 10:
            f, zc = f[m], zc[m]

    if gaussian_sigma and gaussian_sigma > 0:
        try:
            from scipy.ndimage import gaussian_filter1d
            zc = (gaussian_filter1d(zc.real, gaussian_sigma)
                  + 1j * gaussian_filter1d(zc.imag, gaussian_sigma))
        except Exception:
            pass

    out: Dict = {
        "ok": False, "error": "",
        "f_hz": f, "mag_db": 20.0 * np.log10(np.abs(zc) + 1e-30),
        "z_raw": zc,
        "f_sim_hz": None, "mag_sim_db": None, "phase_sim_deg": None,
        "z_sim_raw": None,
        "fr": None, "Qi": None, "Qi_err": None, "Qc": None, "Qc_err": None,
        "absQc": None, "Qc_dia_corr": None, "Ql": None, "Ql_err": None,
        "phi": 0.0, "theta0": 0.0, "chi_square": None, "photons": None,
        "_port": None,
    }
    try:
        notch_port = _import_notch_port()
        port = notch_port(f, zc)
        port.autofit()
        res = port.fitresults

        Qi = float(_g(res, "Qi_dia_corr", "Qi"))                 # diameter-corrected Qi
        absQc = float(_g(res, "absQc", "Qc_no_dia_corr"))         # |Qc|
        Qc_dia = float(_g(res, "Qc_dia_corr", "Qc", default=absQc))
        Ql = float(res["Ql"]); fr = float(res["fr"])
        phi0 = float(_g(res, "phi0", "phi", default=0.0))
        if not (Qi > 0 and Ql > 0):
            raise ValueError(f"Invalid Q (Qi={Qi}, Ql={Ql})")

        z_sim = np.asarray(port.z_data_sim, dtype=complex)
        out.update({
            "ok": True,
            "fr": fr, "Qi": Qi, "Qc": absQc, "absQc": absQc, "Qc_dia_corr": Qc_dia,
            "Ql": Ql, "phi": phi0, "theta0": float(_g(res, "theta0", "theta", default=0.0)),
            "Qi_err": float(_g(res, "Qi_dia_corr_err", "Qi_err", default=0.0)),
            "Qc_err": float(_g(res, "absQc_err", "Qc_err", default=0.0)),
            "Ql_err": float(_g(res, "Ql_err", default=0.0)),
            "fr_err": float(_g(res, "fr_err", default=0.0)),
            "chi_square": float(_g(res, "chi_square", default=0.0)),
            "f_sim_hz": f,
            "mag_sim_db": 20.0 * np.log10(np.abs(z_sim) + 1e-30),
            "phase_sim_deg": np.degrees(np.angle(z_sim)),
            "z_sim_raw": z_sim,
            "_port": port,
        })
    except Exception as e:
        out["error"] = str(e)
    return out


def photons_in_resonator(fit: Dict, chip_power_dbm: float) -> float:
    """
    Photon number for a given on-chip power (dBm). Uses the fitted port's own
    get_photons_in_resonator (Probst, diacorr) when available, so the value is
    identical to Probst_GUI_V3; falls back to the same k_c/k_i formula otherwise.
    """
    if not fit.get("ok"):
        return float("nan")
    port = fit.get("_port")
    if port is not None and hasattr(port, "get_photons_in_resonator"):
        try:
            return float(port.get_photons_in_resonator(chip_power_dbm, unit="dBm"))
        except Exception:
            pass
    # fallback: identical formula using dia-corrected Q's
    try:
        fr = fit["fr"]; Qc = fit.get("Qc_dia_corr") or fit.get("absQc"); Qi = fit["Qi"]
        p_w = 1e-3 * 10.0 ** (chip_power_dbm / 10.0)
        k_c = 2 * np.pi * fr / Qc
        k_i = 2 * np.pi * fr / Qi
        return float(4.0 * k_c / (2 * np.pi * _HBAR * fr * (k_c + k_i) ** 2) * p_w)
    except Exception:
        return float("nan")


def add_photons(fit: Dict, chip_power_dbm) -> Dict:
    """Attach photon number (chip_power_dbm = VNA power + attenuation[dB, negative])."""
    if fit.get("ok") and chip_power_dbm == chip_power_dbm:
        fit["photons"] = photons_in_resonator(fit, chip_power_dbm)
    return fit


def format_q(q) -> str:
    if q is None or not np.isfinite(q):
        return "—"
    if q >= 1e6:
        return f"{q/1e6:.3f}M"
    if q >= 1e3:
        return f"{q/1e3:.2f}k"
    return f"{q:.0f}"


def format_photons(n) -> str:
    if n is None or not np.isfinite(n):
        return "—"
    return f"{n:.3g}"
