"""
core/drivers/keysight_pna.py
============================
QCoDeS driver for Keysight PNA / PNA-L / PNA-X network analyzers (N52xx),
based on the stock ``qcodes.instrument_drivers.Keysight.N52xx`` module with
**segment-sweep support** added.  It is kept inside the project (instead of
patching site-packages) so a ``pip install -U qcodes`` cannot undo it.

What was added on top of the stock driver
-----------------------------------------
* Segment table API on the instrument:
    - ``set_segment_table(segments)``   program a whole table and switch to SEGM
    - ``get_segment_table()``           read it back (start, stop, points, state)
    - ``clear_segments()``, ``segment_count``, ``segment_arbitrary``
    - ``use_linear_sweep(start, stop, points)``  return to a normal sweep
* ``stimulus_axis`` – the *actual* frequency of every point, read from the
  instrument (``SENS:X?``) in 64-bit.  With the driver's default
  ``FORM REAL,32`` the frequencies would be rounded to ~512 Hz at 5 GHz.
* ``FormattedSweep.setpoints`` understands ``SEGM``: magnitude / phase / etc.
  now work in segment mode (stock driver raises NotImplementedError) and their
  setpoints are the real non-uniform frequencies.
* ``trace_points``: number of points in the current trace for any sweep type
  (used for the array-shape validators).
* ``check_errors()`` drains ``SYST:ERR?`` and raises ``PNAError`` if the
  instrument rejected something.
* ``KeysightN5235A`` (PNA-L, 2 ports).  Option 216 (source attenuators) unlocks
  the -90 dBm minimum power; the stock N5245A class only checked 219/419 and
  would reject e.g. ``pna.power(-50)`` on an N5235A with option 216.

SCPI references: Keysight PNA help, "Sense:Segment Commands"
(SENSe:SEGMent:LIST, :DELete:ALL, :COUNt?, :ARBitrary, :BWIDth:CONTrol,
:POWer:CONTrol) and SENSe:X[:VALues]?.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

import numpy as np
from pyvisa import constants, errors

from qcodes.instrument import (
    ChannelList,
    InstrumentBaseKWArgs,
    InstrumentChannel,
    VisaInstrument,
    VisaInstrumentKWArgs,
)
from qcodes.parameters import (
    Parameter,
    ParameterBase,
    ParameterWithSetpoints,
    create_on_off_val_mapping,
)
from qcodes.validators import Arrays, Bool, Enum, Ints, Numbers

if TYPE_CHECKING:
    from typing_extensions import Unpack


Segment = Tuple[float, float, int]          # (start_hz, stop_hz, n_points)

FREQ_RESOLUTION_HZ = 1.0                    # PNA-L synthesizer resolution (datasheet Table 6)


class PNAError(RuntimeError):
    """The instrument reported one or more SCPI errors."""


def _parse_sweep_type(s: str) -> str:
    """Normalise SENS:SWE:TYPE? replies ('SEGM', 'SEGMENT', '"LIN"', ...)."""
    t = s.strip().strip('"').upper()
    return {"SEGMENT": "SEGM", "LINEAR": "LIN", "LOGARITHMIC": "LOG",
            "POWER": "POW", "PHASE": "PHAS"}.get(t, t)


# ---------------------------------------------------------------------------
# Axis parameters
# ---------------------------------------------------------------------------

class PNAAxisParameter(Parameter):
    def __init__(self, startparam: Parameter, stopparam: Parameter,
                 pointsparam: Parameter, **kwargs: Any):
        """Axis parameter for traces from the PNA"""
        super().__init__(**kwargs)
        self._startparam = startparam
        self._stopparam = stopparam
        self._pointsparam = pointsparam

    def get_raw(self) -> np.ndarray:
        return np.linspace(self._startparam(), self._stopparam(), self._pointsparam())


class PNALogAxisParamter(PNAAxisParameter):
    def get_raw(self) -> np.ndarray:
        return np.geomspace(self._startparam(), self._stopparam(), self._pointsparam())


class PNATimeAxisParameter(PNAAxisParameter):
    def get_raw(self) -> np.ndarray:
        return np.linspace(0, self._stopparam(), self._pointsparam())


class PNAStimulusAxisParameter(Parameter):
    """
    The actual stimulus value of every point, read back from the instrument.
    Correct for any sweep type, including (non-uniform) segment sweeps.
    """

    def get_raw(self) -> np.ndarray:
        root: KeysightPNABase = self.root_instrument  # type: ignore[assignment]
        return root._read_stimulus()


# ---------------------------------------------------------------------------
# Formatted trace data
# ---------------------------------------------------------------------------

class FormattedSweep(ParameterWithSetpoints):
    """
    Mag will run a sweep, including averaging, before returning data.
    As such, wait time in a loop is not needed.
    """

    def __init__(self, name: str, instrument: "KeysightPNATrace", sweep_format: str,
                 label: str, unit: str, memory: bool = False, **kwargs: Any) -> None:
        super().__init__(name, instrument=instrument, label=label, unit=unit, **kwargs)
        self.sweep_format = sweep_format
        self.memory = memory

    @property
    def setpoints(self) -> "Sequence[ParameterBase]":
        """Ask the PNA what type of sweep is active and return the matching axis."""
        if self.instrument is None:
            raise RuntimeError("Cannot return setpoints if not attached to instrument")
        root_instrument: KeysightPNABase = self.root_instrument  # type: ignore[assignment]
        sweep_type = root_instrument.sweep_type()
        if sweep_type == "LIN":
            return (root_instrument.frequency_axis,)
        elif sweep_type == "LOG":
            return (root_instrument.frequency_log_axis,)
        elif sweep_type == "CW":
            return (root_instrument.time_axis,)
        elif sweep_type == "SEGM":
            return (root_instrument.stimulus_axis,)
        else:
            raise NotImplementedError(f"Axis for type {sweep_type} not implemented yet")

    @setpoints.setter
    def setpoints(self, setpoints: Any) -> None:
        return

    def get_raw(self) -> np.ndarray:
        if self.instrument is None:
            raise RuntimeError("Cannot get data without instrument")
        root_instr = self.instrument.root_instrument
        auto_sweep = root_instr.auto_sweep()

        prev_mode = ""
        if auto_sweep:
            prev_mode = self.instrument.run_sweep()
        self.instrument.format(self.sweep_format)
        data = root_instr.visa_handle.query_binary_values(
            "CALC:DATA? FDATA", datatype="f", is_big_endian=True
        )
        data = np.array(data)
        if auto_sweep:
            root_instr.sweep_mode(prev_mode)
        return data


# ---------------------------------------------------------------------------
# Ports / traces
# ---------------------------------------------------------------------------

class KeysightPNAPort(InstrumentChannel):
    """Allow operations on individual PNA ports."""

    def __init__(self, parent: "KeysightPNABase", name: str, port: int,
                 min_power: float, max_power: float,
                 **kwargs: "Unpack[InstrumentBaseKWArgs]") -> None:
        super().__init__(parent, name, **kwargs)

        self.port = int(port)
        if self.port < 1 or self.port > 4:
            raise ValueError("Port must be between 1 and 4.")

        pow_cmd = f"SOUR:POW{self.port}"
        self.source_power: Parameter = self.add_parameter(
            "source_power", label="power", unit="dBm",
            get_cmd=f"{pow_cmd}?", set_cmd=f"{pow_cmd} {{}}", get_parser=float,
            vals=Numbers(min_value=min_power, max_value=max_power),
        )

    def _set_power_limits(self, min_power: float, max_power: float) -> None:
        self.source_power.vals = Numbers(min_value=min_power, max_value=max_power)


PNAPort = KeysightPNAPort


class KeysightPNATrace(InstrumentChannel):
    """Allow operations on individual PNA traces."""

    def __init__(self, parent: "KeysightPNABase", name: str, trace_name: str,
                 trace_num: int, **kwargs: "Unpack[InstrumentBaseKWArgs]") -> None:
        super().__init__(parent, name, **kwargs)
        self.trace_name = trace_name
        self.trace_num = trace_num

        self.trace: Parameter = self.add_parameter(
            "trace", label="Trace", get_cmd=self._Sparam, set_cmd=self._set_Sparam
        )
        self.format: Parameter = self.add_parameter(
            "format", label="Format", get_cmd="CALC:FORM?", set_cmd="CALC:FORM {}",
            vals=Enum("MLIN", "MLOG", "PHAS", "UPH", "IMAG", "REAL", "POLAR", "GDEL"),
        )

        # array shape follows the *actual* number of points of any sweep type
        npts = self.parent.trace_points
        for pname, fmt, label, unit in (
            ("magnitude", "MLOG", "Magnitude", "dB"),
            ("linear_magnitude", "MLIN", "Magnitude", "ratio"),
            ("phase", "PHAS", "Phase", "deg"),
            ("unwrapped_phase", "UPH", "Phase", "deg"),
            ("group_delay", "GDEL", "Group Delay", "s"),
            ("real", "REAL", "Real", "LinMag"),
            ("imaginary", "IMAG", "Imaginary", "LinMag"),
        ):
            self.add_parameter(pname, sweep_format=fmt, label=label, unit=unit,
                               parameter_class=FormattedSweep,
                               vals=Arrays(shape=(npts,)))
        self.add_parameter(
            "polar", sweep_format="POLAR", label="Polar", unit="ratio",
            parameter_class=FormattedSweep, get_parser=self._parse_polar_data,
            vals=Arrays(shape=(npts,), valid_types=(complex,)),
        )

    def disable(self) -> None:
        self.write(f"DISP:TRAC{self.trace_num}:STAT 0")

    @staticmethod
    def _parse_polar_data(data: np.ndarray) -> np.ndarray:
        data_shape = data.size
        return data.reshape((data_shape // 2, 2)).view(dtype=np.complex128).flatten()

    def run_sweep(self) -> str:
        """Run a (possibly averaged) sweep on all traces of the channel."""
        root_instr = self.root_instrument
        prev_mode = root_instr.sweep_mode()
        if root_instr.averages_enabled():
            avg = root_instr.averages()
            root_instr.reset_averages()
            root_instr.group_trigger_count(avg)
            root_instr.sweep_mode("GRO")
        else:
            root_instr.sweep_mode("SING")
        try:
            while root_instr.sweep_mode() != "HOLD":
                time.sleep(0.1)
        except KeyboardInterrupt:
            msg = "User abort detected. "
            source = root_instr.trigger_source()
            if source == "MAN":
                msg += ("The trigger source is manual. Are you sure this is correct? "
                        "Please set the correct source with the 'trigger_source' parameter")
            elif source == "EXT":
                msg += "The trigger source is external. Is the trigger source functional?"
            self.log.warning(msg)
            raise
        return prev_mode

    def write(self, cmd: str) -> None:
        self.root_instrument.active_trace(self.trace_num)
        super().write(cmd)

    def ask(self, cmd: str) -> str:
        self.root_instrument.active_trace(self.trace_num)
        return super().ask(cmd)

    def _Sparam(self) -> str:
        paramspec = self.root_instrument.get_trace_catalog()
        specs = paramspec.split(",")
        for spec_ind in range(len(specs) // 2):
            name, param = specs[spec_ind * 2: (spec_ind + 1) * 2]
            if name == self.trace_name:
                return param
        raise RuntimeError("Can't find selected trace on the PNA")

    def _set_Sparam(self, val: str) -> None:
        if not re.match("S[1-4][1-4]", val):
            raise ValueError("Invalid S parameter spec")
        self.write(f'CALC:PAR:MOD:EXT "{val}"')


PNATrace = KeysightPNATrace


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class KeysightPNABase(VisaInstrument):
    """
    Base class for qcodes drivers for Agilent/Keysight series PNAs.
    Not to be instantiated directly. Use a model specific subclass.

    Note: this driver only expects a single channel on the PNA.
    """

    default_terminator = "\n"
    max_points = 100001

    def __init__(self, name: str, address: str, min_freq: float, max_freq: float,
                 min_power: float, max_power: float, nports: int,
                 **kwargs: "Unpack[VisaInstrumentKWArgs]") -> None:
        super().__init__(name, address, **kwargs)
        self.min_freq = min_freq
        self.max_freq = max_freq
        self._stim_cache: Optional[Tuple[tuple, np.ndarray]] = None

        ports = ChannelList(self, "PNAPorts", KeysightPNAPort)
        for port_num in range(1, nports + 1):
            port = KeysightPNAPort(self, f"port{port_num}", port_num, min_power, max_power)
            ports.append(port)
            self.add_submodule(f"port{port_num}", port)
        self.add_submodule("ports", ports.to_channel_tuple())

        self.output: Parameter = self.add_parameter(
            "output", label="RF Output", get_cmd=":OUTPut?", set_cmd=":OUTPut {}",
            val_mapping=create_on_off_val_mapping(on_val="1", off_val="0"),
        )
        self.power: Parameter = self.add_parameter(
            "power", label="Power", get_cmd="SOUR:POW?", get_parser=float,
            set_cmd="SOUR:POW {:.2f}", unit="dBm",
            vals=Numbers(min_value=min_power, max_value=max_power),
        )
        self.if_bandwidth: Parameter = self.add_parameter(
            "if_bandwidth", label="IF Bandwidth", get_cmd="SENS:BAND?", get_parser=float,
            set_cmd="SENS:BAND {:.2f}", unit="Hz", vals=Numbers(min_value=1, max_value=15e6),
        )
        self.averages_enabled: Parameter = self.add_parameter(
            "averages_enabled", label="Averages Enabled", get_cmd="SENS:AVER?",
            set_cmd="SENS:AVER {}", val_mapping={True: "1", False: "0"},
        )
        self.averages: Parameter = self.add_parameter(
            "averages", label="Averages", get_cmd="SENS:AVER:COUN?", get_parser=int,
            set_cmd="SENS:AVER:COUN {:d}", unit="", vals=Numbers(min_value=1, max_value=65536),
        )
        self.start: Parameter = self.add_parameter(
            "start", label="Start Frequency", get_cmd="SENS:FREQ:STAR?", get_parser=float,
            set_cmd="SENS:FREQ:STAR {}", unit="Hz",
            vals=Numbers(min_value=min_freq, max_value=max_freq),
        )
        self.stop: Parameter = self.add_parameter(
            "stop", label="Stop Frequency", get_cmd="SENS:FREQ:STOP?", get_parser=float,
            set_cmd="SENS:FREQ:STOP {}", unit="Hz",
            vals=Numbers(min_value=min_freq, max_value=max_freq),
        )
        self.center: Parameter = self.add_parameter(
            "center", label="Center Frequency", get_cmd="SENS:FREQ:CENT?", get_parser=float,
            set_cmd="SENS:FREQ:CENT {}", unit="Hz",
            vals=Numbers(min_value=min_freq, max_value=max_freq),
        )
        self.span: Parameter = self.add_parameter(
            "span", label="Frequency Span", get_cmd="SENS:FREQ:SPAN?", get_parser=float,
            set_cmd="SENS:FREQ:SPAN {}", unit="Hz",
            vals=Numbers(min_value=min_freq, max_value=max_freq),
        )
        self.cw: Parameter = self.add_parameter(
            "cw", label="CW Frequency", get_cmd="SENS:FREQ:CW?", get_parser=float,
            set_cmd="SENS:FREQ:CW {}", unit="Hz",
            vals=Numbers(min_value=min_freq, max_value=max_freq),
        )
        self.points: Parameter = self.add_parameter(
            "points", label="Points", get_cmd="SENS:SWE:POIN?", get_parser=int,
            set_cmd="SENS:SWE:POIN {}", unit="",
            vals=Numbers(min_value=1, max_value=self.max_points),
        )
        self.electrical_delay: Parameter = self.add_parameter(
            "electrical_delay", label="Electrical Delay", get_cmd="CALC:CORR:EDEL:TIME?",
            get_parser=float, set_cmd="CALC:CORR:EDEL:TIME {:.6e}", unit="s",
            vals=Numbers(min_value=0, max_value=100000),
        )
        self.sweep_time: Parameter = self.add_parameter(
            "sweep_time", label="Time", get_cmd="SENS:SWE:TIME?", get_parser=float,
            unit="s", vals=Numbers(0, 1e6),
        )
        self.sweep_mode: Parameter = self.add_parameter(
            "sweep_mode", label="Mode", get_cmd="SENS:SWE:MODE?", set_cmd="SENS:SWE:MODE {}",
            vals=Enum("HOLD", "CONT", "GRO", "SING"),
        )
        self.sweep_type: Parameter = self.add_parameter(
            "sweep_type", label="Type", get_cmd="SENS:SWE:TYPE?", set_cmd="SENS:SWE:TYPE {}",
            get_parser=_parse_sweep_type,
            vals=Enum("LIN", "LOG", "POW", "CW", "SEGM", "PHAS"),
        )
        self.group_trigger_count: Parameter = self.add_parameter(
            "group_trigger_count", get_cmd="SENS:SWE:GRO:COUN?", get_parser=int,
            set_cmd="SENS:SWE:GRO:COUN {}", vals=Ints(1, 2000000),
        )
        self.trigger_source: Parameter = self.add_parameter(
            "trigger_source", get_cmd="TRIG:SOUR?", set_cmd="TRIG:SOUR {}",
            vals=Enum("EXT", "IMM", "MAN"),
        )

        # ---- segment sweep -----------------------------------------------
        self.segment_count: Parameter = self.add_parameter(
            "segment_count", label="Number of segments", get_cmd="SENS:SEGM:COUN?",
            get_parser=lambda s: int(float(s)),
        )
        self.segment_arbitrary: Parameter = self.add_parameter(
            "segment_arbitrary", label="Arbitrary (overlapping) segments",
            get_cmd="SENS:SEGM:ARB?", set_cmd="SENS:SEGM:ARB {}",
            val_mapping=create_on_off_val_mapping(on_val="1", off_val="0"),
        )
        self.trace_points: Parameter = self.add_parameter(
            "trace_points", label="Points in the current trace",
            get_cmd=self._get_trace_points,
        )

        # ---- axis parameters ---------------------------------------------
        self.frequency_axis: PNAAxisParameter = self.add_parameter(
            "frequency_axis", unit="Hz", label="Frequency", parameter_class=PNAAxisParameter,
            startparam=self.start, stopparam=self.stop, pointsparam=self.points,
            vals=Arrays(shape=(self.trace_points,)),
        )
        self.frequency_log_axis: PNALogAxisParamter = self.add_parameter(
            "frequency_log_axis", unit="Hz", label="Frequency",
            parameter_class=PNALogAxisParamter,
            startparam=self.start, stopparam=self.stop, pointsparam=self.points,
            vals=Arrays(shape=(self.trace_points,)),
        )
        self.time_axis: PNATimeAxisParameter = self.add_parameter(
            "time_axis", unit="s", label="Time", parameter_class=PNATimeAxisParameter,
            startparam=None, stopparam=self.sweep_time, pointsparam=self.points,
            vals=Arrays(shape=(self.trace_points,)),
        )
        self.stimulus_axis: PNAStimulusAxisParameter = self.add_parameter(
            "stimulus_axis", unit="Hz", label="Frequency",
            parameter_class=PNAStimulusAxisParameter,
            vals=Arrays(shape=(self.trace_points,)),
        )

        # ---- traces --------------------------------------------------------
        self.active_trace: Parameter = self.add_parameter(
            "active_trace", label="Active Trace", get_cmd="CALC:PAR:MNUM?", get_parser=int,
            set_cmd="CALC:PAR:MNUM {}", vals=Numbers(min_value=1, max_value=24),
        )
        self._traces = ChannelList(self, "PNATraces", KeysightPNATrace)
        self.add_submodule("traces", self._traces)
        trace1 = self.traces[0]
        params = trace1.parameters
        if not isinstance(params, dict):
            raise RuntimeError(f"Expected trace.parameters to be a dict got {type(params)}")
        for param in params.values():
            self.parameters[param.name] = param
        self.run_sweep = trace1.run_sweep
        self.active_trace(trace1.trace_num)

        self.auto_sweep: Parameter = self.add_parameter(
            "auto_sweep", label="Auto Sweep", set_cmd=None, get_cmd=None,
            vals=Bool(), initial_value=True,
        )

        self.write("FORM REAL,32")
        self.write("FORM:BORD NORM")
        self.connect_message()

    # ------------------------------------------------------------------
    # traces
    # ------------------------------------------------------------------

    @property
    def traces(self) -> ChannelList:
        try:
            active_trace = self.active_trace()
        except errors.VisaIOError as e:
            self.log.debug("Exception on querying active trace: %r", e)
            if e.error_code == constants.StatusCode.error_timeout:
                self.log.info("No active trace on PNA")
                active_trace = None
            else:
                raise
        parlist = self.get_trace_catalog().split(",")
        self._traces.clear()
        for trace_name in parlist[::2]:
            trace_num = self.select_trace_by_name(trace_name)
            pna_trace = KeysightPNATrace(self, f"tr{trace_num}", trace_name, trace_num)
            self._traces.append(pna_trace)
        if active_trace:
            self.active_trace(active_trace)
        return self._traces

    def get_options(self) -> "Sequence[str]":
        return [o.strip() for o in self.ask("*OPT?").strip().strip('"').split(",") if o.strip()]

    def add_trace(self) -> KeysightPNATrace:
        existing_traces = [tr.trace_name for tr in self.traces]
        self.write("DISP:TRAC:NEW 0")
        time.sleep(0.5)
        for new_trace, old_trace in zip(self.traces, existing_traces):
            if new_trace.trace_name != old_trace:
                return new_trace
        raise RuntimeError("Failed to add PNA trace")

    def enable_trace(self, trace_num: int) -> KeysightPNATrace:
        self.write(f"DISP:TRAC{trace_num}:STAT 1")
        time.sleep(0.5)
        for trace in self.traces:
            if trace.trace_num == trace_num:
                return trace
        raise RuntimeError(f"Failed to enable PNA trace tr{trace_num}")

    def get_trace_catalog(self) -> str:
        return self.ask("CALC:PAR:CAT:EXT?").strip('"')

    def select_trace_by_name(self, trace_name: str) -> int:
        self.write(f"CALC:PAR:SEL '{trace_name}'")
        return self.active_trace()

    def reset_averages(self) -> None:
        self.write("SENS:AVER:CLE")

    def averages_on(self) -> None:
        self.averages_enabled(True)

    def averages_off(self) -> None:
        self.averages_enabled(False)

    def _set_power_limits(self, min_power: float, max_power: float) -> None:
        self.power.vals = Numbers(min_value=min_power, max_value=max_power)
        for port in self.ports:
            port._set_power_limits(min_power, max_power)

    # ------------------------------------------------------------------
    # errors
    # ------------------------------------------------------------------

    def get_errors(self, max_reads: int = 1000) -> List[str]:
        """Drain the instrument error queue and return the non-zero entries."""
        out = []
        for _ in range(max_reads):
            e = self.ask("SYST:ERR?").strip()
            code = e.split(",", 1)[0].strip()
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                break
            out.append(e)
        return out

    def check_errors(self, context: str = "") -> None:
        errs = self.get_errors()
        if errs:
            shown = "; ".join(errs[:3]) + (f" (+{len(errs) - 3} more)" if len(errs) > 3 else "")
            raise PNAError((context + ": " if context else "") + shown)

    # ------------------------------------------------------------------
    # binary helpers
    # ------------------------------------------------------------------

    def _query_float64(self, cmd: str) -> np.ndarray:
        """Binary query in REAL,64, restoring the driver's REAL,32 afterwards."""
        self.write("FORM REAL,64")
        try:
            vals = self.visa_handle.query_binary_values(cmd, datatype="d", is_big_endian=True)
        finally:
            self.write("FORM REAL,32")
        return np.asarray(vals, dtype=float)

    def _read_stimulus(self) -> np.ndarray:
        """Actual stimulus of every point (``SENS:X?``), full 64-bit resolution."""
        return self._query_float64("SENS:X?")

    def _get_trace_points(self) -> int:
        if self.sweep_type() == "SEGM":
            return int(self._read_stimulus().size)
        return int(self.points())

    # ------------------------------------------------------------------
    # segment sweep
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_segments(segments: Sequence[Segment],
                           resolution: float = FREQ_RESOLUTION_HZ
                           ) -> List[Segment]:
        """
        Round to the synthesizer resolution and check the table is a valid
        non-arbitrary segment list: ascending, non-overlapping, >=1 point each,
        and no two points closer than the frequency resolution.
        """
        out: List[Segment] = []
        prev_stop = -np.inf
        for i, seg in enumerate(segments, 1):
            f0, f1, n = float(seg[0]), float(seg[1]), int(seg[2])
            f0 = round(f0 / resolution) * resolution
            f1 = round(f1 / resolution) * resolution
            if n < 1:
                raise ValueError(f"segment {i}: needs at least 1 point")
            if n == 1:
                f1 = f0
            if f1 < f0:
                raise ValueError(f"segment {i}: stop < start ({f1} < {f0})")
            if n > 1 and (f1 - f0) / (n - 1) < resolution - 1e-9:
                raise ValueError(
                    f"segment {i}: {n} points in {f1 - f0:.0f} Hz is finer than the "
                    f"{resolution:g} Hz frequency resolution")
            if f0 <= prev_stop:
                raise ValueError(f"segment {i} overlaps segment {i - 1} "
                                 f"({f0:.0f} <= {prev_stop:.0f} Hz)")
            out.append((f0, f1, n))
            prev_stop = f1
        if not out:
            raise ValueError("empty segment table")
        return out

    def clear_segments(self) -> None:
        """Delete all segments (the PNA falls back to a linear sweep)."""
        self.write("SENS:SEGM:DEL:ALL")

    def set_segment_table(self, segments: Sequence[Segment], *,
                          activate: bool = True, verify: bool = True) -> np.ndarray:
        """
        Program a full segment table and (by default) switch to SEGM sweep.

        ``segments`` is a list of ``(start_hz, stop_hz, n_points)``, ascending
        and non-overlapping.  All segments use the channel IF bandwidth and
        power (per-segment IFBW/power control is switched off), so
        ``if_bandwidth`` / ``power`` keep working as before.

        The table is sent in one ``SENS:SEGM:LIST SSTOP,...`` command (ASCII
        format for the duration of the write).  If that is rejected, it is
        programmed segment by segment.  Afterwards the instrument error queue
        is checked and the table is read back; returns the real stimulus axis.
        """
        segs = self.normalize_segments(segments)
        total = sum(n for _, _, n in segs)
        if total > self.max_points:
            raise ValueError(f"segment table has {total} points (max {self.max_points})")
        for f0, f1, _ in segs:
            if f0 < self.min_freq or f1 > self.max_freq:
                raise ValueError(f"segment {f0:.0f}-{f1:.0f} Hz outside instrument range")

        self.get_errors()                         # start from a clean queue
        self.write("SENS:SEGM:ARB OFF")
        self.write("SENS:SEGM:BWID:CONT OFF")      # channel IFBW for all segments
        self.write("SENS:SEGM:POW:CONT OFF")       # channel power for all segments

        try:
            self._write_segment_list(segs)
            self.check_errors("SENS:SEGM:LIST")
            if self.segment_count() != len(segs):
                raise PNAError("segment count mismatch after SENS:SEGM:LIST")
        except Exception as e:  # fall back to one command per segment
            self.log.warning("SEGM:LIST failed (%s); programming segments one by one", e)
            self.get_errors()
            self._write_segments_individually(segs)
            self.check_errors("segment table")

        if activate:
            self.sweep_type("SEGM")
            self.check_errors("SENS:SWE:TYPE SEGM")
            if self.sweep_type() != "SEGM":
                raise PNAError("instrument did not enter segment sweep")

        stim = self._read_stimulus() if activate else np.array([])
        if verify:
            readback = self.get_segment_table()
            ok = len(readback) == len(segs) and all(
                abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1 and a[2] == b[2]
                for a, b in zip(readback, segs))
            if not ok:
                raise PNAError(f"segment table read-back mismatch: {readback[:3]}...")
            if activate and stim.size != total:
                raise PNAError(f"stimulus has {stim.size} points, expected {total}")
        return stim

    def _write_segment_list(self, segs: List[Segment]) -> None:
        body = ",".join(f"1,{n},{f0:.0f},{f1:.0f}" for f0, f1, n in segs)
        self.write("FORM ASC,0")
        try:
            self.write(f"SENS:SEGM:LIST SSTOP,{len(segs)},{body}")
        finally:
            self.write("FORM REAL,32")

    def _write_segments_individually(self, segs: List[Segment]) -> None:
        self.clear_segments()
        for i, (f0, f1, n) in enumerate(segs, 1):
            self.write(f"SENS:SEGM{i}:ADD")
            # stop before start for segment 1 doesn't matter; each new segment
            # starts at the previous stop, so set STOP first, then STAR
            self.write(f"SENS:SEGM{i}:FREQ:STOP {f1:.0f}")
            self.write(f"SENS:SEGM{i}:FREQ:STAR {f0:.0f}")
            self.write(f"SENS:SEGM{i}:SWE:POIN {n}")
            self.write(f"SENS:SEGM{i}:STAT ON")

    def get_segment_table(self) -> List[Segment]:
        """Read back the segment table as [(start, stop, points), ...] (ON segments)."""
        n = self.segment_count()
        if n == 0:
            return []
        vals = self._query_float64("SENS:SEGM:LIST? SSTOP")
        per = vals.size // n
        rows = vals.reshape(n, per)
        return [(float(r[2]), float(r[3]), int(round(r[1]))) for r in rows if r[0] > 0.5]

    def use_linear_sweep(self, start: Optional[float] = None, stop: Optional[float] = None,
                         points: Optional[int] = None) -> None:
        """Back to a normal linear sweep (segments are kept in the table)."""
        self.sweep_type("LIN")
        if start is not None:
            self.start(start)
        if stop is not None:
            self.stop(stop)
        if points is not None:
            self.points(points)


