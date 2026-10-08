"""Step 2: the extended driver in segment mode (simulated N5235A)."""
import sys
sys.path.insert(0, "..")
import numpy as np
from qcodes.dataset import Measurement, initialise_or_create_database_at, load_or_create_experiment
from qcodes import Station

from harness import make_pna, FakePNAHandle
from core.drivers.keysight_pna import KeysightN5235A, PNAError

ok = True
def check(cond, msg):
    global ok
    print(("  PASS " if cond else "  FAIL ") + msg)
    ok &= bool(cond)

h = FakePNAHandle(options="216")
pna = make_pna(KeysightN5235A, h)

# power limits with option 216
check(pna.power.vals._min_value == -90, f"option 216 unlocks -90 dBm ({pna.power.vals})")
pna.power(-60)
check(abs(pna.power() + 60) < 1e-9, "pna.power(-60) accepted")

# linear sweep still works exactly as before
pna.start(5.12e9); pna.stop(5.13e9); pna.points(201)
pna.auto_sweep(False); pna.sweep_mode("SING")
while pna.sweep_mode() != "HOLD": pass
m = pna.magnitude()
check(m.size == 201, "LIN: magnitude has 201 points")
check(np.allclose(pna.frequency_axis(), np.linspace(5.12e9, 5.13e9, 201)), "LIN: frequency_axis unchanged")

# segment table
segs = [(5.1230e9, 5.1234e9, 11), (5.1234001e9, 5.12350e9, 1001), (5.1235001e9, 5.1240e9, 11)]
stim = pna.set_segment_table(segs)
truth = np.concatenate([np.round(np.linspace(a, b, n)) for a, b, n in segs])
check(pna.sweep_type() == "SEGM", "sweep type is SEGM")
check(stim.size == truth.size and np.max(np.abs(stim - truth)) == 0,
      f"stimulus read back exactly in 64-bit ({stim.size} pts, max err {np.max(np.abs(stim-truth)):.0f} Hz)")
check(pna.trace_points() == truth.size, "trace_points == total segment points")
check(pna.get_segment_table() == [(float(round(a)), float(round(b)), n) for a, b, n in segs],
      "segment table read-back matches")
check(h.form == "REAL,32", "data format restored to REAL,32 after stimulus/list I/O")
check(any(c.startswith("SENS:SEGM:LIST SSTOP,3,") for c in h.log), "table sent as one SEGM:LIST")

pna.sweep_mode("SING")
while pna.sweep_mode() != "HOLD": pass
mag, ph = pna.magnitude(), pna.phase()
check(mag.size == truth.size and ph.size == truth.size, "SEGM: magnitude/phase read OK (stock driver raised here)")
sp = pna.magnitude.setpoints[0]
check(sp is pna.stimulus_axis and np.allclose(sp(), truth), "SEGM: setpoints are the real stimulus")

# saving through the QCoDeS dataset with the driver's own setpoints
initialise_or_create_database_at("/tmp/claude-0/segtest.db")
exp = load_or_create_experiment("segtest", sample_name="fake")
st = Station(pna)
meas = Measurement(exp=exp, station=st)
meas.register_parameter(pna.magnitude)
with meas.run() as ds:
    ds.add_result((pna.stimulus_axis, pna.stimulus_axis()), (pna.magnitude, mag))
    rid = ds.run_id
d = ds.dataset.get_parameter_data()["pna_tr1_magnitude"]
check(np.allclose(d["pna_stimulus_axis"].ravel(), truth), "dataset stores the non-uniform frequencies")

# invalid tables are refused *before* touching the instrument
for bad, why in [([(5e9, 5.1e9, 11), (5.05e9, 5.2e9, 11)], "overlap"),
                 ([(5e9, 5e9 + 50, 101)], "sub-Hz spacing"),
                 ([(5e9, 5.1e9, 60000), (5.2e9, 5.3e9, 60000)], "too many points")]:
    try:
        pna.set_segment_table(bad); check(False, f"rejects {why}")
    except ValueError:
        check(True, f"rejects {why}")

# fallback path: make SEGM:LIST fail on the 'instrument'
orig = h._seg_list
h._seg_list = lambda arg: h._err(-113, "Undefined header")
stim2 = pna.set_segment_table(segs)
check(np.array_equal(stim2, truth), "per-segment fallback programs the same table")
h._seg_list = orig

# back to linear
pna.use_linear_sweep(5.12e9, 5.13e9, 401)
check(pna.sweep_type() == "LIN" and pna.trace_points() == 401, "use_linear_sweep restores LIN")
check(pna.get_errors() == [], "no SCPI errors left in the queue")
pna.close()
print("ALL PASS" if ok else "SOME FAILURES")
sys.exit(0 if ok else 1)
