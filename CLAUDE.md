# CLAUDE.md — CRISPy

Multi-device ASI CRISP autofocus plugin for the pymmcore-gui ecosystem. Controls
N CRISP autofocus axes on ASI Tiger controllers via pymmcore-plus.

## Orchestration rule

- Before implementing anything non-trivial, write the execution plan to
  `.claude/current_plan.md` and keep it updated as work proceeds. One feature per
  file; git history keeps the earlier plans.

## Commands (run from this repo root via `uv`)

- `uv run pytest -q` — unit tests. They are Qt-free and hardware-free (`MagicMock` or
  small fake cores), so they run anywhere `uv sync` works.
- `just demo` — launch the standalone UI demo harness (`run_demo.py`). It needs a Qt
  binding in the environment; the lockfile has none (the host supplies it), so
  outside the pymmcore-gui env use `uv run --with PyQt6 python run_demo.py`.
- `just fix` — ruff check --fix, ruff format, mdformat. `ruff` is not a project
  dependency; install it separately (or `uvx ruff`).
- Installed editable into the pymmcore-gui env; it appears there as a Plugins entry.

## Directory map (`src/CRISPy/`)

- `discovery.py` — `discover_crisp_devices()`: find ASITiger AutoFocus axes;
  `find_paired_actuator_label()` / `classify_focus_actuator()`: pair an axis with its
  stage (same axis letter + hex address) and call it piezo or stage
- `controller.py` — `ASICrispController`: per-device property read/write, lock ops,
  calibration gain/range/sensitivity (`counts_per_um()`), the paired
  `actuator_label`, joystick lockout, lock-offset setpoint
- `ui.py` — `CrispControlPanel`: Qt-native control/calibration/telemetry panel with
  the Dither / Focus curve / Hold test tabs
- `dither.py` — Qt-free Dither assistant: `read_dither_sample`, `DitherTracker`
  (median |Err|, steady-only peak-hold, Sum band check)
- `focus_curve.py` — Qt-free `run_focus_curve` (step the actuator, read the error,
  always return to start) and `analyze_focus_curve` (lock point, slope, peaks,
  capture range, noise floor)
- `hold.py` — Qt-free `HoldTest` (records actuator position + error while locked) and
  `analyze_hold` (drift / wander / jitter)
- `sampling.py` — the shared fast reader thread (`Sampler`) and read helpers
- `plot.py` — `LinePlot`, a small QPainter plot (no plotting dependency)
- `plugin.py` — `launch_crisp_plugin()`: layout orchestration (1=panel, 2=horizontal
  splitter, 3+=tabs)

## Architectural boundaries (do not cross)

- **Go through the Core.** All device I/O uses the shared `CMMCorePlus` instance.
  Never open the serial/COM port directly.
- **Per-device, autonomous locking.** Each panel writes its own device's
  `"CRISP State"`; the Tiger card servos autonomously. Never use the Core
  continuous-autofocus API for dual-axis locking.
- **Poll sensors sparingly, off the GUI thread.** The adapter never emits
  `propertyChanged` for read-only sensor values (SNR, Dither Error, Sum, ...), so the
  panel polls them with `AsyncPoller` on a worker thread. Every tick reads only the
  state, Dither Error and Sum; the rest is a "full read" every 10 s, after a button and
  on a state change. Poll only while the panel is visible, auto-pause during MDA
  (`mda.events.sequenceStarted/Finished`), never read the serial port from the GUI
  thread, and keep to one reader at a time.
- **The diagnostic tools are the one exception, one at a time.** The Dither assistant,
  Focus curve and Hold test each claim the port (`_begin_tool` / `_end_tool`), which
  pauses the slow poll; they stop on hide, close and MDA start. Don't run a tool and the
  slow poll together, or two tools at once.
