"""
core/settings.py
================
Persistent, app-wide settings store.

Why this exists
---------------
A core requirement of the platform: *"whenever a user adjusts values and runs,
the new values should become the default of the software"*.  This module is the
single place where that promise is kept.  Every window reads its initial widget
values from here on construction and writes them back the moment the user
commits an action (Run / Apply / Connect).  Because everything goes through one
JSON file, defaults survive restarts and are shared across windows.

Design
------
* A flat-ish nested dict persisted as JSON next to the user's config dir
  (``%APPDATA%\\QResPlatform\\settings.json`` on Windows, ``~/.config/...`` on
  Linux/Mac).  Falls back to the working directory if that path is not writable.
* Dotted-key access: ``settings.get("pna.if_bw", 1000)`` /
  ``settings.set("pna.if_bw", 500)``.
* ``remember(prefix, mapping)`` is the convenience the UI uses after a Run:
  it stores a whole block of widget values at once and flushes to disk.
* Writes are debounced-on-demand: callers decide when to ``flush()``.  ``set``
  with ``autosave=True`` (the default) flushes immediately, which is what you
  want for "this is now the default".

This module deliberately has **no Qt / qcodes dependency** so it can be imported
and unit-tested anywhere, including this sandbox.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from typing import Any, Dict, Mapping, Optional


# ---------------------------------------------------------------------------
# Where to put the file
# ---------------------------------------------------------------------------

APP_DIR_NAME = "QResPlatform"
SETTINGS_FILENAME = "settings.json"


def _config_dir() -> str:
    """Return a per-user, writable directory for the settings file."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
        # confirm writable
        testfile = os.path.join(path, ".write_test")
        with open(testfile, "w") as fh:
            fh.write("ok")
        os.remove(testfile)
        return path
    except Exception:
        # Fall back to a temp dir so the app never crashes on a read-only home.
        fallback = os.path.join(tempfile.gettempdir(), APP_DIR_NAME)
        os.makedirs(fallback, exist_ok=True)
        return fallback


# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------
#
# These are the *factory* defaults.  User edits are layered on top and win.
# Keep this in sync with the widgets so a fresh install behaves sensibly.

FACTORY_DEFAULTS: Dict[str, Any] = {
    "app": {
        "last_fridge": "",          # "proteox" | "teslatron" | "other"
        "last_db_path": "",
        "sample_name": "",
        "export_dir": "",           # analysis export folder (per-db, see analysis window)
        "export_base_name": "",
    },
    "network": {
        # Editable IP addresses with permanent-save support.
        "pna_address": "TCPIP0::192.168.1.151::5025::SOCKET",
        "teslatron_address": "TCPIP0::192.168.1.100::7020::SOCKET",
        # Proteox is brokered by the DECS-VISA subprocess; HOST/PORT live in the
        # decs_visa_settings module, but we mirror them here so the UI can edit.
        "proteox_host": "127.0.0.1",
        "proteox_port": 33576,
    },
    "pna": {
        "start_ghz": 4.0,
        "stop_ghz": 7.0,
        "points": 100001,
        "if_bw": 1000,
        "power_dbm": -30.0,
        "averages": 1,
        "avg_enabled": False,
        "trigger_continuous": False,
        "max_points": 100001,       # hardware ceiling for the PNA
        "inline_attenuation_db": 80,  # for measurement-name labelling
    },
    "wideband": {
        "wait_for_ref_temp": False,
        "ref_temp_name": "",         # set per fridge, e.g. "Mixing Chamber"
        "ref_temp_target_k": 0.015,  # 15 mK proteox default; 1.5 K teslatron
        "poll_interval_s": 3600,     # check once an hour
    },
    "span_picker": {
        "default_span_mhz": 2.0,
    },
    "quality": {
        "assess_power_dbm": -10.0,
        "assess_if_bw": 1000,
        "assess_averages": 1,
    },
    "resonators": [],   # curated list survives between sessions (list of dicts)
}


# ---------------------------------------------------------------------------
# Settings object
# ---------------------------------------------------------------------------


class Settings:
    """Thread-safe dotted-key JSON settings with factory-default fallback."""

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self.path = path or os.path.join(_config_dir(), SETTINGS_FILENAME)
        self._data: Dict[str, Any] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        self._data = json.load(fh)
                except Exception:
                    # Corrupt file -> start clean but keep a backup.
                    try:
                        os.replace(self.path, self.path + ".corrupt")
                    except Exception:
                        pass
                    self._data = {}
            else:
                self._data = {}

    def flush(self) -> None:
        """Atomically write the current state to disk."""
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, default=_json_safe)
                os.replace(tmp, self.path)
            except Exception:
                # Never let a settings write take down the app.
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

    # -- dotted-key access --------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Resolve ``a.b.c`` against user data, then factory defaults."""
        with self._lock:
            val = _dig(self._data, key, _MISSING)
            if val is not _MISSING:
                return val
            val = _dig(FACTORY_DEFAULTS, key, _MISSING)
            if val is not _MISSING:
                return val
            return default

    def set(self, key: str, value: Any, autosave: bool = True) -> None:
        with self._lock:
            _plant(self._data, key, value)
            if autosave:
                self.flush()

    def remember(self, prefix: str, mapping: Mapping[str, Any],
                 autosave: bool = True) -> None:
        """
        Store a whole block of values, e.g. after a Run::

            settings.remember("pna", {
                "start_ghz": 4.0, "stop_ghz": 7.0, "if_bw": 1000, ...
            })

        These become the new defaults the widgets load next time.
        """
        with self._lock:
            for k, v in mapping.items():
                _plant(self._data, f"{prefix}.{k}" if prefix else k, v)
            if autosave:
                self.flush()

    def block(self, prefix: str) -> Dict[str, Any]:
        """Return a merged (factory <- user) dict for a whole prefix block."""
        merged: Dict[str, Any] = {}
        fac = _dig(FACTORY_DEFAULTS, prefix, {})
        usr = _dig(self._data, prefix, {})
        if isinstance(fac, dict):
            merged.update(fac)
        if isinstance(usr, dict):
            merged.update(usr)
        return merged

    # -- raw access for things like the resonator list ----------------------

    def get_list(self, key: str) -> list:
        val = self.get(key, [])
        return list(val) if isinstance(val, (list, tuple)) else []

    def set_list(self, key: str, value: list, autosave: bool = True) -> None:
        self.set(key, list(value), autosave=autosave)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_MISSING = object()


def _dig(d: Any, dotted: str, default: Any) -> Any:
    cur = d
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _plant(d: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _json_safe(obj: Any) -> Any:
    """Best-effort coercion for numpy scalars etc. so JSON never fails."""
    try:
        import numpy as np  # local import; optional
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:
        pass
    return str(obj)


# Global shared instance -- import this everywhere.
settings = Settings()


if __name__ == "__main__":
    # tiny smoke test
    s = Settings(path=os.path.join(tempfile.gettempdir(), "qres_smoke.json"))
    assert s.get("pna.points") == 100001
    s.set("pna.points", 50001)
    assert s.get("pna.points") == 50001
    s.remember("pna", {"if_bw": 500, "power_dbm": -25.0})
    assert s.get("pna.if_bw") == 500
    s2 = Settings(path=s.path)  # reload from disk
    assert s2.get("pna.if_bw") == 500
    print("settings.py smoke test passed ->", s.path)
