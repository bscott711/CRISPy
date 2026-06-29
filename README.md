# CRISPy

**C**ontinuous **R**eflective **I**nterface **S**ample **P**lacement plugin for **y**our microscope.

A modern, multi-device ASI CRISP autofocus plugin designed specifically for the [pymmcore-gui](https://github.com/pymmcore/pymmcore-gui) ecosystem

## ✨ Features

- **Multi-Device Support:** Seamlessly control and calibrate N ASI CRISP systems simultaneously. (e.g., Two Z-drives on the same Tiger Controller).
- **Zero Serial Polling:** Uses `pymmcore-plus` event-driven signals (`propertyChanged`) to update the UI. This prevents flooding the serial port (COM4) and avoids lagging your XY stages or camera triggers.
- **Smart UI Orchestration:** Automatically generates a single window for one CRISP, or a tabbed interface for multiple CRISPs.
- **Guided Calibration:** "Traffic light" UI indicators provide immediate, obvious visual feedback for SNR and Dither Error during the 3-step calibration process.
- **Native Integration:** Built with `magicgui` to natively match the styling and workflow of `pymmcore-gui`.

## 📋 Prerequisites

- **Python 3.10+**
- **pymmcore-gui:** The host application.
- **Micro-Manager Hardware Config:** Your configuration file must have the ASI CRISP devices loaded, and the device description *must* start with `"ASI CRISP"`.

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
1. Follow the 3-step guided calibration on the UI cards:
   - **Step 1:** Run Log Cal (Wait for 🟢 Good SNR).
   - **Step 2:** Run Dither (Wait for 🟢 Low Error).
   - **Step 3:** Set Gain.

## 🧪 Demo Mode (No Hardware Required)

CRISPy includes a standalone test harness that completely mocks the C++ Micro-Manager core. This allows you to test the UI, tabs, and signal logic without needing physical ASI hardware or a serial connection.

If you have the `just` command runner installed:

```bash
just demo
```

*Or manually via `uv`:*

```bash
uv run python run_demo.py
```

This will launch a standalone Qt window with two mock CRISP tabs, generating randomized SNR and Error telemetry when you click the buttons.

## 🛠️ Development & Tooling

This project uses `uv` for dependency management and a `justfile` for standardizing development tasks.

**Setup the environment:**

```bash
uv sync
```

**Available `just` recipes:**

| Command     | Description                                                      |
| :---------- | :--------------------------------------------------------------- |
| `just demo` | Launches the standalone UI test harness.                         |
| `just fix`  | Runs `ruff` to automatically fix linting errors and format code. |

## 🏗️ Architecture

CRISPy strictly adheres to Separation of Concerns (SoC) and DRY principles:

- **`controller.py`**: The Hardware Interface. Wraps `CMMCorePlus.instance()` to send specific ASI string commands (`loG_cal`, `Dither`, `gain_Cal`) to the Tiger Controller.
- **`discovery.py`**: The Device Finder. Queries the core for `AutoFocusDevice` types and filters by the `"ASI CRISP"` description prefix.
- **`ui.py`**: The Visuals. Uses `magicgui` Containers and Qt Stylesheets to render the "Card" based calibration workflow.
- **`plugin.py`**: The Entry Point. Registered via `pyproject.toml` entry points. Acts as the orchestrator, querying discovery, instantiating controllers, and building the UI tabs.
