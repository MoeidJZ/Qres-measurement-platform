"""Glue: build a QCoDeS PNA driver on top of FakePNAHandle (no real VISA)."""
from __future__ import annotations

import contextlib

from qcodes.instrument import Instrument, VisaInstrument

from fake_pna import FakePNAHandle, Resonator  # noqa: F401


@contextlib.contextmanager
def fake_visa(handle: FakePNAHandle):
    orig = VisaInstrument._connect_and_handle_error
    VisaInstrument._connect_and_handle_error = lambda self, address, visalib: handle
    try:
        yield handle
    finally:
        VisaInstrument._connect_and_handle_error = orig


def make_pna(cls, handle: FakePNAHandle, name="pna"):
    Instrument.close_all()
    with fake_visa(handle):
        return cls(name, "TCPIP0::fake-pna::inst0::INSTR")