- **Measure, don't tune.** The tools never write lock settings (Averages, Update rate,
  Loop Gain, LED). The user changes one setting at a time and the Hold test judges it
  (the user's "verify one step at a time on the bench" rule; no closed-loop auto-tuner).
- **Qt via `qtpy`.** Use the `qtpy` abstraction (not a hard PyQt6 import) and reuse
  `pymmcore-widgets` `PropertyWidget` for parameter fields.
- **Stay a plugin.** CRISPy is host-agnostic; don't import pymmcore-gui internals.

## Design decisions worth knowing

Sources: the ASI CRISP manual (docs.asiimaging.com/crisp_manual), ASI's CRISP plugin
page, and the ASITiger adapter source (`DeviceAdapters/ASITiger/ASICRISP.cpp` in
micro-manager/mmCoreAndDevices).

- **The knob is the detector lateral adjustment screw, turned in Dither** for the
  largest *absolute* dither Err (≥ 100 per ASI's plugin, > 200 plenty), with Sum in
  50-80. Moving it gives large temporary values, so the peak only latches when the
  window is steady (its thirds agree within `STEADY_TREND`), not merely smoothed.
- **SNR is measured once, at Log Cal** (`EX Y?` returns the stored value). It can't
  respond to the screw or to lock quality; don't build anything that tracks it live.
  (The removed "Tune" mode did, and was the wrong tool.)
- **While dithering, the state reads "Calibrating" most of the time**: the adapter
  maps the dither sub-states 'g'-'j' and 't' to "Calibrating" and only 'f' to
  "Dither". `dither.DITHER_STATES` includes both.
- **The locked error is in-loop**; it can be near zero while the actuator moves ~100 nm
  (rig log 2026-08-14). Judge lock stability by actuator position (Hold test).
- **Focus curve is software-based** (move the actuator, read `Dither Error`), as ASI's
  plugin does on Tiger, rather than parsing raw `LK F=97` output. Moves are refused
  while locked (they fail with `COD 47`), and the actuator always returns to start.
- **`RefreshPropertyValues` is a software-only, persistent adapter flag** (verified in
  the adapter source): it makes reads of the cached properties (Lock Offset,
  Calibration Gain/Range, parameters) query the controller. It is written once.
  State, SNR, Sum, Dither Error and AGC are always read from the controller.
- **Three different scales; only the Focus curve converts the error to µm.** The error
  readout (`Dither Error` = `LK Y?` = the LCD's Err) is a coarse whole-count number: on
  the rig's Z axis (2026-10-19 focus curve) 8.6 counts/µm, so one count ≈ 117 nm.
  |Cal Gain| / Cal Range was 2564, ~300× that, so Cal Gain (and probably Lock Offset,
  which reads in the thousands) is an internal scale. The dither Err is a third scale
  (|Cal Gain| was 10-13× it on both axes; the manual says it "is actually different
  from the Err signal computed during operation"). The panel converts the error to µm
  only with a measured slope (until Log Cal / Dither). Error noise near one count is
  rounding, so the readout can't show focus noise below one count's worth; the Hold
  test (actuator position) measures it. `controller.set_lock_offset_um` (used by
  pymmcore-gui's per-channel focus offset) uses the Cal Gain figure, which may be right
  for Lock Offset's internal scale but is unverified.
- **The rig's Z focus curve (2026-10-19, ±30 µm) shows two features.** The lock point
  sits on a broad, weak one (crossing −0.8 µm, ~12 counts/µm, error levels off at
  ~+130 all the way to −30 µm, turns over +8.3 µm above). A second, sharper one with the
  opposite slope crosses zero at about +15.5 µm (~6× steeper, rising to ~+300 by
  +26 µm): most likely another reflecting surface coming into CRISP's focus, and the
  edge of the capture range on that side. Why the lock sits on the weak feature is open
  (see the manual's "Offset the locking plane from the imaging plane"). The analysis
  reports a peak only if the curve turns over (falls ≥ `PEAK_DROP` beyond it), and lists
  other zero crossings at least a quarter as steep as the lock point.
- **Objective NA must be the objective each axis moves**: it sets the dither move
  (1.5 µm / NA²). This rig: the Z axis moves the NA 1.49 objective; the piezo moves the
  "snouty" (NA 1.0, beside an NA 0.95 air lens). On 2026-10-19 they were set to 1.30 and
  1.45 (Cal Range 0.887 / 0.714 µm), so the piezo dithered under half the right
  distance. The Cal Range readout shows the implied NA.
- **Parameter widgets ignore the wheel unless focused** (`_WheelGuard`): sliders in a
  scrolling panel otherwise change controller settings when the user scrolls past.
- **The Hold test also runs unlocked** (Ready / Idle): the actuator's own noise and the
  sensor noise, without CRISP driving. An unlocked run ends if CRISP starts driving.
- **`AsyncPoller` lives in `pymmcore_widgets.control._async_poller`**, which is not in
  the latest PyPI release (0.12.1, which `uv.lock` pins); the user's
  `pymmcore-widgets` checkout has it. `CrispControlPanel` only constructs where it is
  available; `run_demo.py` installs a stand-in if it is missing. The tools use their
  own threads, not `AsyncPoller`.
- **An unreadable CRISP state counts as unlocked, and that re-enables the joystick.**
  This is intentional. The joystick is auto-disabled on a lock edge and restored on an
  unlock edge (the restore value comes from the `Z_P` preset in the `Joystick Wheels`
  config group; without that preset the wheel can be disabled but not restored).
  Tool state reads feed the same edge logic. Don't "fix" the failed-read case to keep
  the wheel disabled.
