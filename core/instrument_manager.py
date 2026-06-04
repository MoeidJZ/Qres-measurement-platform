"""
core/instrument_manager.py
==========================
Central, shared state for the whole application: the live fridge backend, the
PNA, the QCoDeS station, and the active database.  Every window imports the
single ``instrument_manager`` instance from here.

Responsibilities
----------------
* Own the chosen ``FridgeBackend`` (Proteox or Teslatron).
* Own the PNA (optional -- the app is usable without it; PNA-dependent buttons
  are greyed out until ``pna`` is set).
* Provide IP-address override with a "make permanent?" path that writes back to
  the persistent settings store.
* Expose a ``busy`` flag + listener hooks so the UI can grey out controls while
  a measurement is running (the "always accessible, just greyed when busy"
  requirement).
* Keep all heavy imports lazy so this module imports without qcodes present.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from core.settings import settings
from core.fridge import FridgeBackend, make_fridge

logger = logging.getLogger(__name__)


class InstrumentManager:
    def __init__(self):
        self.fridge: Optional[FridgeBackend] = None
        self.fridge_kind: str = ""
        self.pna = None
        self.station = None
        self.db_path: str = settings.get("app.last_db_path", "")
        self.sample_name: str = settings.get("app.sample_name", "")

        self._busy: bool = False
        self._busy_listeners: List[Callable[[bool], None]] = []

    # ------------------------------------------------------------------
    # Busy state -- UI greys controls based on this
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            for cb in list(self._busy_listeners):
                try:
                    cb(value)
                except Exception:
                    logger.exception("busy listener failed")

    def on_busy_changed(self, callback: Callable[[bool], None]) -> None:
        self._busy_listeners.append(callback)

    # ------------------------------------------------------------------
    # Fridge
    # ------------------------------------------------------------------

    def select_fridge(self, kind: str) -> FridgeBackend:
        """Create (but do not connect) the backend for the chosen cryostat."""
        if self.fridge is not None and self.fridge_kind != kind:
            self.fridge.close()
            self.fridge = None
        self.fridge_kind = kind
        self.fridge = make_fridge(kind)
        settings.set("app.last_fridge", kind)
        return self.fridge

    def connect_fridge(self, **kwargs) -> FridgeBackend:
        """
        Connect the previously selected fridge.

        For Proteox pass ``confirm_callback=<callable>`` -- it will be invoked
        while the driver waits for the DECS error-popup acknowledgement and must
        block until the user confirms in the GUI.

        For Teslatron the address is taken from settings
        (``network.teslatron_address``) unless overridden via ``address=``.
        """
        if self.fridge is None:
            raise RuntimeError("No fridge selected. Call select_fridge() first.")
        if self.fridge_kind == "teslatron" and "address" not in kwargs:
            kwargs["address"] = settings.get("network.teslatron_address")
        self.fridge.connect(**kwargs)
        return self.fridge

    def fridge_connected(self) -> bool:
        return self.fridge is not None and self.fridge.is_connected()

    # ------------------------------------------------------------------
    # PNA  (optional)
    # ------------------------------------------------------------------

    def connect_pna(self, address: Optional[str] = None):
        import qcodes as qc
        from qcodes.instrument_drivers.Keysight import KeysightN5245A

        address = address or settings.get("network.pna_address")

        if self.pna is not None:
            try:
                self.pna.close()
            except Exception:
                pass
            self.pna = None
        if "pna" in qc.Instrument._all_instruments:
            try:
                qc.Instrument._all_instruments["pna"].close()
            except Exception:
                pass

        self.pna = KeysightN5245A("pna", address)
        self._patch_pna_quirks(self.pna)
        return self.pna

    @staticmethod
    def _patch_pna_quirks(pna):
        """
        Some PNA firmwares answer on/off status queries (e.g. SENS:AVER:STAT?)
        with '+0' / '+1'. The stock qcodes val_mapping only knows 0/1, so
        *reading* those parameters — which the magnitude getter does internally —
        raises  KeyError("'+0' not in val_mapping").  Replace the inverse map of
        the boolean parameters with one that accepts every form the instrument
        might return.
        """
        onoff = {
            0: False, 1: True, "0": False, "1": True,
            "+0": False, "+1": True, "+0\n": False, "+1\n": True,
            "OFF": False, "ON": True, "off": False, "on": True,
            False: False, True: True,
        }
        for name in ("averages_enabled", "output", "rf_output",
                     "averaging_enabled", "sweep_mode_hold"):
            p = getattr(pna, name, None)
            if p is None:
                continue
            if getattr(p, "val_mapping", None) or getattr(p, "inverse_val_mapping", None):
                try:
                    p.inverse_val_mapping = dict(onoff)
                except Exception:
                    pass

    def pna_connected(self) -> bool:
        return self.pna is not None

    # ------------------------------------------------------------------
    # IP / address override with optional permanent save
    # ------------------------------------------------------------------

    def update_address(self, which: str, new_address, make_permanent: bool) -> None:
        """
        Update an instrument address.

        ``which`` is one of: "pna", "teslatron", "proteox_host", "proteox_port".
        When ``make_permanent`` is True the value is written to the settings
        file so it becomes the default next launch; otherwise it is only used
        for the current session (we still set it, just without persisting).

        The UI flow is: try to connect -> fail -> let the user edit the address
        -> retry -> on success ask "make this permanent?" -> call this with the
        user's answer.
        """
        key_map = {
            "pna": "network.pna_address",
            "teslatron": "network.teslatron_address",
            "proteox_host": "network.proteox_host",
            "proteox_port": "network.proteox_port",
        }
        if which not in key_map:
            raise ValueError(f"Unknown address target {which!r}")
        # autosave=make_permanent: when not permanent we keep it in the live
        # settings object for this session but do not flush to disk.
        settings.set(key_map[which], new_address, autosave=make_permanent)

    # ------------------------------------------------------------------
    # Station / database
    # ------------------------------------------------------------------

    def setup_station(self):
        import qcodes as qc
        self.station = qc.Station()
        if self.pna is not None:
            self.station.add_component(self.pna)
        if self.fridge is not None and self.fridge.driver is not None:
            try:
                self.station.add_component(self.fridge.driver)
            except Exception:
                pass
        return self.station

    def init_database(self, path: str) -> None:
        from qcodes.dataset import initialise_or_create_database_at
        self.db_path = path
        settings.set("app.last_db_path", path)
        initialise_or_create_database_at(path)

    def set_sample_name(self, name: str) -> None:
        self.sample_name = name
        settings.set("app.sample_name", name)

    # ------------------------------------------------------------------
    # Reference-temperature helpers (used by the wideband ref-temp wait)
    # ------------------------------------------------------------------

    def reference_options(self) -> List[str]:
        return self.fridge.reference_options() if self.fridge_connected() else []

    def read_reference_k(self, label: str) -> Optional[float]:
        if not self.fridge_connected():
            return None
        try:
            t = self.fridge.read_reference(label)
            return t if t > 0 else None
        except Exception:
            return None

    def label_temperature_k(self) -> Optional[float]:
        """
        Best-guess current temperature for labelling a measurement: the fridge's
        control temperature (MC for Proteox, sample/VTI loop for Teslatron, the
        recorded value for Manual mode). Returns None if unreadable.
        """
        if not self.fridge_connected():
            return None
        try:
            t = float(self.fridge._control_temperature())
            return t if (t == t and t > 0) else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        if self.pna is not None:
            try:
                self.pna.close()
            except Exception:
                pass
            self.pna = None
        if self.fridge is not None:
            self.fridge.close()


# Global shared instance.
instrument_manager = InstrumentManager()
