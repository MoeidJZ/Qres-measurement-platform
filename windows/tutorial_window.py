"""
windows/tutorial_window.py
==========================
Lightweight, scrollable how-to guides — one for measurement, one for analysis.
They point the user at the workflow order and where each parameter lives, rather
than running anything. Open from the welcome page or the platform's Help menu.
"""

from __future__ import annotations

from core import theme

from PyQt5.QtWidgets import QMainWindow, QTextBrowser


MEASUREMENT_HTML = """
<h2 style='color:#1f6feb'>Measurement — how it flows</h2>
<p>The platform walks you through five steps. You stay in control at every one;
nothing closes or starts on its own. Steps unlock as you complete the previous
one, and you can reopen any window from the workflow buttons.</p>

<h3 style='color:#1f6feb'>Before you start</h3>
<ul>
<li><b>Welcome → pick your fridge</b> (Proteox, Teslatron, or Manual). For
Proteox you'll confirm the DECS popup; for Teslatron you can edit the address if
it doesn't connect.</li>
<li><b>Connect the PNA</b> (optional). PNA-dependent buttons stay greyed until
it's connected. Edit the address and, if it works, choose whether to save it.</li>
<li><b>Database &amp; sample</b> — pick the .db all runs are written to and a
sample name. Both feed the run names.</li>
<li><b>Instrument Control</b> (toolbar) — a dockable panel to read/set fridge
temperatures, heaters, needle valve, pressure at any time, even mid-measurement.
<b>PNA Parameters</b> (toolbar) — start/stop, points (max 100001), IF bandwidth,
power, averaging, trace; locked while a sweep runs.</li>
</ul>

<h3 style='color:#1f6feb'>1 · Wideband scan</h3>
<p>Set start/stop, points, IF bandwidth, power (averaging usually off). Optionally
tick <b>Wait for reference temperature</b>, choose the gating sensor and target;
it fires when the temperature is within +100% of target (e.g. ≤100 mK for a
50 mK target), checking on your chosen interval. <b>Run now</b> skips the wait;
<b>Stop</b> hands control back. When the trace appears, <b>hover</b> to read the
exact frequency and <b>click</b> each resonance dip to drop a marker (click again
to remove). <b>Confirm picks</b> moves on.</p>

<h3 style='color:#1f6feb'>2 · Span picker</h3>
<p>Each pick becomes Res 1…N (low→high). For each, the existing wideband data is
zoomed (default 2 MHz — adjust + <b>Re-show</b>). Drag the shaded region edges to
set the measurement start/stop, keeping them far enough from resonance that
|S21| has settled. Per resonator: <b>Confirm</b>, <b>Ignore</b> (keep, don't
measure), or <b>Discard</b> (drop a dud). <b>Confirm all</b> continues.</p>

<h3 style='color:#1f6feb'>3 · Quality assessment</h3>
<p>One high-power, single-average sweep per resonator. Set power, IF bandwidth,
points. Each trace is auto-fitted (Qi shown). If a fit looks wrong, drag the
green crop region to the clean part and <b>Re-fit (cropped)</b>. Tick the good
resonators to carry into the power sweep.</p>

<h3 style='color:#1f6feb'>4 · Power-dependent</h3>
<p>Set the power vector (start/stop/step) and the per-power averaging/IF-bandwidth
table — type values or use the <b>rule</b> (values at lowest and highest power,
interpolated; averages 1–15, IF bw 1–1000 Hz). Choose <b>SPD</b> (linear,
low→high) or <b>HPD</b> (segment sweep, high→low; table rebuilt from the running
fit, seeded by step 3). <b>Points</b> sets the resolution (HPD segments derive
from it). Qi-vs-power plots live.</p>

<h3 style='color:#1f6feb'>5 · Temperature- &amp; power-dependent</h3>
<p>Set the temperature vector (mK/K, reversible) and a stability criterion. At
each temperature the setpoint is set <i>and read back to confirm it matches</i>,
then the loop waits for stability before running the full power sweep above.
Every temperature is saved under its own run id.</p>
"""

ANALYSIS_HTML = """
<h2 style='color:#1f6feb'>Analysis — how it flows</h2>
<p>The analysis window fits resonator traces with the notch-port circle fit and
exports the results. It reads any .db — including the one a measurement is
writing right now — so you can fit while a long sweep runs.</p>

<h3 style='color:#1f6feb'>Open a database</h3>
<ul>
<li><b>Open .db…</b> — pick the live database or any other.</li>
<li>You'll be asked for an <b>export folder</b> and a <b>base name</b>. These are
remembered per database; opening a different .db asks again.</li>
<li><b>Refresh runs</b> re-reads the list — useful while a measurement keeps
adding runs to the same file.</li>
</ul>

<h3 style='color:#1f6feb'>Fit a trace</h3>
<ul>
<li>Select a run. Multi-power runs show a <b>Power</b> selector; pick a level.</li>
<li>The trace is auto-fitted and fr / Qi / Qc / Ql are shown.</li>
<li>If the fit is poor, drag the green <b>crop region</b> to the clean part of the
resonance and <b>Re-fit (cropped)</b>. <b>Fit full</b> resets the window.
<b>Smoothing σ</b> can help very noisy low-power traces.</li>
</ul>

<h3 style='color:#1f6feb'>Export</h3>
<ul>
<li><b>Export this fit</b> writes the trace to
<code>&lt;base&gt;_&lt;Res&gt;_&lt;T&gt;_&lt;P&gt;.csv</code> (frequency,
magnitude, phase, and the fitted curve) and appends the fit parameters to
<code>&lt;base&gt;_fitting_parameters.csv</code>.</li>
<li><b>Fit &amp; export all powers</b> does this for every power in a multi-power
run at once.</li>
</ul>

<h3 style='color:#1f6feb'>Tip</h3>
<p>The circle fit is most accurate when the frequency window spans roughly a few
linewidths and the background is flat — crop out any non-resonant structure
before re-fitting.</p>
"""


class TutorialWindow(QMainWindow):
    def __init__(self, kind: str = "measurement", parent=None):
        super().__init__(parent)
        title = "Tutorial — Measurement" if kind == "measurement" else "Tutorial — Analysis"
        self.setWindowTitle(title)
        self.setMinimumSize(680, 720)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(MEASUREMENT_HTML if kind == "measurement" else ANALYSIS_HTML)
        browser.setStyleSheet(f"QTextBrowser{{background:{theme.hx('panel')}; "
                              f"color:{theme.hx('text')}; border:none; padding:14px;}}")
        self.setCentralWidget(browser)
