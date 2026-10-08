"""
tests/fake_pna.py
=================
A strict, stateful SCPI emulator of a single-channel Keysight PNA/PNA-L
(N52xx family), good enough to drive the real QCoDeS driver class.

It is deliberately *strict* so that it catches the mistakes that bite on the
real instrument:

* Data and stimulus queries are returned as IEEE binary blocks packed in the
  current FORM (REAL,32 / REAL,64).  Reading them with the wrong datatype
  raises, and reading them through a plain ASCII ``query`` raises the same
  ``'ascii' codec can't decode`` error the real PNA produces.
* REAL,32 really is float32 — stimulus frequencies read that way are rounded
  exactly like on the instrument (512 Hz steps at ~5 GHz).
* Segment tables follow the documented rules: no overlapping segments
  (ARB OFF), 1 Hz frequency resolution, ``SEGM:LIST`` data must match FORM,
  ``SEGM:DEL:ALL`` drops the sweep type back to LIN.
* Unknown commands push a SCPI error onto ``SYST:ERR?``; unknown queries
  time out like a real instrument would.

The device under test is a notch resonator following Eq. (1) of
Baity et al., PRR 6, 013329 (2024), with a power-dependent (TLS-like) Qi,
complex background a·exp(iα − 2πifτ), Gaussian background noise σn that
scales with power, IFBW and averages, and resonance-frequency jitter σ_fr.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from pyvisa import constants
from pyvisa.errors import VisaIOError


class SimVisaLibrary:      # name matters: qcodes treats it as the "sim" backend
    pass


@dataclass
class Resonator:
    fr: float = 5.123456789e9
    absQc: float = 2.0e5
    phi: float = 0.3               # impedance-mismatch rotation (rad)
    Qi_high: float = 2.0e6         # saturated (high power) Qi
    Q_tls: float = 4.0e5           # low-power TLS-limited Qi contribution
    p_c_dbm: float = -40.0         # TLS saturation power (VNA power)
    a: float = 0.05
    alpha: float = 1.1
    tau: float = 52e-9             # cable delay (s)
    sigma_fr: float = 0.0          # resonance-frequency jitter (Hz)
    noise_ref: float = 2e-4        # σn (relative to a) at 0 dBm, 1 kHz IFBW, 1 avg
    slope: float = 0.0             # linear background slope (|S21| per Hz)
    kerr_hz_0dbm: float = 0.0      # Kerr shift of fr (Hz) at full intracavity occupation, 0 dBm

    def Qi(self, p_dbm: float) -> float:
        # Qi rises with power as TLS saturate: 1/Qi = 1/Qi_high + 1/Q_tls / sqrt(1 + P/Pc)
        x = 10 ** ((p_dbm - self.p_c_dbm) / 10.0)
        return 1.0 / (1.0 / self.Qi_high + (1.0 / self.Q_tls) / np.sqrt(1.0 + x))

    def Ql(self, p_dbm: float) -> float:
        Qc_eff = self.absQc / np.cos(self.phi)          # Re{1/Qc} = cos(phi)/|Qc|
        return 1.0 / (1.0 / self.Qi(p_dbm) + 1.0 / Qc_eff)

    def s21(self, f, p_dbm, ifbw, navg, rng):
        f = np.asarray(f, float)
        Ql = self.Ql(p_dbm)
        fr = self.fr + rng.normal(0, self.sigma_fr, f.size) if self.sigma_fr else self.fr
        if self.kerr_hz_0dbm:
            # Duffing-like: fr is pulled down in proportion to the stored energy,
            # solved self-consistently point by point (sweeping up in frequency)
            shift_max = self.kerr_hz_0dbm * 10 ** (p_dbm / 10.0)
            fr_eff = np.empty_like(f); occ = 0.0
            for i, fi in enumerate(f):
                for _ in range(60):
                    fe = (fr if np.isscalar(fr) else fr[i]) - shift_max * occ
                    x = 2 * Ql * (fi / fe - 1.0)
                    occ = 0.7 * occ + 0.3 / (1 + x * x)
                fr_eff[i] = fe
            fr = fr_eff
        ideal = 1.0 - (Ql / self.absQc) * np.exp(1j * self.phi) / (1.0 + 2j * Ql * (f / fr - 1.0))
        bg = (self.a + self.slope * (f - self.fr)) * np.exp(1j * self.alpha - 2j * np.pi * f * self.tau)
        z = bg * ideal
        sig = self.a * self.noise_ref * 10 ** (-p_dbm / 20.0) * np.sqrt(ifbw / 1e3) / np.sqrt(max(navg, 1))
        z = z + rng.normal(0, sig, f.size) + 1j * rng.normal(0, sig, f.size)
        return z


@dataclass
class Segment:
    state: bool = True
    points: int = 21
    start: float = 1e9
    stop: float = 2e9


class FakePNAHandle:
    """Minimal pyvisa MessageBasedResource look-alike backed by a SCPI emulator."""

    def __init__(self, resonator: Optional[Resonator] = None, options: str = "216",
                 seed: int = 1, min_freq=10e6, max_freq=50e9, max_points=100003):
        self.visalib = SimVisaLibrary()
        self.resource_name = "TCPIP0::fake-pna::inst0::INSTR"
        self.timeout = 10000
        self.read_termination = "\n"
        self.write_termination = "\n"
        self.res = resonator or Resonator()
        self.options = options
        self.rng = np.random.default_rng(seed)
        self.min_freq, self.max_freq, self.max_points = min_freq, max_freq, max_points
        # state
        self.form = "REAL,32"
        self.border = "NORM"
        self.start, self.stop, self.points = 10e6, 50e9, 201
        self.sweep_type = "LIN"
        self.sweep_mode = "CONT"
        self._pending_hold = 0
        self.power = -15.0
        self.ifbw = 1e3
        self.aver = False
        self.aver_count = 1
        self.group_count = 1
        self.output = True
        self.trig_source = "IMM"
        self.calc_form = "MLOG"
        self.mnum = 1
        self.edelay = 0.0
        self.segments: List[Segment] = []
        self.seg_arb = False
        self.seg_bw_ctrl = False
        self.seg_pow_ctrl = False
        self.errors: List[str] = []
        self._z = None                  # last acquired complex trace
        self.log: List[str] = []
        self.sweeps = 0
        self.closed = False
        self.session = None

    # ---------------- pyvisa-ish surface ----------------
    def close(self):
        self.closed = True

    def clear(self):
        pass

    def write(self, cmd: str):
        self.log.append(cmd)
        self._dispatch(cmd.strip(), query=False)
        return len(cmd)

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        out = self._dispatch(cmd.strip(), query=True)
        if isinstance(out, (bytes, bytearray)):
            # what pyvisa does when you ascii-read a binary block
            raise UnicodeDecodeError("ascii", bytes(out), 0, 1, "ordinal not in range(128)")
        return out

    def query_binary_values(self, cmd, datatype="f", is_big_endian=False,
                            container=list, **kw):
        self.log.append(cmd)
        out = self._dispatch(cmd.strip(), query=True)
        if isinstance(out, str):
            raise VisaIOError(constants.StatusCode.error_timeout)  # header '#' never arrives
        blob = bytes(out)
        hdr_n = int(chr(blob[1]))
        n = int(blob[2:2 + hdr_n])
        payload = blob[2 + hdr_n:2 + hdr_n + n]
        size = struct.calcsize(datatype)
        if len(payload) % size:
            raise ValueError("binary block length does not match datatype")
        endian = ">" if is_big_endian else "<"
        vals = struct.unpack(f"{endian}{len(payload)//size}{datatype}", payload)
        return container(vals)

    # ---------------- helpers ----------------
    def _err(self, code, msg):
        self.errors.append(f'{code},"{msg}"')

    def _block(self, values) -> bytes | str:
        values = np.asarray(values, float)
        if self.form.startswith("ASC"):
            return ",".join(f"{v:+.12E}" for v in values)
        dt = "d" if self.form == "REAL,64" else "f"
        endian = ">" if self.border == "NORM" else "<"
        payload = struct.pack(f"{endian}{values.size}{dt}", *values.tolist())
        n = str(len(payload))
        return b"#" + str(len(n)).encode() + n.encode() + payload

    def _active_segments(self):
        return [s for s in self.segments if s.state]

    def _stimulus(self) -> np.ndarray:
        if self.sweep_type == "SEGM":
            parts = []
            for s in sorted(self._active_segments(), key=lambda s: s.start):
                if s.points == 1:
                    parts.append(np.array([s.start]))
                else:
                    parts.append(np.linspace(s.start, s.stop, s.points))
            f = np.concatenate(parts) if parts else np.array([])
        else:
            f = np.linspace(self.start, self.stop, self.points)
        return np.round(f)          # 1 Hz synthesizer resolution

    def _total_points(self):
        if self.sweep_type == "SEGM":
            return sum(s.points for s in self._active_segments())
        return self.points

    def _validate_segments(self) -> bool:
        act = sorted(self._active_segments(), key=lambda s: s.start)
        if not act:
            return False
        if sum(s.points for s in act) > self.max_points:
            self._err(-222, "Data out of range; too many points")
            return False
        if not self.seg_arb:
            for s in act:
                if s.stop < s.start:
                    self._err(-221, "Settings conflict; reverse segment needs ARB ON")
                    return False
            for a, b in zip(act, act[1:]):
                if b.start <= a.stop:
                    self._err(-221, "Settings conflict; segments overlap")
                    return False
        return True

    def _acquire(self):
        f = self._stimulus()
        navg = self.aver_count if self.aver else 1
        self._z = self.res.s21(f, self.power, self.ifbw, navg, self.rng)
        self.sweeps += 1

    def _formatted(self):
        if self._z is None:
            self._acquire()
        z = self._z
        if self.calc_form == "MLOG":
            return 20 * np.log10(np.abs(z))
        if self.calc_form == "MLIN":
            return np.abs(z)
        if self.calc_form == "PHAS":
            return np.degrees(np.angle(z))
        if self.calc_form == "UPH":
            return np.degrees(np.unwrap(np.angle(z)))
        if self.calc_form == "REAL":
            return z.real
        if self.calc_form == "IMAG":
            return z.imag
        if self.calc_form == "POLAR":
            return np.column_stack([z.real, z.imag]).ravel()
        raise ValueError(self.calc_form)

    @staticmethod
    def _num(s):
        s = s.strip().upper()
        for suf, mul in (("GHZ", 1e9), ("MHZ", 1e6), ("KHZ", 1e3), ("HZ", 1.0)):
            if s.endswith(suf):
                return float(s[: -len(suf)]) * mul
        return float(s)

    @staticmethod
    def _bool(s):
        return s.strip().upper() in ("1", "ON", "+1")

    # ---------------- SCPI dispatch ----------------
    def _dispatch(self, cmd: str, query: bool):
        u = cmd.upper()
        m = re.match(r"^:?([A-Z0-9:*?]+)\s*(.*)$", u, re.S)
        head, arg = (m.group(1), cmd[m.end(1):].strip()) if m else (u, "")
        # normalise optional SENSe1 / CALCulate1 numbering
        head = re.sub(r"^SENSE?1?:", "SENS:", head)
        head = re.sub(r"^CALCULATE1?:|^CALC1:", "CALC:", head)

        def unknown():
            self._err(-113, f"Undefined header; {cmd}")
            if query:
                raise VisaIOError(constants.StatusCode.error_timeout)

        # ---- common ----
        if head == "*IDN?":
            return "Keysight Technologies,N5235A,MY12345678,A.10.49.08"
        if head == "*OPT?":
            return f'"{self.options}"'
        if head in ("*CLS",):
            self.errors.clear(); return
        if head == "*OPC?":
            return "1"
        if head in ("SYST:ERR?", "SYSTEM:ERROR?", "SYST:ERR:NEXT?"):
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if head in ("ABOR", "ABORT"):
            self.sweep_mode = "HOLD"; return
        if head.startswith("DISP"):
            return "1" if query else None
        # ---- format ----
        if head in ("FORM", "FORM:DATA", "FORMAT", "FORMAT:DATA"):
            if query:
                return self.form.replace("ASC", "ASC,+0") if self.form.startswith("ASC") else self.form
            a = arg.upper().replace(" ", "")
            if a.startswith("REAL,64"):
                self.form = "REAL,64"
            elif a.startswith("REAL,32") or a == "REAL":
                self.form = "REAL,32"
            elif a.startswith("ASC"):
                self.form = "ASC"
            else:
                unknown()
            return
        if head in ("FORM?", "FORM:DATA?"):
            return self.form
        if head in ("FORM:BORD",):
            self.border = arg.upper().strip(); return
        # ---- output / power ----
        if head in ("OUTP", "OUTPUT", "OUTP?", "OUTPUT?"):
            if head.endswith("?"):
                return "1" if self.output else "0"
            self.output = self._bool(arg); return
        if re.match(r"^SOUR(CE)?\d?:POW(ER)?\d?\??$", head):
            if head.endswith("?"):
                return f"{self.power:+.6E}"
            self.power = round(self._num(arg), 2); return
        # ---- sense basics ----
        simple = {
            "SENS:BAND": "ifbw", "SENS:BWID": "ifbw",
            "SENS:FREQ:STAR": "start", "SENS:FREQ:STOP": "stop",
            "SENS:AVER:COUN": "aver_count", "SENS:SWE:GRO:COUN": "group_count",
            "CALC:CORR:EDEL:TIME": "edelay",
        }
        hq = head.rstrip("?")
        if hq in simple:
            attr = simple[hq]
            if head.endswith("?"):
                if attr in ("start", "stop") and self.sweep_type == "SEGM":
                    f = self._stimulus()
                    v = f[0] if attr == "start" else f[-1]
                    return f"{v:+.12E}"
                v = getattr(self, attr)
                return f"{v:+.12E}" if isinstance(v, float) else f"+{int(v)}"
            v = self._num(arg)
            if attr in ("aver_count", "group_count"):
                v = int(v)
            if attr in ("start", "stop"):
                v = float(round(v))
            setattr(self, attr, v)
            return
        if hq == "SENS:FREQ:CENT":
            if head.endswith("?"):
                return f"{(self.start+self.stop)/2:+.12E}"
            c = self._num(arg); span = self.stop - self.start
            self.start, self.stop = c - span / 2, c + span / 2; return
        if hq == "SENS:FREQ:SPAN":
            if head.endswith("?"):
                return f"{self.stop-self.start:+.12E}"
            s = self._num(arg); c = (self.start + self.stop) / 2
            self.start, self.stop = c - s / 2, c + s / 2; return
        if hq == "SENS:FREQ:CW":
            return "+1.0E9" if head.endswith("?") else None
        if hq == "SENS:SWE:POIN":
            if head.endswith("?"):
                return f"+{self._total_points()}"
            n = int(self._num(arg))
            if not 1 <= n <= self.max_points:
                self._err(-222, "Data out of range"); return
            self.points = n; return
        if hq in ("SENS:AVER", "SENS:AVER:STAT"):
            if head.endswith("?"):
                return "1" if self.aver else "0"
            self.aver = self._bool(arg); return
        if head == "SENS:AVER:CLE":
            return
        if hq == "SENS:SWE:TIME":
            return f"{self._total_points()/self.ifbw:+.6E}" if head.endswith("?") else None
        if hq == "SENS:SWE:TYPE":
            if head.endswith("?"):
                return self.sweep_type
            t = arg.upper().strip()
            t = {"SEGMENT": "SEGM", "LINEAR": "LIN", "LOGARITHMIC": "LOG"}.get(t, t)
            if t == "SEGM" and not self._active_segments():
                self._err(-221, "Settings conflict; no segments ON")
                self.sweep_type = "LIN"; return
            if t not in ("LIN", "LOG", "POW", "CW", "SEGM", "PHAS"):
                unknown(); return
            self.sweep_type = t; return
        if hq == "SENS:SWE:MODE":
            if head.endswith("?"):
                if self._pending_hold:
                    self._pending_hold -= 1
                    if self._pending_hold == 0:
                        self.sweep_mode = "HOLD"
                    return "SING"
                return self.sweep_mode
            mode = arg.upper().strip()
            mode = {"SINGLE": "SING", "GROUPS": "GRO", "CONTINUOUS": "CONT"}.get(mode, mode)
            if mode in ("SING", "GRO"):
                if self.sweep_type == "SEGM" and not self._validate_segments():
                    self.sweep_mode = "HOLD"; return
                self._acquire()
                self._pending_hold = 1
            self.sweep_mode = mode if mode not in ("SING", "GRO") else mode
            return
        if hq == "TRIG:SOUR":
            if head.endswith("?"):
                return self.trig_source
            self.trig_source = arg.upper().strip(); return
        # ---- stimulus readback ----
        if head in ("SENS:X?", "SENS:X:VAL?", "CALC:X?", "CALC:X:VAL?"):
            return self._block(self._stimulus())
        # ---- traces ----
        if hq == "CALC:PAR:MNUM":
            if head.endswith("?"):
                return f"+{self.mnum}"
            self.mnum = int(self._num(arg)); return
        if head == "CALC:PAR:CAT:EXT?":
            return '"CH1_S21_1,S21"'
        if head in ("CALC:PAR:SEL",):
            return
        if head == "CALC:PAR:MOD:EXT":
            return
        if hq == "CALC:FORM":
            if head.endswith("?"):
                return self.calc_form
            self.calc_form = arg.upper().strip(); return
        if head == "CALC:DATA?":
            if arg.upper().strip() != "FDATA":
                unknown(); return
            return self._block(self._formatted())
        # ---- segments ----
        m = re.match(r"^SENS:SEGM(\d*):(.+)$", head) or (
            re.match(r"^SENS:SEGM(\d*)$", head) and re.match(r"^SENS:SEGM(\d*)()$", head))
        if m:
            return self._segment(m.group(1), m.group(2), arg, head, query, unknown)
        unknown()

    def _segment(self, num, sub, arg, head, query, unknown):
        n = int(num) if num else 1
        isq = head.endswith("?")
        sub = sub.rstrip("?")
        if sub == "DEL:ALL":
            self.segments.clear(); self.sweep_type = "LIN"; return
        if sub == "COUN":
            return f"+{len(self.segments)}"
        if sub == "ADD":
            if n < 1 or n > len(self.segments) + 1:
                self._err(-222, "Segment number out of range"); return
            prev = self.segments[n - 2] if n >= 2 else None
            st = prev.stop if prev else self.start
            self.segments.insert(n - 1, Segment(state=False, points=21, start=st, stop=st))
            return
        if sub == "ARB":
            if isq: return "1" if self.seg_arb else "0"
            self.seg_arb = self._bool(arg); return
        if sub in ("BWID:CONT", "BWID:RES:CONT", "BAND:CONT"):
            if isq: return "1" if self.seg_bw_ctrl else "0"
            self.seg_bw_ctrl = self._bool(arg); return
        if sub in ("POW:CONT", "POW:LEV:CONT"):
            if isq: return "1" if self.seg_pow_ctrl else "0"
            self.seg_pow_ctrl = self._bool(arg); return
        if sub == "LIST":
            if isq:
                vals = []
                for s in self.segments:
                    vals += [1.0 if s.state else 0.0, s.points, s.start, s.stop,
                             self.ifbw, 0.0, self.power, self.power]
                return self._block(vals)
            return self._seg_list(arg)
        if n < 1 or n > len(self.segments):
            self._err(-222, "Segment does not exist")
            if query: raise VisaIOError(constants.StatusCode.error_timeout)
            return
        s = self.segments[n - 1]
        if sub in ("", "STAT"):
            if isq: return "1" if s.state else "0"
            s.state = self._bool(arg); return
        if sub == "FREQ:STAR":
            if isq: return f"{s.start:+.12E}"
            s.start = float(round(self._num(arg)))
            if s.stop < s.start: s.stop = s.start
            return
        if sub == "FREQ:STOP":
            if isq: return f"{s.stop:+.12E}"
            s.stop = float(round(self._num(arg)))
            if s.start > s.stop: s.start = s.stop
            return
        if sub == "SWE:POIN":
            if isq: return f"+{s.points}"
            s.points = int(self._num(arg)); return
        unknown()

    def _seg_list(self, arg):
        # SENS:SEGM:LIST SSTOP,<n>,<data...>   (data must follow FORM:DATA)
        parts = [p.strip() for p in arg.split(",")]
        if len(parts) < 2 or parts[0].upper() not in ("SSTOP", "CSPAN"):
            self._err(-109, "Missing parameter"); return
        if not self.form.startswith("ASC"):
            # the real PNA would try to parse a binary block here
            self._err(-161, "Invalid block data; FORM is binary"); return
        mode = parts[0].upper(); nseg = int(float(parts[1]))
        data = [float(p) for p in parts[2:]]
        if nseg <= 0 or len(data) % nseg:
            self._err(-109, "Wrong number of values"); return
        per = len(data) // nseg
        if per < 4 or per > 8:
            self._err(-109, "Wrong number of values per segment"); return
        segs = []
        for i in range(nseg):
            st, npts, a, b = data[i * per: i * per + 4]
            if mode == "CSPAN":
                a, b = a - b / 2, a + b / 2
            segs.append(Segment(state=bool(st), points=int(npts),
                                start=float(round(a)), stop=float(round(b))))
        self.segments = segs
