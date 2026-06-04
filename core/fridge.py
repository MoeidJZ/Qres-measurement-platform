"""
core/fridge.py
==============
A common abstraction over the two cryostats so the rest of the application
never has to special-case Proteox vs. Teslatron.

The UI talks to a ``FridgeBackend``; the backend wraps the underlying QCoDeS
driver (``oiDECS`` for Proteox, ``MercuryiTC`` for Teslatron).  Each backend
exposes:

* ``channels()``        -> list[Channel]   (drives the always-on control window)
* ``read(channel_id)``  -> float
* ``write(channel_id, value)``             (only for settable channels)
* ``reference_options()`` -> list[str]     (which temps can gate a measurement)
* ``read_reference(name)`` -> float (Kelvin)
* ``set_target_temperature(value_K)``      with **read-back verification**
* ``get_target_temperature()`` -> float
* ``wait_until_target_verified(...)``      fixes the notebook target-mismatch bug
* ``base_temperature_k``                   (15 mK proteox / 1.5 K teslatron)

Heavy imports (qcodes, the drivers) are done lazily *inside* methods, so this
module imports cleanly even where qcodes is not installed (e.g. for testing the
UI logic).  ``connect()`` is the only thing that needs the real hardware.
"""

from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel descriptor (what the control window renders as a row)
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    id: str                 # internal key used by read()/write()
    label: str              # human label, e.g. "Mixing Chamber"
    unit: str               # "K", "W", "Pa", "mB", "%"
    settable: bool = False  # True -> show a setpoint field + Apply
    group: str = "General"  # used to group rows in the control window
    # how to read/write -- bound at construction by the backend
    getter: Optional[Callable[[], float]] = None
    setter: Optional[Callable[[float], None]] = None
    is_reference: bool = False   # can this gate a measurement (ref temperature)?


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class FridgeBackend(ABC):
    kind: str = "generic"
    base_temperature_k: float = 0.0

    def __init__(self, name: str):
        self.name = name
        self.driver = None          # the live qcodes instrument
        self._channels: List[Channel] = []

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    def connect(self, **kwargs) -> None:
        """Open the driver. Raise on failure."""

    def is_connected(self) -> bool:
        return self.driver is not None

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                logger.exception("Error closing %s", self.name)
            self.driver = None

    # -- channel access -----------------------------------------------------

    def channels(self) -> List[Channel]:
        return list(self._channels)

    def _channel(self, channel_id: str) -> Channel:
        for ch in self._channels:
            if ch.id == channel_id:
                return ch
        raise KeyError(f"Unknown channel {channel_id!r} on {self.name}")

    def read(self, channel_id: str) -> float:
        ch = self._channel(channel_id)
        if ch.getter is None:
            raise RuntimeError(f"Channel {channel_id!r} is not readable")
        return float(ch.getter())

    def write(self, channel_id: str, value: float) -> None:
        ch = self._channel(channel_id)
        if not ch.settable or ch.setter is None:
            raise RuntimeError(f"Channel {channel_id!r} is read-only")
        ch.setter(float(value))

    def read_all(self) -> Dict[str, float]:
        """Read every readable channel, tolerating per-channel errors."""
        out: Dict[str, float] = {}
        for ch in self._channels:
            if ch.getter is None:
                continue
            try:
                out[ch.id] = float(ch.getter())
            except Exception:
                out[ch.id] = float("nan")
        return out

    # -- reference temperatures (gate a measurement) ------------------------

    def reference_options(self) -> List[str]:
        return [ch.label for ch in self._channels if ch.is_reference]

    def _ref_channel(self, label: str) -> Channel:
        for ch in self._channels:
            if ch.is_reference and ch.label == label:
                return ch
        raise KeyError(f"{label!r} is not a reference temperature on {self.name}")

    def read_reference(self, label: str) -> float:
        """Reference temperature in Kelvin."""
        return float(self._ref_channel(label).getter())

    # -- target temperature with verification -------------------------------
    #
    # Subclasses implement the raw set/get; this base method wraps them with
    # the read-back-and-verify logic that fixes the notebook bug where the
    # value the controller is actually holding can differ from what the code
    # intended (rounding / latching delay).

    @abstractmethod
    def _set_target_raw(self, value_k: float) -> None: ...

    @abstractmethod
    def _get_target_raw(self) -> float: ...

    def set_target_temperature(self, value_k: float, tol_k: float = 1e-4,
                               retries: int = 5, settle_s: float = 1.0) -> float:
        """
        Set the control loop setpoint and **confirm the controller accepted it**.

        Returns the read-back setpoint actually in effect.  Raises RuntimeError
        if, after ``retries`` attempts, the read-back still disagrees with the
        requested value beyond ``tol_k``.  This is the guard the temperature
        sweep must call before it ever starts waiting for stability.
        """
        last = float("nan")
        for attempt in range(1, retries + 1):
            self._set_target_raw(value_k)
            time.sleep(settle_s)
            try:
                last = self._get_target_raw()
            except Exception:
                last = float("nan")
            if last == last and abs(last - value_k) <= tol_k:  # not NaN and close
                logger.info("%s target verified: requested %.6f K, controller %.6f K",
                            self.name, value_k, last)
                return last
            logger.warning("%s target mismatch (attempt %d/%d): requested %.6f K, "
                           "read back %.6f K -- retrying", self.name, attempt,
                           retries, value_k, last)
        raise RuntimeError(
            f"{self.name}: could not verify target temperature. Requested "
            f"{value_k:.6f} K but controller reports {last:.6f} K after "
            f"{retries} attempts."
        )

    def get_target_temperature(self) -> float:
        return float(self._get_target_raw())

    # -- stability wait -----------------------------------------------------

    @abstractmethod
    def _control_temperature(self) -> float:
        """The temperature the control loop regulates (MC for proteox, etc.)."""

    def wait_until_stable(self, target_k: float, *, stable_mean_k: float = 0.002,
                          stable_std_k: float = 0.002, time_between_readings: float = 5.0,
                          window: int = 30, timeout_s: Optional[float] = None,
                          should_abort: Optional[Callable[[], bool]] = None,
                          on_reading: Optional[Callable[[float], None]] = None) -> None:
        """
        Block until the control temperature is stable around ``target_k``.

        Unlike the original driver helper this:
          * uses the *passed* target (already verified by the caller) rather than
            re-reading the controller setpoint -- so we never wait on a stale or
            rounded number;
          * reads the control temperature consistently (no Sample/MC mix-up);
          * supports cooperative abort and a timeout;
          * reports each reading via ``on_reading`` for live UI feedback.
        """
        import numpy as np

        readings: List[float] = []
        t0 = time.time()
        while True:
            if should_abort and should_abort():
                raise InterruptedError("wait_until_stable aborted by user")
            if timeout_s is not None and (time.time() - t0) > timeout_s:
                raise TimeoutError(
                    f"{self.name}: temperature did not stabilise at "
                    f"{target_k:.4f} K within {timeout_s:.0f} s"
                )
            try:
                t = float(self._control_temperature())
            except Exception:
                time.sleep(time_between_readings)
                continue
            if on_reading:
                on_reading(t)
            readings.append(t)
            if len(readings) > window:
                readings = readings[-window:]
            if len(readings) >= window:
                arr = np.array(readings)
                mean_err = abs(float(arr.mean()) - target_k)
                std = float(arr.std())
                if mean_err < stable_mean_k and std < stable_std_k:
                    logger.info("%s stable at %.4f K (mean_err=%.4g, std=%.4g)",
                                self.name, target_k, mean_err, std)
                    return
            time.sleep(time_between_readings)


