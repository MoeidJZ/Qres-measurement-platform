# Simulator tests for segment sweep / HPD

`fake_pna.py` is a strict SCPI emulator of an N5235A (binary blocks in REAL,32/64,
segment-table rules, error queue) with a notch resonator from Eq. (1) of
Baity et al. 2024 and a power-dependent Qi. No VISA or instrument needed.

Run from this folder (needs qcodes, PyQt5, scipy; the fit uses the project's
circuit.py if importable, otherwise `resonator_tools`):

    python test_stock_driver_segment.py   # shows the stock QCoDeS driver failing in SEGM mode
    python test_driver_segment.py         # extended driver: table, 64-bit stimulus, fallbacks
    python test_hpd_plan.py               # HPD point plan vs. the paper's theta mapping
    python test_hpd_end_to_end.py         # full HPD power sweep vs SPD vs truth (~2 min)
