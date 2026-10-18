# CLAUDE.md — CRISPy

Multi-device ASI CRISP autofocus plugin for the pymmcore-gui ecosystem. Controls
N CRISP autofocus axes on ASI Tiger controllers via pymmcore-plus.

## Orchestration rule
- Before implementing anything non-trivial, write the execution plan to
  `.claude/current_plan.md` and keep it updated as work proceeds.

## Commands (run from this repo root via `uv`)
- `just demo` — launch the standalone UI demo harness (`run_demo.py`)
- `just fix`  — ruff check --fix, ruff format, mdformat
- Installed editable into the pymmcore-gui env; it appears there as a Plugins entry.

## Directory map (`src/CRISPy/`)
- `discovery.py`  — `discover_crisp_devices()`: find ASITiger AutoFocus axes
- `controller.py` — `ASICrispController`: per-device property read/write + lock ops
- `ui.py`         — `CrispControlPanel`: Qt-native control/calibration panel
- `tuning.py`     — Qt-free fast sampling + peak-hold for turning the CRISP
                    detector knob (`TuneSampler`, `TuneTracker`)
- `plugin.py`     — `launch_crisp_plugin()`: layout orchestration
                    (1=panel, 2=horizontal splitter, 3+=tabs)

## Architectural boundaries (do not cross)
- **Go through the Core.** All device I/O uses the shared `CMMCorePlus` instance.
  Never open the serial/COM port directly.
- **Per-device, autonomous locking.** Each panel writes its own device's
  `"CRISP State"`; the Tiger card servos autonomously. Never use the Core
  continuous-autofocus API for dual-axis locking.
- **Event-driven UI.** Update from `propertyChanged` signals. Read-only sensors
  (SNR, Dither Error) need explicit polling — gate it on
  `hasProperty("RefreshPropertyValues")`, run only while the panel is visible, and
  auto-pause during MDA (`mda.events.sequenceStarted/Finished`).
  Fast Tune mode is the one exception: its own sampler thread reads only SNR / Sum /
  Dither Error at high rate, pauses the slow poll while running (one reader on the
  serial port) and stops on hide, close and MDA start.
- **Qt via `qtpy`.** Use the `qtpy` abstraction (not a hard PyQt6 import) and reuse
  `pymmcore-widgets` `PropertyWidget` for parameter fields.
- **Stay a plugin.** CRISPy is host-agnostic; don't import pymmcore-gui internals.