# ---------------------------------------------------------------------------
# Proteox backend  (oiDECS driver)
# ---------------------------------------------------------------------------

class ProteoxBackend(FridgeBackend):
    kind = "proteox"
    base_temperature_k = 0.015   # 15 mK

    def connect(self, *, confirm_callback: Optional[Callable[[], None]] = None,
                timeout: int = 100, **kwargs) -> None:
        """
        Connect to Proteox via oiDECS.

        The oiDECS driver blocks on ``input("> ")`` so the user can dismiss any
        DECS error popup before parameters register.  In a GUI there is no
        console, so we temporarily replace ``builtins.input`` with a function
        that calls ``confirm_callback`` (which should block until the user
        clicks "Confirm" in a dialog) and then returns.  If no callback is
        supplied we return immediately (assumes no popup).
        """
        import builtins
        import qcodes as qc
        try:
            from Proteox import oiDECS            # local Proteox.py next to main.py
        except Exception:
            from qcodes_contrib_drivers.drivers.OxfordInstruments.Proteox import oiDECS

        self.close()
        if "Proteox" in qc.Instrument._all_instruments:
            try:
                qc.Instrument._all_instruments["Proteox"].close()
            except Exception:
                pass

        original_input = builtins.input

        def _gui_input(prompt: str = "") -> str:
            logger.info("Proteox connect prompt: %s", prompt.strip())
            if confirm_callback is not None:
                confirm_callback()   # should block until user confirms
            return ""

        # Silence the noisy *IDN? failures the DECS layer logs during connect.
        visa_logger = logging.getLogger("qcodes.instrument.visa")
        prev_level = visa_logger.level
        visa_logger.setLevel(logging.CRITICAL)
        builtins.input = _gui_input
        try:
            self.driver = oiDECS("Proteox")
        finally:
            builtins.input = original_input
            visa_logger.setLevel(prev_level)

        try:
            self.driver.timeout(timeout)
        except Exception:
            pass
        self._build_channels()

    def _build_channels(self) -> None:
        d = self.driver
        ch = self._channels = []
        # Reference-capable temperatures (can gate a measurement).
        ch.append(Channel("mc_t", "Mixing Chamber", "K", group="Temperatures",
                           getter=d.Mixing_Chamber_Temperature, is_reference=True))
        # PT2 plate ~ the "4 K plate" stage on Proteox.
        ch.append(Channel("pt2_plate", "4K Plate (PT2)", "K", group="Temperatures",
                           getter=d.PT2_Plate_Temperature, is_reference=True))
        ch.append(Channel("still_t", "Still", "K", group="Temperatures",
                           getter=d.Still_Plate_Temperature))
        ch.append(Channel("cold_plate", "Cold Plate", "K", group="Temperatures",
                           getter=d.Cold_Plate_Temperature))
        ch.append(Channel("sample_t", "Sample", "K", group="Temperatures",
                           getter=d.Sample_Temperature, is_reference=True))
        # Settable: MC target + heaters.
        ch.append(Channel("mc_target", "MC Target", "K", settable=True,
                           group="Control",
                           getter=d.Mixing_Chamber_Temperature_Target,
                           setter=d.Mixing_Chamber_Temperature_Target))
        ch.append(Channel("mc_heater", "MC Heater Power", "W", settable=True,
                           group="Control",
                           getter=d.Mixing_Chamber_Heater_Power,
                           setter=d.Mixing_Chamber_Heater_Power))
        ch.append(Channel("still_heater", "Still Heater Power", "W", settable=True,
                           group="Control",
                           getter=d.Still_Heater_Power, setter=d.Still_Heater_Power))
        # Pressures (read-only).
        for i in range(1, 7):
            p = getattr(d, f"P{i}_Pressure")
            ch.append(Channel(f"p{i}", f"P{i}", "Pa", group="Pressures", getter=p))
        ch.append(Channel("ovc", "OVC", "Pa", group="Pressures", getter=d.OVC_Pressure))

    # target temperature
    def _set_target_raw(self, value_k: float) -> None:
        self.driver.Mixing_Chamber_Temperature_Target(value_k)

    def _get_target_raw(self) -> float:
        return float(self.driver.Mixing_Chamber_Temperature_Target())

    def _control_temperature(self) -> float:
        return float(self.driver.Mixing_Chamber_Temperature())

    def heaters_off(self) -> None:
        try:
            self.driver.mixing_chamber_heater_off()
        except Exception:
            pass
        try:
            self.driver.still_heater_off()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Teslatron backend  (MercuryiTC driver)
