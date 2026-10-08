"""
tools/pna_segment_smoke_test.py
===============================
Smoke test of PNA *segment sweep* through the project's driver
(core/drivers/keysight_pna.py). No resonator needed: with nothing (or just
noise) on the ports it only checks that the instrument accepts the segment
commands, sweeps, and returns data of the right size without SCPI errors.

Run from the project root (the folder that contains core/):

    python -m tools.pna_segment_smoke_test --address TCPIP0::<ip>::inst0::INSTR

or set PNA_ADDRESS near the top of this file and just run it (also from an IDE).

Options (all have safe defaults):
    --fstart 5.0e9 --fstop 5.01e9   window used for every sweep (Hz)
    --points 2001                   points for the linear and HPD-style sweeps
    --power -30                     source power (dBm); RF is on only while sweeping
    --ifbw 1000                     IF bandwidth (Hz)
    --avg 3                         averages for the averaged segment sweep

Steps (each prints PASS / FAIL and the PNA error queue on failure; a failing
step does not stop the later ones):
  1. Connect, report model / options / power limits / trigger source.
  2. Normal linear sweep (baseline: proves the basic setup works).
  3. Segment table sent in ONE command  (SENS:SEGM:LIST, ASCII).
  4. Segment table sent segment by segment (SENS:SEGMn:ADD / FREQ / POIN / STAT).
  5. Driver path set_segment_table(): enter SEGM, stimulus read in 64-bit,
     SENS:SWE:POIN?, one sweep, read magnitude + phase.
  6. Averaged segment sweep (group trigger, --avg sweeps).
  7. HPD-style table (the many-segment table the measurement code builds).
  8. Back to a linear sweep, sweep and read again.
  9. Error queue empty, data format back to REAL,32.

At the end the original start/stop/points/IFBW/trigger source are restored,
the PNA is left on a linear sweep, power at the minimum, RF off.
A log (.txt) and all measured traces (.npz) are written next to where you run
it — send me both if anything fails.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
import traceback

import numpy as np


# ---------------------------------------------------------------------------
# PNA ADDRESS — put your PNA's VISA address here, between the quotes, e.g.
#     PNA_ADDRESS = "TCPIP0::192.168.1.50::inst0::INSTR"     (LAN)
#     PNA_ADDRESS = "GPIB0::16::INSTR"                        (GPIB)
# If left empty, the script uses --address from the command line, and if that
# is missing too, the PNA address saved by the QRes app (Connect window).
# Run with --list to print every VISA address this PC can see.
# ---------------------------------------------------------------------------
PNA_ADDRESS = "TCPIP0::192.168.1.151::5025::SOCKET"


def _resolve_address(cli_value):
    if cli_value:
        return cli_value, "command line"
    if PNA_ADDRESS.strip():
        return PNA_ADDRESS.strip(), "PNA_ADDRESS at the top of this file"
    try:
        from core.settings import settings
        saved = settings.get("network.pna_address")
        if saved:
            return str(saved), "address saved by the QRes app"
    except Exception:
        pass
    return None, None


def _list_resources():
    import pyvisa
    try:
        rm = pyvisa.ResourceManager()
    except Exception as e:
        print(f"Could not open the VISA library: {e}")
        return
    print(f"VISA library: {rm.visalib}")
    res = rm.list_resources()
    if not res:
        print("No VISA instruments found. LAN instruments often do not show up here; "
              "use Keysight Connection Expert, or the PNA's own address "
              "(System > Configure > SICL/GPIB / LAN, e.g. TCPIP0::<ip>::inst0::INSTR).")
    for r in res:
        try:
            idn = rm.open_resource(r, open_timeout=2000).query("*IDN?").strip()
        except Exception as e:
            idn = f"(no reply: {type(e).__name__})"
        print(f"  {r:40s} {idn}")


class Report:
    def __init__(self, log_path):
        self.rows = []
        self.f = open(log_path, "w", encoding="utf-8")

    def say(self, msg=""):
        print(msg, flush=True)
        self.f.write(msg + "\n"); self.f.flush()

    def check(self, step, cond, msg):
        self.rows.append((step, bool(cond), msg))
        self.say(("  PASS  " if cond else "  FAIL  ") + msg)
        return bool(cond)

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", default=None,
                    help="VISA address; optional if PNA_ADDRESS is set at the top of the file")
    ap.add_argument("--list", action="store_true", help="list VISA instruments and exit")
    ap.add_argument("--fstart", type=float, default=5.0e9)
    ap.add_argument("--fstop", type=float, default=5.01e9)
    ap.add_argument("--points", type=int, default=2001)
    ap.add_argument("--power", type=float, default=-30.0)
    ap.add_argument("--ifbw", type=float, default=100.0)
    ap.add_argument("--avg", type=int, default=3)
    a = ap.parse_args()
    if a.list:
        _list_resources()
        return
    a.address, source = _resolve_address(a.address)
    if not a.address:
        print("No PNA address given.\n"
              "  Either edit PNA_ADDRESS at the top of tools/pna_segment_smoke_test.py,\n"
              "  or run:  python -m tools.pna_segment_smoke_test --address TCPIP0::<ip>::inst0::INSTR\n"
              "  To see the addresses this PC can reach:  python -m tools.pna_segment_smoke_test --list")
        sys.exit(2)
    print(f"Using PNA address {a.address}  (from {source})")

    from core.drivers.keysight_pna import KeysightN5235A, KeysightN5245A
    from core.pna_segment import build_hpd_segments

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rep = Report(f"pna_segment_smoke_{stamp}.log.txt")
    traces = {}
    f0, f1 = float(a.fstart), float(a.fstop)
    rep.say(f"PNA segment-sweep smoke test  {stamp}")
    rep.say(f"window {f0/1e9:.6f}–{f1/1e9:.6f} GHz, {a.points} pts, {a.power} dBm, "
            f"IFBW {a.ifbw:g} Hz, avg {a.avg}\n")

    # ---------------------------------------------------------------- helpers
    def errors(pna):
        try:
            return pna.get_errors()
        except Exception as e:
            return [f"(could not read SYST:ERR?: {e})"]

    def run_step(step, title, fn, pna):
        rep.say(f"[{step}] {title}")
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            rep.check(step, False, f"exception: {type(e).__name__}: {e}")
            rep.say("        " + traceback.format_exc().strip().replace("\n", "\n        "))
            errs = errors(pna)
            if errs:
                rep.say(f"        PNA error queue: {errs[:10]}")
        rep.say(f"      ({time.time() - t0:.1f} s)\n")

    def sweep(pna, navg=1, label=""):
        """Trigger one (averaged) sweep, wait for HOLD with a timeout, read mag+phase."""
        pna.auto_sweep(False)
        if navg > 1:
            pna.averages_enabled(True); pna.averages(int(navg))
            pna.reset_averages(); pna.group_trigger_count(int(navg))
            pna.sweep_mode("GRO")
        else:
            pna.averages_enabled(False)
            pna.sweep_mode("SING")
        try:
            est = float(pna.sweep_time()) * max(navg, 1)
        except Exception:
            est = 30.0
        limit = max(30.0, 5 * est + 10)
        t0 = time.time()
        while pna.sweep_mode() != "HOLD":
            if time.time() - t0 > limit:
                pna.write("ABOR")
                raise TimeoutError(f"sweep not finished after {limit:.0f} s "
                                   f"(sweep_time says {est:.2f} s)")
            time.sleep(0.1)
        dt = time.time() - t0
        mag = np.asarray(pna.magnitude(), float)
        ph = np.asarray(pna.phase(), float)
        if label:
            traces[label + "_mag_db"] = mag
            traces[label + "_phase_deg"] = ph
        return mag, ph, dt

    def check_trace(step, mag, ph, n, dt):
        rep.check(step, mag.size == n and ph.size == n,
                  f"magnitude/phase have {mag.size}/{ph.size} points (expected {n}); sweep {dt:.2f} s")
        rep.check(step, np.all(np.isfinite(mag)) and np.all(np.isfinite(ph)),
                  f"all values finite (|S21| {np.nanmin(mag):.1f} … {np.nanmax(mag):.1f} dB, "
                  f"median {np.nanmedian(mag):.1f} dB)")

    def expected_grid(segs, pna):
        return np.concatenate([np.round(np.linspace(s, e, n)) if n > 1 else np.array([round(s)])
                               for s, e, n in pna.normalize_segments(segs)])

    # ---------------------------------------------------------------- connect
    rep.say("[1] Connect")
    try:
        pna = KeysightN5235A("pna_smoke", a.address)
        model = str(pna.IDN().get("model") or "")
        if model.upper().startswith(("N5241", "N5242", "N5244", "N5245", "N5247", "N5249")):
            pna.close()
            pna = KeysightN5245A("pna_smoke", a.address)
    except Exception as e:
        rep.check(1, False, f"could not connect to {a.address}: {type(e).__name__}: {e}")
        rep.say("      Check the address (run with --list), that the PNA is on and reachable "
                "(ping its IP), and that no other program holds the connection.")
        rep.close()
        sys.exit(2)
    rep.check(1, True, f"connected: {pna.IDN()}")
    rep.say(f"      options: {pna.get_options()}")
    rep.say(f"      power limits in driver: {pna.power.vals}")
    old = {}
    for name in ("start", "stop", "points", "if_bandwidth", "trigger_source", "sweep_type", "power"):
        try:
            old[name] = getattr(pna, name)()
        except Exception as e:
            old[name] = None
            rep.say(f"      (could not read {name}: {e})")
    rep.say(f"      current state: {old}")
    stale = errors(pna)
    if stale:
        rep.say(f"      errors already in the queue before the test (cleared): {stale[:10]}")
    if old.get("trigger_source") not in (None, "IMM"):
        rep.say(f"      trigger source is {old['trigger_source']}; setting IMM for the test "
                "(restored at the end)")
        pna.trigger_source("IMM")
    rep.say("")

    third = (f1 - f0) / 3.0
    small = [(f0, f0 + third, 21),
             (f0 + third + 1e3, f1 - third, 201),
             (f1 - third + 1e3, f1, 21)]
    n_small = sum(n for *_, n in small)

    try:
        pna.trace("S21")
        pna.if_bandwidth(a.ifbw)
        pna.power(a.power)

        # ------------------------------------------------------------ 2
        def s2():
            pna.use_linear_sweep(f0, f1, a.points)
            pna.output(True)
            mag, ph, dt = sweep(pna, label="linear_before")
            traces["linear_before_freq_hz"] = np.linspace(f0, f1, a.points)
            check_trace(2, mag, ph, a.points, dt)
            rep.check(2, not errors(pna), "no SCPI errors")
        run_step(2, "Baseline linear sweep", s2, pna)

        # ------------------------------------------------------------ 3
        def s3():
            pna.get_errors()
            pna.write("SENS:SEGM:ARB OFF")
            pna.write("SENS:SEGM:BWID:CONT OFF")
            pna.write("SENS:SEGM:POW:CONT OFF")
            pna._write_segment_list(pna.normalize_segments(small))
            errs = errors(pna)
            rep.check(3, not errs, f"SENS:SEGM:LIST accepted (errors: {errs[:5] or 'none'})")
            cnt = pna.segment_count()
            rep.check(3, cnt == 3, f"SENS:SEGM:COUN? = {cnt} (expected 3)")
            back = pna.get_segment_table()
            rep.say(f"      read back: {back}")
            want = pna.normalize_segments(small)
            rep.check(3, len(back) == 3 and all(abs(x[0] - y[0]) <= 1 and abs(x[1] - y[1]) <= 1
                                                and x[2] == y[2] for x, y in zip(back, want)),
                      "table read back matches")
        run_step(3, "Segment table in one command (SENS:SEGM:LIST)", s3, pna)

        # ------------------------------------------------------------ 4
        def s4():
            pna.get_errors()
            pna._write_segments_individually(pna.normalize_segments(small))
            errs = errors(pna)
            rep.check(4, not errs, f"per-segment commands accepted (errors: {errs[:5] or 'none'})")
            cnt = pna.segment_count()
            rep.check(4, cnt == 3, f"SENS:SEGM:COUN? = {cnt} (expected 3)")
            back = pna.get_segment_table()
            rep.say(f"      read back: {back}")
            want = pna.normalize_segments(small)
            rep.check(4, len(back) == 3 and all(abs(x[0] - y[0]) <= 1 and abs(x[1] - y[1]) <= 1
                                                and x[2] == y[2] for x, y in zip(back, want)),
                      "table read back matches")
        run_step(4, "Segment table segment by segment (SENS:SEGMn:...)", s4, pna)

        # ------------------------------------------------------------ 5
        def s5():
            stim = pna.set_segment_table(small)      # driver path, verifies itself
            rep.check(5, pna.sweep_type() == "SEGM", f"sweep type = {pna.sweep_type()}")
            truth = expected_grid(small, pna)
            rep.check(5, stim.size == truth.size and np.max(np.abs(stim - truth)) <= 1,
                      f"64-bit stimulus = programmed grid ({stim.size} pts, max dev "
                      f"{np.max(np.abs(stim - truth)) if stim.size == truth.size else float('nan'):.1f} Hz)")
            f32 = np.asarray(pna.visa_handle.query_binary_values(
                "SENS:X?", datatype="f", is_big_endian=True), float)
            if f32.size == truth.size:
                rep.say(f"      info: same axis read in 32-bit would be off by up to "
                        f"{np.max(np.abs(f32 - truth)):.0f} Hz")
            npts = int(pna.points())
            rep.say(f"      info: SENS:SWE:POIN? in segment mode returns {npts} "
                    f"(total is {n_small}; the driver does not depend on this)")
            rep.check(5, int(pna.trace_points()) == n_small, f"trace_points = {pna.trace_points()}")
            pna.output(True)
            mag, ph, dt = sweep(pna, label="segment_small")
            traces["segment_small_freq_hz"] = stim
            check_trace(5, mag, ph, n_small, dt)
            rep.check(5, not errors(pna), "no SCPI errors")
        run_step(5, "Driver segment sweep: program, sweep, read", s5, pna)

        # ------------------------------------------------------------ 6
        def s6():
            if pna.sweep_type() != "SEGM":
                pna.set_segment_table(small)
            pna.output(True)
            mag, ph, dt = sweep(pna, navg=a.avg, label="segment_avg")
            check_trace(6, mag, ph, n_small, dt)
            rep.check(6, not errors(pna), "no SCPI errors")
            pna.averages_enabled(False)
        run_step(6, f"Averaged segment sweep ({a.avg} averages)", s6, pna)

        # ------------------------------------------------------------ 7
        def s7():
            fr = 0.5 * (f0 + f1)
            Ql = 40.0 * fr / (f1 - f0)                # pretend span = 40 linewidths
            plan = build_hpd_segments(f0, f1, fr, Ql, a.points)
            rep.say(f"      HPD-style plan: {len(plan.segments)} segments, {plan.n_points} points")
            t0 = time.time()
            stim = pna.set_segment_table(plan.segments)
            rep.say(f"      programming + read-back took {time.time() - t0:.2f} s")
            rep.check(7, stim.size == plan.n_points and np.max(np.abs(stim - plan.frequencies)) <= 1,
                      f"stimulus matches plan ({stim.size} pts)")
            pna.output(True)
            mag, ph, dt = sweep(pna, label="segment_hpd")
            traces["segment_hpd_freq_hz"] = stim
            check_trace(7, mag, ph, plan.n_points, dt)
            rep.check(7, not errors(pna), "no SCPI errors")
        run_step(7, "HPD-style many-segment table", s7, pna)

        # ------------------------------------------------------------ 8
        def s8():
            pna.use_linear_sweep(f0, f1, a.points)
            rep.check(8, pna.sweep_type() == "LIN", f"sweep type = {pna.sweep_type()}")
            pna.output(True)
            mag, ph, dt = sweep(pna, label="linear_after")
            check_trace(8, mag, ph, a.points, dt)
            rep.check(8, not errors(pna), "no SCPI errors")
        run_step(8, "Back to linear sweep", s8, pna)

        # ------------------------------------------------------------ 9
        def s9():
            errs = errors(pna)
            rep.check(9, not errs, f"error queue empty ({errs[:5] or 'none'})")
            form = pna.ask("FORM?").strip()
            rep.check(9, "32" in form, f"data format = {form}")
        run_step(9, "Final instrument state", s9, pna)

    finally:
        rep.say("Cleanup: abort, linear sweep, restore settings, minimum power, RF off")
        for action in (
            lambda: pna.write("ABOR"),
            lambda: pna.averages_enabled(False),
            lambda: pna.use_linear_sweep(),
            lambda: old["start"] is not None and pna.start(old["start"]),
            lambda: old["stop"] is not None and pna.stop(old["stop"]),
            lambda: old["points"] is not None and pna.points(int(old["points"])),
            lambda: old["if_bandwidth"] is not None and pna.if_bandwidth(old["if_bandwidth"]),
            lambda: old["trigger_source"] is not None and pna.trigger_source(old["trigger_source"]),
            lambda: pna.power(pna.power.vals._min_value),
            lambda: pna.output(False),
        ):
            try:
                action()
            except Exception as e:
                rep.say(f"  cleanup step failed: {e}")
        try:
            np.savez(f"pna_segment_smoke_{stamp}_traces.npz", **traces)
            rep.say(f"traces saved to pna_segment_smoke_{stamp}_traces.npz")
        except Exception as e:
            rep.say(f"could not save traces: {e}")
        pna.close()

    n_ok = sum(ok for _, ok, _ in rep.rows)
    rep.say(f"\nSUMMARY: {n_ok}/{len(rep.rows)} checks passed")
    for step, ok, msg in rep.rows:
        if not ok:
            rep.say(f"  step {step}: {msg}")
    rep.say(f"log: pna_segment_smoke_{stamp}.log.txt")
    rep.close()
    sys.exit(0 if n_ok == len(rep.rows) else 1)


if __name__ == "__main__":
    main()