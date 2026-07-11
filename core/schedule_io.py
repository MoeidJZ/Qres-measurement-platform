"""
core/schedule_io.py
===================
Tiny helpers to save/load a power-sweep schedule (the per-power averaging /
IF-bandwidth table) to a JSON file, so the same non-linear power plan can be
reused across sessions and shared between the power- and temperature-dependent
steps.

Schedule format in memory: list of (power_dBm, averages, if_bw_Hz) tuples.
On disk:
    {"kind": "qres_power_schedule", "version": 1,
     "schedule": [[-40, 1, 1000], [-60, 15, 10], ...]}
"""

from __future__ import annotations

import json
from typing import List, Tuple

Schedule = List[Tuple[float, int, int]]


def save_schedule(path: str, schedule: Schedule) -> None:
    rows = [[float(p), int(a), int(b)] for (p, a, b) in schedule]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"kind": "qres_power_schedule", "version": 1, "schedule": rows},
                  fh, indent=2)


def load_schedule(path: str) -> Schedule:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        rows = data.get("schedule", [])
    else:                       # tolerate a bare list
        rows = data
    out: Schedule = []
    for row in rows:
        try:
            p, a, b = row[0], row[1], row[2]
            out.append((float(p), int(round(float(a))), int(round(float(b)))))
        except Exception:
            continue
    if not out:
        raise ValueError("No valid schedule rows found in file.")
    return out
