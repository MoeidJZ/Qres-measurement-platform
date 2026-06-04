"""
core/control_workers.py
=======================
Threaded read/write workers for the instrument-control dock and the PNA
parameter window, so live VISA traffic never blocks the GUI.
"""

from __future__ import annotations

import traceback
from typing import Dict

from PyQt5.QtCore import QThread, pyqtSignal


class ChannelReadWorker(QThread):
    """Read every readable channel of a fridge backend in one pass."""
    result = pyqtSignal(dict)          # {channel_id: value}
    error = pyqtSignal(str)

    def __init__(self, fridge, parent=None):
        super().__init__(parent)
        self.fridge = fridge

    def run(self):
        try:
            self.result.emit(self.fridge.read_all())
        except Exception:
            self.error.emit(traceback.format_exc())


class ChannelWriteWorker(QThread):
    """Write a single settable channel, then read it back for confirmation."""
    done = pyqtSignal(str, bool, str)   # channel_id, ok, message_or_value

    def __init__(self, fridge, channel_id: str, value: float, parent=None):
        super().__init__(parent)
        self.fridge = fridge
        self.channel_id = channel_id
        self.value = value

    def run(self):
        try:
            self.fridge.write(self.channel_id, self.value)
            try:
                back = self.fridge.read(self.channel_id)
                self.done.emit(self.channel_id, True, f"{back:g}")
            except Exception:
                self.done.emit(self.channel_id, True, "set")
        except Exception as e:
            self.done.emit(self.channel_id, False, str(e))


class PNAReadWorker(QThread):
    """Read current PNA settings into a dict for the parameter window."""
    result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, pna, parent=None):
        super().__init__(parent)
        self.pna = pna

    def run(self):
        try:
            p = self.pna
            out = {}
            # Wrap each read so one failing parameter doesn't lose the rest.
            def g(fn, default=None):
                try:
                    return fn()
                except Exception:
                    return default
            out["start_ghz"] = (g(p.start) or 0) / 1e9
            out["stop_ghz"] = (g(p.stop) or 0) / 1e9
            out["points"] = g(p.points)
            out["if_bw"] = g(p.if_bandwidth)
            out["power_dbm"] = g(p.power)
            out["averages"] = g(p.averages)
            out["avg_enabled"] = bool(g(p.averages_enabled))
            out["output"] = bool(g(p.output))
            self.result.emit({k: v for k, v in out.items() if v is not None})
        except Exception:
            self.error.emit(traceback.format_exc())


class PNAWriteWorker(QThread):
    """Apply a settings dict to the PNA."""
    done = pyqtSignal(bool, str)

    def __init__(self, pna, params: Dict, parent=None):
        super().__init__(parent)
        self.pna = pna
        self.params = params

    def run(self):
        try:
            p = self.pna
            d = self.params
            if "trace" in d:
                p.trace(d["trace"])
            if "avg_enabled" in d:
                p.averages_enabled(int(bool(d["avg_enabled"])))
            if "averages" in d:
                p.averages(int(d["averages"]))
            if "start_ghz" in d:
                p.start(float(d["start_ghz"]) * 1e9)
            if "stop_ghz" in d:
                p.stop(float(d["stop_ghz"]) * 1e9)
            if "points" in d:
                p.points(int(d["points"]))
            if "if_bw" in d:
                p.if_bandwidth(int(d["if_bw"]))
            if "power_dbm" in d:
                p.power(float(d["power_dbm"]))
            if "trigger_continuous" in d:
                # continuous trigger ~ auto-sweep on
                try:
                    p.auto_sweep(bool(d["trigger_continuous"]))
                except Exception:
                    pass
            if "output" in d:
                p.output(int(bool(d["output"])))
            self.done.emit(True, "Applied")
        except Exception as e:
            self.done.emit(False, str(e))


class PNASafeStateWorker(QThread):
    """Put the PNA in a low-heat-load safe state: -80 dBm, RF off."""
    done = pyqtSignal(bool, str)

    def __init__(self, pna, parent=None):
        super().__init__(parent)
        self.pna = pna

    def run(self):
        try:
            self.pna.power(-80)
            self.pna.output(0)
            self.done.emit(True, "PNA safe state set (-80 dBm, RF off).")
        except Exception as e:
            self.done.emit(False, str(e))
