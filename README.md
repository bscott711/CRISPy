# CRISPy

**C**ontinuous **R**eflective **I**nterface **S**ample **P**lacement plugin for **y**our microscope.

A modern, multi-device ASI CRISP autofocus plugin designed specifically for the [pymmcore-gui](https://github.com/pymmcore/pymmcore-gui) ecosystem

## ✨ Features

- **Multi-Device Support:** Control and calibrate N ASI CRISP axes on a Tiger controller at once (e.g. a Z stage and a piezo). Each axis locks independently.
- **Smart UI Orchestration:** One panel for a single CRISP, two side by side for a pair, and tabs for three or more. Each panel stays compact: the **Dither** assistant and **Parameters** fold away, and the Focus curve and Hold test open in a window of their own (**Focus curve / Hold test…**).
- **Live Telemetry:** SNR (green at or above the 4 dB ASI target), Dither Error, Sum (green inside ASI's 50-80 band), AGC, Lock Offset and the CRISP state, read off the GUI thread so a slow serial port never stalls the live view. Each readout's tooltip gives ASI's guidance for it.
- **Actuator-Aware Scaling:** Each axis shows whether it drives a piezo or a Z stage, its calibration gain and range, the resulting error sensitivity (counts per µm), the focus error in microns, and the in-loop error fluctuation while locked.
- **Guided Calibration:** Buttons for the 3-step calibration (Log Cal, Dither, Set Gain), with a **Dither assistant** for the detector alignment. See [Aligning the detector](#-aligning-the-detector-dither).
- **Lock Diagnostics:** A **Focus curve** that measures the error slope, peaks and capture range around the lock point (and can snap an image at each position, to show where the image is actually sharpest), and a **Hold test** that measures how still the locked focus actually stays. See [Diagnosing a noisy lock](#-diagnosing-a-noisy-lock).
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

Once installed, launch `pymmcore-gui`. pymmcore-gui embeds CRISPy itself (its CRISP widget calls `CRISPy.plugin.launch_crisp_plugin()`) and sets the host hooks CRISPy offers: `CRISPy.imaging` (snap and image view for the Focus curve) and `CRISPy.spot.store.set_settings_store` (where the Spot lock keeps its settings). Without a host, `launch_crisp_plugin()` works on its own and `run_demo.py` shows it.

## 🚀 Usage

1. Open `pymmcore-gui` and load your Hardware Configuration.
1. Open the **CRISP** widget (pymmcore-gui builds it with `launch_crisp_plugin()`).
1. The plugin will automatically discover all loaded ASI CRISP devices.
1. In each axis's **Parameters** box, set **Objective NA** to the NA of the objective *that axis moves*. It sets the dither move (1.5 µm / NA²); the Cal Range readout shows the NA it corresponds to. Too high an NA gives too short a dither move and a small, noisy dither Err.
1. Focus on your sample, then follow the 3-step calibration in each axis's **Calibration** box:
   - **Step 1:** Log Cal. Aim for SNR > 4 dB (at least 2); if it is low, raise LED Intensity and repeat.
   - **Step 2:** Dither. Turn the detector lateral adjustment screw for the largest |Err| (see [Aligning the detector](#-aligning-the-detector-dither)).
   - **Step 3:** Set Gain.
1. Use **Set Ready** to turn the LED on and **Lock** to engage the servo. **Set Offset** re-zeroes the focus error at the current position and **Save to Controller** writes the settings to the controller's NVRAM.

Telemetry is polled only while a panel is visible and pauses automatically during a Multi-Dimensional Acquisition so the Tiger serial port stays free. Each tick reads the state, Dither Error and Sum. The rest (SNR is fixed at Log Cal; the calibration values change only on recalibration) is read every 10 s, after a button press and when the state changes. The **Poll** checkbox and interval (100 ms to 10 s, default 750 ms) are in each panel. ASI's own plugin suggests turning polling off once calibration is done.

## 🎯 Aligning the detector (Dither)

From the [ASI CRISP manual](https://docs.asiimaging.com/crisp_manual): in the Dither state the focus moves up and down by the calibration range, and the Err number is the *change* in error across that move. Slowly turn the **detector lateral adjustment screw** on the CRISP unit for the largest **absolute** Err (large negative numbers are as good as large positive ones). Pause after each adjustment, because moving the detector gives large temporary values. Aim for at least ±100 (ASI's plugin; the manual says > 50, and > 200 is "plenty of signal"), with Sum staying in 50-80. If Sum ends up outside that band, redo Log Cal.

Pressing **2. Dither** expands the panel's **Dither** section and starts the assistant (or press **Watch** if the controller is already dithering):

- It reads only Dither Error and Sum, as fast as the link allows, and shows the rate.
- The big number is the median of |Err| over a short window (default 1 s), so brief spikes are ignored.
- The **peak** is the best value seen while the readings were steady (the window's thirds agree within 10%), so a temporary value from moving the detector can't become the peak, however long it takes to decay. "settling…" means it hasn't settled yet.
- The **scatter** is the standard deviation of the readings over the window, in counts and as % of |Err|. Compare axes by the counts; a large % means the readings (and the peak) move that much from noise alone, so raise **Smoothing** to 2-3 s and pause longer.
- The trace shows the last 30 s with the 100 and 200 marks; the raw readings are the grey dots.
- Sum is checked against 50-80; 0 or 100 means a detector half is saturated.

The normal poll pauses while it runs, so only one reader uses the serial port. It stops by itself when the state leaves Dither (while dithering, the controller's sub-states read as "Calibrating", which counts), when the panel is hidden, when you collapse the Dither section, and when an acquisition starts.

## 🔬 Diagnosing a noisy lock

The locked error number is measured *inside* the feedback loop, so it can look quiet while the focus moves. On this rig's 2026-08-14 log, the locked piezo's error stayed within ±9 counts while the piezo moved ~100 nm between samples. Measure the focus motion instead, and change one thing at a time.

1. **Check the calibration.** SNR > 4 dB, Sum 50-80, and a dither |Err| of at least 100. A Sum of 0 or 100 means the detector is saturated; redo Log Cal.
1. **Run a Focus curve** (in Ready, after Set Offset). It steps the focus actuator through ±10 µm around its position (adjustable), reads the error at each step and returns it to the start. It stays inside the actuator's travel limits (`LowerLim(mm)` / `UpperLim(mm)`), leaves out any position the actuator doesn't reach, and plots each point where the actuator actually was, so a scan that runs into a limit shows up as a shorter curve with a **Travel limit** note, not as a flat stretch. It reports:
   - the **lock point**: where CRISP would hold focus with the current lock offset, relative to where you started (Set Offset at best focus moves it to 0);
   - the **slope** in counts of the error readout per µm, and how much focus one count is. The readout is a whole number and coarse (on the rig's Z axis one count was about 117 nm). After a focus curve the panel uses this slope for its µm figures, until the next Log Cal or Dither. The Cal Gain / Cal Range figure is on a different, internal scale (~300× on that axis), so it isn't used for conversions;
   - how far the lock point is from the curve's **peaks** (a lock point close to a peak is hard to hold), or that there is no peak within the scan if the error is still growing at its ends;
   - the **capture range**;
   - with **Snap an image at each position** ticked, where the **image is sharpest** and how far that is from the lock point (to lock there instead, move there in Ready and Set Offset). The images open in their own window, full size, one view per camera, with a slider to step through the positions; the Focus curve tab shows a sharpness plot. It snaps the way the host's Snap does and shows the images with the host's preview widget when the host provides those (pymmcore-gui does: its cameras, lasers and Preview windows' viewer); otherwise it snaps with the core's current camera and uses a viewer that behaves the same way (grey, contrast from each frame's minimum to maximum, wheel zoom, drag pan). The full frames are kept in temporary files until the next scan or until the panel closes;
   - the **error noise**: how much the error scatters from one position to the next (about a local fit), and what that is in focus (÷ slope). When it is about one count, it is mostly rounding and the readout simply can't show smaller focus noise; the Hold test measures the real motion. When the noise is as large as the change from one position to the next (fine steps, noisy signal), the lock point, peaks and capture range are judged on a running mean, so noise can't fake them.
1. **Run a Hold test unlocked** (Ready, sitting still). CRISP isn't driving the actuator, so the position shows the actuator's *own* noise (the piezo and its drive), and the error rms is the sensor and optics noise. If the actuator already wanders by itself, the lock can't hold it stiller than that. The error's **trend** (Err /min) is the optics or sensor drifting with the actuator still; after a focus curve the panel converts it to the drift a lock would follow, to compare with the locked run. The plot shows the error over time next to the position: after a focus curve both in nm (the error ÷ slope, as the move a lock would make to cancel it), otherwise the error in counts. (A locked run plots only the position: its error is in-loop, mostly sensor noise the lock averages out, and the summary says how much.)
1. **Run a Hold test locked.** It records the actuator's own position for 60 s (adjustable) and splits it into **drift** (the trend, nm/min), **wander** (how much the 1 s averages move) and **jitter** (the spread within each second). It also reports the error rms and whether the lock offset changed. Compared with the unlocked run, it shows what the lock adds or removes. The lock settings and the mode are recorded with each run, and the last runs sit side by side in a table.
1. **Change one setting** in the Parameters box and run the Hold test again. ASI's levers for a still sample:
   - **Averages (2^N)**: "N of 5 to 8 with reduced loop gain (LR T=1 to 5) can … hold the focus very still". Too much averaging adds delay and can oscillate.
   - **Update rate (ms)**: "Increase Update_Rate to improve stability when using piezo Z-axis drive systems" (piezo default 5 ms); raise Loop Gain as you do.
   - **Loop Gain**: reduce it if the lock jitters or oscillates.
1. To see whether polling itself disturbs the lock, run the Hold test once at a low rate and once at a high rate.

CRISPy never changes these settings for you; it only measures. The parameter sliders also ignore the mouse wheel unless you click them first, so scrolling the panel can't change a setting by accident. Both tools (under **Focus curve / Hold test…**) pause the normal poll, stop when the panel or their window is hidden or closed or an acquisition starts, and can save their data as CSV.

## 🎮 Joystick lockout

A joystick wheel that creeps while an axis is locked makes the servo fight it, which shows up as slow drift. So when an axis locks, CRISPy disables the wheel bound to that axis's actuator, and it restores the wheel when the axis unlocks. The **Joystick enabled** checkbox overrides this at any time, including while locked.

The wheel binding is the stage's `JoystickInput` property. The value to restore is read from your own `Z_P` preset in the `Joystick Wheels` config group, so your wheel layout is the source of truth. That preset must exist: without it CRISPy can still disable the wheel, but it cannot restore it, and re-enabling silently does nothing.

**By design:** if the CRISP state cannot be read, the axis is treated as unlocked and the wheel is re-enabled. The next successful read of a locked state disables it again.

## 🧪 Demo Mode (No Hardware Required)

CRISPy includes a standalone test harness that mocks the Micro-Manager core. It models two axes based on a real controller dump, a Z stage and a piezo, each as a small simulation that follows the manual:

- an S-shaped focus curve whose slope depends on the detector screw;
- travel limits where moves stop without an error (the demo piezo starts 2.2 µm above its lower limit);
- a fixed SNR;
- a dither Err with temporary jumps when the screw moves;
- lock jitter that scales with noise ÷ slope and drops with averaging.

A **Demo controls** dock stands in for the screw and the Averages / Loop Gain settings; the piezo starts badly aligned. Every property read has a simulated serial delay. None of the numbers are measurements.

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

## Spot lock

The spot-lock piezo gets a dedicated `PiezoSpotWidget` instead of a CRISP panel: no poller, no CRISP detector controls and no diagnostic tools. It has its own LED button and a Joystick checkbox (off while the lock is active, back on at stop). It holds the piezo at the sample plane by watching the edge of the CRISP LED spot on a second camera, and runs as a separate worker process. It is a controller, not a diagnostic tool: it writes CRISP State and LED Intensity through the service. A config reload rebuilds the widget and stops a running lock. How it works, its calibration and its rules are in [docs/spot_focus_lock.md](docs/spot_focus_lock.md). `just demo` shows it with a simulated camera.

## 🏗️ Architecture

CRISPy strictly adheres to Separation of Concerns (SoC) and DRY principles. All device I/O goes through the shared `CMMCorePlus` instance, never a serial port directly.

- **`controller.py`**: The Hardware Interface. `ASICrispController` wraps the shared core to write the settable `CRISP State` values (`loG_cal`, `Dither`, `gain_Cal`, `Ready`, `Lock`, ...), read the calibration gain, range and sensitivity, switch the joystick, and read and write the lock-offset setpoint.
- **`discovery.py`**: The Device Finder. Finds the loaded CRISP autofocus devices, pairs each with its ASITiger stage (same axis letter and hex address) and classifies that actuator as a piezo or a stage.
- **`dither.py`**, **`focus_curve.py`**, **`hold.py`**: The diagnostics, with no Qt dependency: the Dither assistant's tracker, the focus-curve scan and analysis, and the hold test's recorder and analysis.
- **`sampling.py`**: The fast reader thread and read helpers the diagnostics share.
- **`ui.py`** and **`plot.py`**: The Visuals. `CrispControlPanel` is a Qt-native panel for one axis that reuses `pymmcore-widgets` property controls for the adjustable parameters; `plot.py` is a small QPainter plot for the diagnostics.
- **`plugin.py`**: The Entry Point. `launch_crisp_plugin()` is what pymmcore-gui calls to embed CRISPy (the `pyproject.toml` entry point names the same function). Acts as the orchestrator, querying discovery, instantiating controllers, and building the panels (single, splitter or tabs); it picks the one piezo that gets the `PiezoSpotWidget` (`spot_choice.py`).
- **`imaging.py`**: Qt-free camera images for the Focus curve, with the host hooks `set_snap_provider()` and `set_image_view_factory()`.
- **`spot/`**: The spot edge focus lock (moved here from pymmcore-gui): a worker process that reads the AmScope camera and holds the piezo, its panel, and the `store.py` settings hook. See [docs/spot_focus_lock.md](docs/spot_focus_lock.md).
