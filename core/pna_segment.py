"""
core/pna_segment.py
===================
HPD (homophasal point distribution) support for the Keysight N5245A via
segment sweep.

Two parts:
  1. `build_hpd_segments(...)` — pure math. Given fr, Ql, theta0 and a target
     span + point count, produce a set of contiguous, linearly-spaced segments
     whose local density approximates the homophasal distribution (dense near
     fr, sparse in the wings). Auto-chooses the segment count from the point
     count. This was validated against circuit.py in the standalone demo.
  2. SCPI helpers to program the segment table on the PNA, read back the actual
     (non-uniform) stimulus axis, and restore a normal linear sweep.

NOTE ON SCPI: segment-sweep command spelling varies slightly across PNA
firmware. The commands below follow the documented N52xx PNA-X form. They are
centralised here so they are trivial to adjust on your instrument; if
programming fails, the worker falls back to a linear (SPD) sweep for that point
and logs a warning rather than aborting.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Pure math: build HPD-approximating segments
# ---------------------------------------------------------------------------

def hpd_frequencies(fr: float, Ql: float, span_hz: float, n_points: int,
                    theta0: float = 0.0) -> np.ndarray:
    """Ideal homophasal frequencies across the requested span (Hz)."""
    Ql = max(float(Ql), 1.0)
    th_edge = 2.0 * np.arctan(Ql * span_hz / fr)
    theta = np.linspace(-th_edge, th_edge, int(n_points)) + theta0
    f = fr * (1.0 - np.tan((theta - theta0) / 2.0) / (2.0 * Ql))
    return np.sort(f)


def auto_segment_count(n_points: int) -> int:
    """Sensible number of segments for a target point count."""
    return int(np.clip(round(n_points / 40.0), 12, 40))


def build_hpd_segments(fr: float, Ql: float, span_hz: float, n_points: int,
                       theta0: float = 0.0
                       ) -> Tuple[List[Tuple[float, float, int]], np.ndarray]:
    """
    Return (segments, realized_frequencies).

    segments: list of (f_start_hz, f_stop_hz, n_points_in_segment), contiguous
    and sorted by frequency. realized_frequencies is the piecewise-linear grid
    the PNA will actually measure (concatenation of per-segment linspaces).
    """
    ideal = hpd_frequencies(fr, Ql, span_hz, n_points, theta0)
    K = auto_segment_count(n_points)
    groups = np.array_split(ideal, K)
    segs: List[Tuple[float, float, int]] = []
    realized: List[np.ndarray] = []
    for grp in groups:
        if len(grp) < 2:
            continue
        f0, f1 = float(grp[0]), float(grp[-1])
        if f1 <= f0:
            continue
        nop = int(len(grp))
        segs.append((f0, f1, nop))
        realized.append(np.linspace(f0, f1, nop))
    return segs, (np.concatenate(realized) if realized else ideal)


# ---------------------------------------------------------------------------
# SCPI helpers (N52xx PNA-X)
# ---------------------------------------------------------------------------

def program_segment_sweep(pna, segments: List[Tuple[float, float, int]],
                          if_bw: int, power_dbm: float) -> None:
    """
    Program a segment sweep on the PNA. Uses a single global IF bandwidth and
    power for all segments (per-segment control is available but not needed for
    HPD). Raises on SCPI error so the caller can fall back to SPD.
    """
    w = pna.write
    w("SENS:SWE:TYPE SEGM")
    # clear any existing segments
    try:
        w("SENS:SEGM:DEL:ALL")
    except Exception:
        pass
    # global IFBW / power for all segments (per-segment control OFF)
    try:
        w("SENS:SEGM:BWID:CONT OFF")
        w("SENS:SEGM:POW:CONT OFF")
    except Exception:
        pass
    pna.if_bandwidth(int(if_bw))
    pna.power(float(power_dbm))
    # add segments 1..N
    for n, (f0, f1, nop) in enumerate(segments, start=1):
        w(f"SENS:SEGM{n}:ADD")
        w(f"SENS:SEGM{n}:STAT ON")
        w(f"SENS:SEGM{n}:FREQ:STAR {f0:.6f}")
        w(f"SENS:SEGM{n}:FREQ:STOP {f1:.6f}")
        w(f"SENS:SEGM{n}:SWE:POIN {int(nop)}")
    # ensure segment data drives the sweep
    w("SENS:SEGM:STAT ON")


def read_segment_stimulus(pna, n_expected: int) -> np.ndarray:
    """
    Read the actual stimulus (frequency) axis for the current (segment) sweep.

    The PNA is configured for binary data (FORM REAL,32), so the stimulus query
    must be read as a binary block — reading it as ASCII raises
    'ascii codec can't decode byte 0xa2'. We try a binary read first, then a
    plain ASCII read (in case the format is ASCii), then fall back to the qcodes
    frequency_axis parameter.
    """
    # 1) binary read (matches FORM REAL,32)
    for query in ("SENS:X?", "SENS:X:VAL?", "CALC:X?"):
        try:
            vals = np.array(pna.visa_handle.query_binary_values(
                query, datatype="f", is_big_endian=True), dtype=float)
            if vals.size:
                return vals
        except Exception:
            pass
    # 2) ASCII read (if the instrument is in ASCii format)
    for query in ("SENS:X?", "SENS:X:VAL?", "CALC:X?"):
        try:
            raw = pna.ask(query)
            vals = np.array([float(x) for x in raw.strip().split(",") if x])
            if vals.size:
                return vals
        except Exception:
            continue
    # last resort: whatever the driver exposes
    try:
        return np.asarray(pna.frequency_axis(), dtype=float)
    except Exception:
        return np.array([])


def restore_linear_sweep(pna) -> None:
    """Return the PNA to a normal linear sweep (called after HPD runs)."""
    try:
        pna.write("SENS:SWE:TYPE LIN")
    except Exception:
        pass
