# CRISPy

**C**ontinuous **R**eflective **I**nterface **S**ample **P**lacement plugin for **y**our microscope.

A modern, multi-device ASI CRISP autofocus plugin designed specifically for the [pymmcore-gui](https://github.com/pymmcore/pymmcore-gui) ecosystem

## ✨ Features

- **Multi-Device Support:** Control and calibrate N ASI CRISP axes on a Tiger controller at once (e.g. a Z stage and a piezo). Each axis locks independently.
- **Smart UI Orchestration:** One panel for a single CRISP, two side by side for a pair, and tabs for three or more.
- **Live Telemetry:** SNR (green at or above the 4 dB ASI target), Dither Error, Sum, AGC, Lock Offset and the CRISP state, read off the GUI thread so a slow serial port never stalls the live view.
- **Actuator-Aware Scaling:** Each axis shows whether it drives a piezo or a Z stage, its calibration gain and range, the resulting error sensitivity (counts per µm), the focus error in microns, and a rolling fluctuation (std-dev and peak-to-peak).
- **Fast Tune Mode:** High-rate sampling with a peak-hold meter for aligning the CRISP detector knob. See [Tuning the detector knob](#-tuning-the-detector-knob).
- **Guided Calibration:** Buttons for the 3-step calibration (Log Cal, Dither, Set Gain) next to the live SNR and error readouts.
- **Joystick Lockout:** The joystick wheel is disabled while an axis is locked and restored when it unlocks. See [Joystick lockout](#-joystick-lockout).
- **Lock-Offset Setpoint:** `ASICrispController` can read and write the servo's lock offset in counts or microns, for hosts that want to hold a deliberately shifted focus plane (e.g. a per-wavelength chromatic correction).
- **Native Integration:** Built on `qtpy` and reuses `pymmcore-widgets` property controls to match `pymmcore-gui`.

## 📋 Prerequisites

- **Python 3.10+**
- **pymmcore-gui:** The host application, which also provides the Qt binding.
- **Micro-Manager Hardware Config:** Your configuration must load the ASI Tiger CRISP autofocus devices (`ASITiger` adapter, labels like `CRISPAFocus:Z:32`). A device is picked up if its label or adapter name contains `CRISP`, or if it is an autofocus device from the `ASITiger` library.
- **A `pymmcore-widgets` that provides `AsyncPoller`:** the telemetry poller imports `pymmcore_widgets.control._async_poller`. At the time of writing that module is not in the latest PyPI release (0.12.1), so the panel cannot be built with that release alone.

## 📦 Installation

CRISPy is designed to be installed directly into the same Python environment where `pymmcore-gui` is running.

**From a local clone (Development/Editable mode):**

```bash
git clone https://github.com/bscott711/CRISPy.git
cd CRISPy
uv pip install -e .
```

**From GitHub (Direct install):**

```bash
uv pip install git+https://github.com/bscott711/CRISPy.git
```

Once installed, launch `pymmcore-gui`. CRISPy will automatically register itself and appear in the **Plugins** menu.

## 🚀 Usage

1. Open `pymmcore-gui` and load your Hardware Configuration.
1. Navigate to **Plugins -> CRISPy**.
1. The plugin will automatically discover all loaded ASI CRISP devices.
1. Follow the 3-step calibration in each axis's **Calibration** box:
   - **Step 1:** Run Log Cal (wait for a green SNR).
   - **Step 2:** Run Dither (wait for a low Dither Error).
   - **Step 3:** Set Gain.
1. Use **Set Ready** to turn the LED on and **Lock** to engage the servo. **Set Offset** re-zeroes the focus error at the current position and **Save to Controller** writes the settings to the controller's NVRAM.

Telemetry is polled only while a panel is visible and pauses automatically during a Multi-Dimensional Acquisition so the Tiger serial port stays free. The **Poll** checkbox and interval (100 ms to 10 s, default 750 ms) are in each panel.

## 🎛️ Tuning the detector knob

The CRISP unit has a physical knob that moves the line detector. It has no software readout, so the best setting can only be found by sweeping it and watching a peak-held metric. The normal readouts are too slow for that: they poll every 750 ms and the fluctuation figure averages the last 20 polls.

Press **Start Tune** in a panel (the LED must be on):

- It samples only SNR, Sum and Dither Error, as fast as the controller allows (capped at 100 Hz), and shows the achieved rate.
- Each metric is averaged over a short window (default 0.5 s, adjustable) and the best value seen is held next to the live one.
- The **meter** shows the live value as a percentage of the best seen. Choose whether it follows SNR, Sum or Stability (lowest error fluctuation).
- Sweep the knob, note the peak, then back up until the live value matches it. **Reset peak** starts a fresh sweep.

While tuning, normal polling is paused so only one reader uses the serial port, and the Readouts box is marked as paused because its values freeze. Tune stops when the panel is hidden or closed or an acquisition starts, and shows "no data from the controller" if samples stop arriving.

The controller's own **Number of Averages** and **Update Skips** limit how often these values can change. CRISPy never changes them for you, because they write controller state; lower them yourself if the rate is still too slow.

## 🕹️ Joystick lockout

A joystick wheel that creeps while an axis is locked makes the servo fight it, which shows up as slow drift. So when an axis locks, CRISPy disables the wheel bound to that axis's actuator, and it restores the wheel when the axis unlocks. The **Joystick enabled** checkbox overrides this at any time, including while locked.

The wheel binding is the stage's `JoystickInput` property. The value to restore is read from your own `Z_P` preset in the `Joystick Wheels` config group, so your wheel layout is the source of truth. That preset must exist: without it CRISPy can still disable the wheel, but it cannot restore it, and re-enabling silently does nothing.

**By design:** if the CRISP state cannot be read, the axis is treated as unlocked and the wheel is re-enabled. The next successful read of a locked state disables it again.

## 🧪 Demo Mode (No Hardware Required)

CRISPy includes a standalone test harness that mocks the Micro-Manager core. It models two axes taken from a real controller dump, a Z stage and a piezo, so the actuator badge, calibration and sensitivity readouts, micron conversion and fluctuation metric all run without hardware. It also simulates a virtual detector knob sweeping back and forth, with a bump in SNR and Sum and quieter error near the optimum, plus a simulated serial delay per property read, so you can try **Tune** on your desk.

The demo needs a Qt binding in the environment. The `pymmcore-gui` environment has one; otherwise add one for the run:

```bash
uv run --with PyQt6 python run_demo.py
```

If you have the `just` command runner installed and a Qt binding available, `just demo` does the same as `uv run python run_demo.py`.

If your `pymmcore-widgets` lacks `AsyncPoller` (see Prerequisites), the demo falls back to a minimal stand-in so it still runs.

## 🛠️ Development & Tooling

This project uses `uv` for dependency management and a `justfile` for standardizing development tasks.

**Setup the environment:**

```bash
uv sync
```

**Run the tests** (no Qt and no hardware needed):

```bash
uv run pytest -q
```

**Available `just` recipes:**

| Command     | Description                                                                         |
| :---------- | :---------------------------------------------------------------------------------- |
| `just demo` | Launches the standalone UI test harness.                                            |
| `just fix`  | Runs `ruff` to fix linting errors and format code, then `mdformat` on the markdown. |

`ruff` is not a project dependency; install it separately (for example `uv tool install ruff`).

## 🏗️ Architecture

CRISPy strictly adheres to Separation of Concerns (SoC) and DRY principles. All device I/O goes through the shared `CMMCorePlus` instance, never a serial port directly.

- **`controller.py`**: The Hardware Interface. `ASICrispController` wraps the shared core to write the settable `CRISP State` values (`loG_cal`, `Dither`, `gain_Cal`, `Ready`, `Lock`, ...), read the calibration gain, range and sensitivity, switch the joystick, and read and write the lock-offset setpoint.
- **`discovery.py`**: The Device Finder. Finds the loaded CRISP autofocus devices, pairs each with its ASITiger stage (same axis letter and hex address) and classifies that actuator as a piezo or a stage.
- **`tuning.py`**: Fast Tune mode, with no Qt dependency. It reads SNR, Sum and Dither Error, keeps the rolling window and peak-hold, and runs the sampler thread.
- **`ui.py`**: The Visuals. `CrispControlPanel` is a Qt-native panel for one axis that reuses `pymmcore-widgets` property controls for the adjustable parameters.
- **`plugin.py`**: The Entry Point. Registered via `pyproject.toml` entry points. Acts as the orchestrator, querying discovery, instantiating controllers, and building the panels (single, splitter or tabs).
