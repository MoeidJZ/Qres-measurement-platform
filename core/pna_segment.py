"""
core/pna_segment.py
===================
Homophasal point distribution (HPD) for resonator sweeps, implemented as a
PNA segment sweep.

Reference: P. G. Baity et al., "Circle fit optimization for resonator quality
factor measurements: Point redistribution for maximal accuracy",
Phys. Rev. Research 6, 013329 (2024), Sec. IV.

The paper's recipe
------------------
The resonance phase follows (their Sec. IV, from Probst et al.)

    theta(f) = theta0 + 2*arctan(2*Ql*(1 - f/fr))

with inverse

    f(theta) = fr * (1 - tan((theta - theta0)/2) / (2*Ql)).

Taking N equidistant values of theta and mapping them back to frequency puts
the points uniformly around the resonance circle instead of piling them up at
the off-resonant point (their Fig. 3a / Fig. 6a).  Only approximate fr and Ql
are needed; theta0 cancels out of the mapping.

What this module does
---------------------
* ``hpd_frequencies``: the ideal HPD over a *fixed* window [f_start, f_stop]
  (the resonator's chosen span is kept; the window does not shrink).  The
  phase range is theta(f_stop)..theta(f_start), so an off-centre resonance
  is handled exactly.  If the ideal spacing near fr would be finer than the
  PNA's 1 Hz synthesizer resolution, the density is capped there and the
  spare points are pushed outward (the result stays monotonic and spans the
  full window).
* ``build_hpd_segments``: approximates that point set with contiguous linear
  segments, which is what a PNA segment sweep can execute.  Segments are cut
  where the local point spacing changes by more than ``max_ratio`` (default
  1.2), so the realised phase spacing stays within ~±10 % of uniform.  Near
  fr the spacing is almost constant (one wide segment); in the wings it grows
  like 1 + x^2, so more, shorter segments are used there.
* ``phase_uniformity``: a diagnostic — how uniform the realised points are
  around the circle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np

Segment = Tuple[float, float, int]

FREQ_RESOLUTION_HZ = 1.0
MAX_SEGMENTS = 200          # conservative; well within PNA table limits


# ---------------------------------------------------------------------------
# Ideal homophasal distribution
# ---------------------------------------------------------------------------

def _theta(f, fr, Ql):
    """Resonance phase without theta0 (it cancels)."""
    return 2.0 * np.arctan(2.0 * Ql * (1.0 - np.asarray(f, float) / fr))


def _f_of_theta(th, fr, Ql):
    return fr * (1.0 - np.tan(np.asarray(th, float) / 2.0) / (2.0 * Ql))


def hpd_frequencies(f_start: float, f_stop: float, fr: float, Ql: float,
                    n_points: int, resolution: float = FREQ_RESOLUTION_HZ
                    ) -> np.ndarray:
    """
    N frequencies in [f_start, f_stop] equidistant in resonance phase
    (Baity et al. 2024, Sec. IV), ascending, endpoints included.

    If the spacing near fr would drop below ``resolution`` the point density
    is capped at 1/resolution there; the remaining points keep the
    homophasal shape elsewhere.
    """
    f_start, f_stop = float(min(f_start, f_stop)), float(max(f_start, f_stop))
    n = int(n_points)
    if n < 2:
        raise ValueError("need at least 2 points")
    Ql = max(float(Ql), 1.0)
    fr = float(fr)

    # uniform in theta between the two window edges (theta decreases with f)
    th_hi, th_lo = _theta(f_start, fr, Ql), _theta(f_stop, fr, Ql)
    th = np.linspace(th_hi, th_lo, n)
    f = _f_of_theta(th, fr, Ql)
    f[0], f[-1] = f_start, f_stop                 # exact window edges
    f = np.sort(f)

    if resolution and np.min(np.diff(f)) < resolution:
        f = _capped_density(f_start, f_stop, fr, Ql, n, resolution)
    return f


def _capped_density(f_start, f_stop, fr, Ql, n, resolution) -> np.ndarray:
    """
    Point density  rho(f) = min(c * dtheta/df, 1/resolution), with c chosen so
    that the window holds exactly n points; points placed by inverting the
    cumulative count.  Equals the ideal HPD wherever the cap is inactive.
    """
    if (f_stop - f_start) / resolution < n - 1:
        raise ValueError(
            f"{n} points do not fit in {f_stop - f_start:.0f} Hz at "
            f"{resolution:g} Hz resolution; reduce points or widen the span")
    # fine grid that is itself dense near fr (homophasal, 40x oversampled)
    m = max(40 * n, 20001)
    th = np.linspace(_theta(f_start, fr, Ql), _theta(f_stop, fr, Ql), m)
    grid = np.unique(np.concatenate([_f_of_theta(th, fr, Ql),
                                     np.linspace(f_start, f_stop, m)]))
    grid = grid[(grid >= f_start) & (grid <= f_stop)]
    x = 2.0 * Ql * (1.0 - grid / fr)
    dth_df = (4.0 * Ql / fr) / (1.0 + x * x)          # |dtheta/df|
    cap = 1.0 / resolution

    def count(c):
        rho = np.minimum(c * dth_df, cap)
        return np.concatenate([[0.0], np.cumsum(0.5 * (rho[1:] + rho[:-1]) * np.diff(grid))])

    lo, hi = 0.0, 1.0
    while count(hi)[-1] < n - 1:
        hi *= 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if count(mid)[-1] < n - 1:
            lo = mid
        else:
            hi = mid
    cum = count(hi)
    cum *= (n - 1) / cum[-1]
    f = np.interp(np.arange(n, dtype=float), cum, grid)
    f[0], f[-1] = f_start, f_stop
    return f


# ---------------------------------------------------------------------------
# Segment approximation
# ---------------------------------------------------------------------------

@dataclass
class HPDPlan:
    segments: List[Segment]               # (start_hz, stop_hz, points)
    frequencies: np.ndarray               # what the PNA will measure (rounded to 1 Hz)
    ideal: np.ndarray                     # ideal HPD frequencies
    fr: float
    Ql: float
    span_over_linewidth: float
    uniformity: float                     # std/mean of phase steps (0 = perfect)
    capped: bool                          # 1 Hz resolution cap was active
    notes: List[str] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return int(sum(n for _, _, n in self.segments))


def _segment_breaks(f: np.ndarray, max_ratio: float) -> List[Tuple[int, int]]:
    """Greedy split of point indices so each segment's step sizes stay within max_ratio."""
    steps = np.diff(f)
    out, i, n = [], 0, f.size
    while i < n - 1:
        lo = hi = steps[i]
        j = i + 1                     # segment covers points i..j
        while j < n - 1:
            s = steps[j]
            nlo, nhi = min(lo, s), max(hi, s)
            if nhi / nlo > max_ratio:
                break
            lo, hi = nlo, nhi
            j += 1
        out.append((i, j))
        i = j + 1                     # next segment starts at the following point
    if i == n - 1:                    # a lone last point: merge into previous segment
        a, _ = out[-1]
        out[-1] = (a, n - 1)
    return out


