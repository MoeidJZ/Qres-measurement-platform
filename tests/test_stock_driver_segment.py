"""
Step 1: does the *stock* QCoDeS N52xx driver support segment sweeps?

We program a 3-segment table with raw SCPI (the driver has no segment API),
switch the sweep type to SEGM (the driver's sweep_type validator does accept
"SEGM"), and then try to read data and the frequency axis the way the
measurement code does.
"""
import sys, traceback
import numpy as np
from qcodes.instrument_drivers.Keysight import KeysightN5245A

from harness import make_pna, FakePNAHandle

h = FakePNAHandle()
pna = make_pna(KeysightN5245A, h)

print("driver power limits:", pna.power.vals)

# 1) segment API present?
api = [n for n in dir(pna) if "seg" in n.lower()]
print("segment-related attributes on driver:", api or "NONE")

# 2) program a table by hand (raw SCPI) and switch to SEGM
pna.write("SENS:SEGM:DEL:ALL")
segs = [(5.1230e9, 5.1234e9, 11), (5.12341e9, 5.12350e9, 101), (5.12351e9, 5.1240e9, 11)]
for i, (a, b, n) in enumerate(segs, 1):
    pna.write(f"SENS:SEGM{i}:ADD")
    pna.write(f"SENS:SEGM{i}:FREQ:STAR {a}")
    pna.write(f"SENS:SEGM{i}:FREQ:STOP {b}")
    pna.write(f"SENS:SEGM{i}:SWE:POIN {n}")
    pna.write(f"SENS:SEGM{i}:STAT ON")
pna.sweep_type("SEGM")
print("sweep_type ->", pna.sweep_type(), "| points ->", pna.points())

# 3) the measurement-code path: auto_sweep off, trigger, read magnitude
pna.auto_sweep(False)
pna.sweep_mode("SING")
while pna.sweep_mode() != "HOLD":
    pass
results = {}
for what, fn in [("pna.magnitude()", lambda: pna.magnitude()),
                 ("pna.phase()", lambda: pna.phase()),
                 ("pna.frequency_axis()", lambda: pna.frequency_axis())]:
    try:
        v = np.asarray(fn())
        results[what] = f"OK, {v.size} values"
        if what == "pna.frequency_axis()":
            truth = np.concatenate([np.linspace(a, b, n) for a, b, n in segs])
            err = np.max(np.abs(v - truth))
            results[what] += f", max error vs real stimulus = {err/1e3:.1f} kHz"
    except Exception as e:
        results[what] = f"FAILS: {type(e).__name__}: {e}"
for k, v in results.items():
    print(f"  {k:24s} {v}")

# 4) stimulus read with the format the driver sets (REAL,32)
f32 = np.array(pna.visa_handle.query_binary_values("SENS:X?", datatype="f", is_big_endian=True))
truth = np.round(np.concatenate([np.linspace(a, b, n) for a, b, n in segs]))
print(f"  SENS:X? as REAL,32       max error = {np.max(np.abs(f32-truth)):.0f} Hz "
      f"(distinct values {np.unique(f32).size}/{truth.size})")
pna.close()
