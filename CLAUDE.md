# CLAUDE.md — CRISPy

Multi-device ASI CRISP autofocus plugin for the pymmcore-gui ecosystem. Controls
N CRISP autofocus axes on ASI Tiger controllers via pymmcore-plus.

## Orchestration rule

- Before implementing anything non-trivial, write the execution plan to
  `.claude/current_plan.md` and keep it updated as work proceeds. One feature per
  file; git history keeps the earlier plans.

## Commands (run from this repo root via `uv`)

- `uv run pytest -q` — unit tests. They are Qt-free and hardware-free (`MagicMock`
  core), so they run anywhere `uv sync` works.
- `just demo` — launch the standalone UI demo harness (`run_demo.py`). It needs a Qt
  binding in the environment; the lockfile has none (the host supplies it), so
  outside the pymmcore-gui env use `uv run --with PyQt6 python run_demo.py`.
- `just fix` — ruff check --fix, ruff format, mdformat. `ruff` is not a project
  dependency; install it separately.
- Installed editable into the pymmcore-gui env; it appears there as a Plugins entry.

## Directory map (`src/CRISPy/`)

- `discovery.py` — `discover_crisp_devices()`: find ASITiger AutoFocus axes;
  `find_paired_actuator_label()` / `classify_focus_actuator()`: pair an axis with its
  stage (same axis letter + hex address) and call it piezo or stage
- `controller.py` — `ASICrispController`: per-device property read/write, lock ops,
  calibration gain/range/sensitivity (`counts_per_um()`), joystick lockout, lock-offset
  setpoint
- `ui.py` — `CrispControlPanel`: Qt-native control/calibration/telemetry panel
- `tuning.py` — Qt-free fast sampling + peak-hold for turning the CRISP detector knob
  (`read_tune_sample`, `TuneTracker`, `TuneSampler`)
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
  panel polls them with `AsyncPoller` on a worker thread. Gate the refresh on
  `hasProperty("RefreshPropertyValues")`, poll only while the panel is visible, and
  auto-pause during MDA (`mda.events.sequenceStarted/Finished`). Never read the serial
  port from the GUI thread, and keep to one reader at a time.
- **Fast Tune mode is the one exception to the polling rule.** Its own sampler thread
  reads only SNR / Sum / Dither Error at high rate. It pauses the slow poll while
  running (one reader on the serial port) and stops on hide, close and MDA start.
  Keep it that way: do not run Tune and the slow poll together.
- **Qt via `qtpy`.** Use the `qtpy` abstraction (not a hard PyQt6 import) and reuse
  `pymmcore-widgets` `PropertyWidget` for parameter fields.
- **Stay a plugin.** CRISPy is host-agnostic; don't import pymmcore-gui internals.

## Design decisions worth knowing

- **The detector knob has no software readout.** It is a mechanical knob on the CRISP
  unit, so "find the best setting" is a peak-hold over live metrics, not a lookup by
  knob position. Tune smooths SNR/Sum over a short time window before holding the
  peak, so a noise spike can't become the record.
- **Tune does not use `AsyncPoller`.** `pymmcore_widgets.control._async_poller` is not
  in the latest PyPI release of pymmcore-widgets (0.12.1, which `uv.lock` pins). The
  slow poll still imports it, so `CrispControlPanel` only constructs where
  pymmcore-widgets provides it; `run_demo.py` installs a stand-in if it is missing.
- **`RefreshPropertyValues="Yes"` is written once when Tune starts**, on the unverified
  assumption that it is a persistent mode. If bench readings look frozen, move the
  write inside `TuneSampler._run`.
- **An unreadable CRISP state counts as unlocked, and that re-enables the joystick.**
  This is intentional. The joystick is auto-disabled on a lock edge and restored on an
  unlock edge (the restore value comes from the `Z_P` preset in the `Joystick Wheels`
  config group; without that preset the wheel can be disabled but not restored).
  Don't "fix" the failed-read case to keep the wheel disabled.
- **Number of Averages / Update Skips are never written by CRISPy** (they change
  controller state); the Tune tooltip tells the user to lower them if the rate is capped.