# ---------------------------------------------------------------------------

class TeslatronBackend(FridgeBackend):
    kind = "teslatron"
    base_temperature_k = 1.5

    def __init__(self, name: str = "MercuryiTC", control_loop: str = "sample"):
        super().__init__(name)
        # which loop regulates the gating/control temperature:
        #   "sample" -> probe/sample loop ;  "vti" -> VTI loop
        self.control_loop = control_loop

    def connect(self, *, address: str, **kwargs) -> None:
        import qcodes as qc
        # The driver lives next to this package as MercuryITC.py; import path
        # is resolved by the caller's sys.path. We try a couple of locations.
        MercuryiTC = _import_mercury()
        self.close()
        if self.name in qc.Instrument._all_instruments:
            try:
                qc.Instrument._all_instruments[self.name].close()
            except Exception:
                pass
        self.driver = MercuryiTC(self.name, address, **kwargs)
        self._build_channels()

    def _build_channels(self) -> None:
        d = self.driver
        ch = self._channels = []
        ch.append(Channel("sample_t", "Probe / Sample", "K", group="Temperatures",
                           getter=d.sample_temperature, is_reference=True))
        ch.append(Channel("vti_t", "VTI", "K", group="Temperatures",
                           getter=d.vti_temperature, is_reference=True))
        ch.append(Channel("sample_set", "Sample Setpoint", "K", settable=True,
                           group="Control",
                           getter=d.sample_temperature_setpoint,
                           setter=d.sample_temperature_setpoint))
        ch.append(Channel("vti_set", "VTI Setpoint", "K", settable=True,
                           group="Control",
                           getter=d.vti_temperature_setpoint,
                           setter=d.vti_temperature_setpoint))
        ch.append(Channel("sample_htr", "Sample Heater", "W", group="Heaters",
                           getter=d.sample_heater_power))
        ch.append(Channel("vti_htr", "VTI Heater", "W", group="Heaters",
                           getter=d.vti_heater_power))
        ch.append(Channel("pressure", "Pressure", "mB", settable=True,
                           group="Pressure",
                           getter=d.pressure_setpoint, setter=d.pressure_setpoint))
        ch.append(Channel("needle", "Needle Valve", "%", group="Pressure",
                           getter=d.needle_valve))

    def _loop_setpoint(self):
        if self.control_loop == "vti":
            return self.driver.vti_temperature_setpoint
        return self.driver.sample_temperature_setpoint

    def _loop_temperature(self):
        if self.control_loop == "vti":
            return self.driver.vti_temperature
        return self.driver.sample_temperature

    def _set_target_raw(self, value_k: float) -> None:
        self._loop_setpoint()(value_k)

    def _get_target_raw(self) -> float:
        return float(self._loop_setpoint()())

    def _control_temperature(self) -> float:
        return float(self._loop_temperature()())