class KeysightPNAxBase(KeysightPNABase):
    def _enable_fom(self) -> None:
        self.aux_frequency: Parameter = self.add_parameter(
            "aux_frequency", label="Aux Frequency", get_cmd="SENS:FOM:RANG4:FREQ:CW?",
            get_parser=float, set_cmd="SENS:FOM:RANG4:FREQ:CW {:.2f}", unit="Hz",
            vals=Numbers(min_value=self.min_freq, max_value=self.max_freq),
        )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class KeysightN5235A(KeysightPNABase):
    """
    PNA-L N5235A, 10 MHz - 50 GHz, 2 ports.
    Option 216 adds source attenuators: settable power down to -90 dBm
    (datasheet Table 12); without it the minimum is -30 dBm.
    """

    def __init__(self, name: str, address: str, **kwargs: "Unpack[VisaInstrumentKWArgs]"):
        super().__init__(name, address, min_freq=10e6, max_freq=50e9,
                         min_power=-30, max_power=13, nports=2, **kwargs)
        options = set(self.get_options())
        if options & {"216", "219", "419"}:
            self._set_power_limits(min_power=-90, max_power=13)


class KeysightN5245A(KeysightPNAxBase):
    """PNA-X N5245A (same as the stock driver, now with segment sweep)."""

    def __init__(self, name: str, address: str, **kwargs: "Unpack[VisaInstrumentKWArgs]"):
        super().__init__(name, address, min_freq=10e6, max_freq=50e9,
                         min_power=-30, max_power=13, nports=4, **kwargs)
        options = set(self.get_options())
        if options & {"219", "419"}:
            self._set_power_limits(min_power=-90, max_power=13)
        if "080" in options:
            self._enable_fom()