def build_hpd_segments(f_start: float, f_stop: float, fr: float, Ql: float,
                       n_points: int, *, max_ratio: float = 1.2,
                       max_segments: int = MAX_SEGMENTS,
                       resolution: float = FREQ_RESOLUTION_HZ) -> HPDPlan:
    """
    Plan an HPD sweep over the fixed window [f_start, f_stop] with n_points.

    fr, Ql: current estimates (from the previous fit). If fr lies outside the
    window it is clamped to the window edge; the result is then still valid,
    just less concentrated.
    """
    f_start, f_stop = float(min(f_start, f_stop)), float(max(f_start, f_stop))
    notes: List[str] = []
    fr_use = float(np.clip(fr, f_start, f_stop))
    if fr_use != fr:
        notes.append(f"fr estimate {fr/1e9:.6f} GHz outside window; clamped to edge")
    Ql = max(float(Ql), 1.0)

    ideal = hpd_frequencies(f_start, f_stop, fr_use, Ql, n_points, resolution)
    th_ideal = np.linspace(_theta(f_start, fr_use, Ql), _theta(f_stop, fr_use, Ql), n_points)
    capped = bool(np.max(np.abs(np.sort(_f_of_theta(th_ideal, fr_use, Ql))[1:-1] - ideal[1:-1]))
                  > max(resolution, 1e-9)) if n_points > 2 else False
    if capped:
        notes.append("1 Hz resolution reached near fr; centre density capped")

    ratio = float(max_ratio)
    while True:
        breaks = _segment_breaks(ideal, ratio)
        if len(breaks) <= max_segments:
            break
        ratio *= 1.1
    if ratio > max_ratio * 1.0001:
        notes.append(f"segment count limited to {max_segments}: step ratio relaxed to {ratio:.2f}")

    segments: List[Segment] = []
    realized = []
    for a, b in breaks:
        f0 = float(np.round(ideal[a] / resolution) * resolution)
        f1 = float(np.round(ideal[b] / resolution) * resolution)
        n = b - a + 1
        if segments and f0 <= segments[-1][1]:
            f0 = segments[-1][1] + resolution
        if n > 1:
            n_fit = int(np.floor((f1 - f0) / resolution + 1e-9)) + 1
            if n_fit < n:
                notes.append(f"segment {f0:.0f}-{f1:.0f} Hz trimmed from {n} to {n_fit} points")
                n = max(n_fit, 1)
        if n == 1:
            f1 = f0
        segments.append((f0, f1, n))
        realized.append(np.round(np.linspace(f0, f1, n) / resolution) * resolution
                        if n > 1 else np.array([f0]))
    freq = np.concatenate(realized)

    return HPDPlan(
        segments=segments, frequencies=freq, ideal=ideal, fr=fr_use, Ql=Ql,
        span_over_linewidth=(f_stop - f_start) * Ql / fr_use,
        uniformity=phase_uniformity(freq, fr_use, Ql),
        capped=capped, notes=notes,
    )


def phase_uniformity(f: np.ndarray, fr: float, Ql: float) -> float:
    """std/mean of the phase steps between consecutive points (0 = perfectly homophasal)."""
    d = np.abs(np.diff(_theta(np.sort(np.asarray(f, float)), fr, Ql)))
    return float(np.std(d) / np.mean(d)) if d.size > 1 else 0.0


# ---------------------------------------------------------------------------
# Instrument helpers (thin wrappers over the extended driver)
# ---------------------------------------------------------------------------

def program_hpd(pna, plan: HPDPlan) -> np.ndarray:
    """Program the plan's segment table; returns the instrument's stimulus axis (Hz)."""
    if not hasattr(pna, "set_segment_table"):
        raise RuntimeError(
            "This PNA driver has no segment-sweep support. Connect with "
            "core.drivers.keysight_pna (KeysightN5235A / KeysightN5245A).")
    return pna.set_segment_table(plan.segments)


def restore_linear_sweep(pna) -> None:
    """Return the PNA to a normal linear sweep (after HPD runs / on errors)."""
    try:
        if hasattr(pna, "use_linear_sweep"):
            pna.use_linear_sweep()
        else:
            pna.write("SENS:SWE:TYPE LIN")
    except Exception:
        pass
