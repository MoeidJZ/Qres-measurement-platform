"""Step 3: HPD plan — follows the paper's theta mapping over the full span."""
import sys
sys.path.insert(0, "..")
import numpy as np
from core.pna_segment import (build_hpd_segments, hpd_frequencies, phase_uniformity,
                              _theta)
from core.drivers.keysight_pna import KeysightPNABase

ok = True
def check(c, m):
    global ok; print(("  PASS " if c else "  FAIL ") + m); ok &= bool(c)

fr, N = 5.123456789e9, 2001
print(f"{'span/lw':>8} {'Ql':>9} {'segs':>5} {'pts':>6} {'unif HPD':>9} {'unif SPD':>9} "
      f"{'pts within ±lw/2':>17} {'SPD same':>9}  notes")
for Ql in (5e4, 5e5, 5e6):
    lw = fr / Ql
    for ratio in (2, 10, 40, 100):
        span = ratio * lw
        f0, f1 = fr - 0.5 * span, fr + 0.5 * span
        p = build_hpd_segments(f0, f1, fr, Ql, N)
        spd = np.linspace(f0, f1, N)
        inside = np.mean(np.abs(p.frequencies - fr) < lw / 2)
        inside_spd = np.mean(np.abs(spd - fr) < lw / 2)
        print(f"{ratio:8.0f} {Ql:9.0e} {len(p.segments):5d} {p.n_points:6d} "
              f"{p.uniformity:9.3f} {phase_uniformity(np.round(spd), fr, Ql):9.3f} "
              f"{inside:17.2%} {inside_spd:9.2%}  {'; '.join(p.notes)}")
        # invariants
        KeysightPNABase.normalize_segments(p.segments)       # valid PNA table
        assert p.frequencies[0] == round(f0) and p.frequencies[-1] == round(f1), "window kept"
        assert np.all(np.diff(p.frequencies) >= 1), "1 Hz resolution"
        assert p.n_points == N or p.capped, "point count"

# the ideal (uncapped) mapping is exactly uniform in theta
Ql = 2e5; lw = fr / Ql
f = hpd_frequencies(fr - 20 * lw, fr + 20 * lw, fr, Ql, 1001, resolution=0)
d = np.diff(_theta(f, fr, Ql))
check(np.allclose(d, d[0]), "ideal HPD: equal phase steps (paper Sec. IV)")

# full window is kept (old code shrank it)
p = build_hpd_segments(fr - 30 * lw, fr + 30 * lw, fr, Ql, 2001)
check(abs(p.frequencies[0] - (fr - 30 * lw)) < 1 and abs(p.frequencies[-1] - (fr + 30 * lw)) < 1,
      "plan covers the full chosen span")
check(p.uniformity < 0.1, f"segmented plan stays homophasal (std/mean of phase step {p.uniformity:.3f})")

# off-centre resonance
p = build_hpd_segments(fr - 10 * lw, fr + 50 * lw, fr, Ql, 2001)
check(p.frequencies[0] < fr - 9.99 * lw and p.frequencies[-1] > fr + 49.99 * lw and p.uniformity < 0.1,
      f"asymmetric window handled (unif {p.uniformity:.3f})")

# extreme Q: 1 Hz cap
p = build_hpd_segments(fr - 1500, fr + 1500, fr, 5e7, 2001)
check(p.capped and np.min(np.diff(p.frequencies)) >= 1 and p.n_points == 2001,
      f"Ql=5e7 (lw≈100 Hz): density capped at 1 Hz, still {p.n_points} points, {len(p.segments)} segs")

# impossible: more points than Hz
try:
    build_hpd_segments(fr - 500, fr + 500, fr, 5e7, 2001); check(False, "too dense rejected")
except ValueError:
    check(True, "more points than 1 Hz bins is rejected with a clear error")

print("ALL PASS" if ok else "SOME FAILURES"); sys.exit(0 if ok else 1)