def _import_mercury():
    """Import the MercuryiTC class regardless of where the driver file sits."""
    try:
        from MercuryITC import MercuryiTC  # if the file is on sys.path
        return MercuryiTC
    except Exception:
        pass
    try:
        from core.MercuryITC import MercuryiTC
        return MercuryiTC
    except Exception:
        pass
    raise ImportError(
        "Could not import MercuryiTC. Place MercuryITC.py on the PYTHONPATH "
        "(e.g. next to main.py)."
    )


# ---------------------------------------------------------------------------
# Manual / no-fridge backend
# ---------------------------------------------------------------------------

class ManualBackend(FridgeBackend):
    """
    "Other" / manual mode: no hardware temperature control.

    Useful when the cryostat is driven outside this app, or for bench testing
    the PNA + analysis pipeline. It still exposes a single user-entered
    "Recorded Temperature" so measurement names can carry a temperature label,
    but it cannot auto-wait on a reference temperature (``reference_options()``
    is empty) and cannot run temperature sweeps (``set_target_temperature``
    raises a clear error). The UI greys those features out accordingly.
    """
    kind = "manual"
    base_temperature_k = 0.0

    def __init__(self, name: str = "Manual"):
        super().__init__(name)
        self._connected = False
        self._manual_temp_k = float("nan")

    def connect(self, **kwargs) -> None:
        self._connected = True
        self._build_channels()

    def is_connected(self) -> bool:
        return self._connected

    def close(self) -> None:
        self._connected = False
        self.driver = None

    def _build_channels(self) -> None:
        self._channels = [
            Channel("manual_t", "Recorded Temperature", "K", settable=True,
                    group="Manual",
                    getter=lambda: self._manual_temp_k,
                    setter=self._set_manual_temp),
        ]

    def _set_manual_temp(self, value_k: float) -> None:
        self._manual_temp_k = float(value_k)

    # No reference temperatures -> reference_options() returns [] from the base.

    def _set_target_raw(self, value_k: float) -> None:
        raise RuntimeError("Manual / no-fridge mode has no temperature control.")

    def _get_target_raw(self) -> float:
        return self._manual_temp_k

    def _control_temperature(self) -> float:
        return self._manual_temp_k


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_fridge(kind: str) -> FridgeBackend:
    kind = (kind or "").lower()
    if kind == "proteox":
        return ProteoxBackend("Proteox")
    if kind == "teslatron":
        return TeslatronBackend("MercuryiTC")
    if kind in ("manual", "other", "none"):
        return ManualBackend("Manual")
    raise ValueError(f"Unknown fridge kind {kind!r}")
