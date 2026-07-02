"""
Standalone test harness to run the CRISPy UI in demo mode.

The mock below models two CRISP axes taken from a real ``crisp_dump.txt``: a
translation-stage axis (``CRISPAFocus:Z:32`` paired with ``ZStage:Z:32``) and a
piezo axis (``CRISPAFocus:P:34`` paired with ``PiezoStage:P:34``).  This lets the
actuator badge, calibration/sensitivity readouts, µm conversion, and the
fluctuation metric all run without hardware.  The piezo axis is given a noisier
error signal so the fluctuation readout visibly differs between the two.
"""

import random
import sys
from unittest.mock import patch

from psygnal import Signal
from qtpy.QtWidgets import QApplication, QMainWindow


# 1. Define the Mocks FIRST
class MockEvents:
    propertyChanged = Signal(str, str, str)


# Per-axis property snapshots (values mirror a real dump).
_AXIS_PROPS: dict[str, dict[str, str]] = {
    "CRISPAFocus:Z:32": {
        "CRISP State": "Lock",
        "Signal Noise Ratio": "3.7",
        "Dither Error": "-3",
        "Sum": "40",
        "LogAmpAGC": "32",
        "Lock Offset": "-792",
        "Calibration Gain": "-2524",
        "Calibration Range(um)": "0.6770",
        "AxisLetter": "Z",
        "TigerHexAddress": "32",
        "Max Lock Range(mm)": "0.1000",
    },
    "CRISPAFocus:P:34": {
        "CRISP State": "Lock",
        "Signal Noise Ratio": "7.1",
        "Dither Error": "-44",
        "Sum": "72",
        "LogAmpAGC": "59",
        "Lock Offset": "7429",
        "Calibration Gain": "-377",
        "Calibration Range(um)": "0.6750",
        "AxisLetter": "P",
        "TigerHexAddress": "34",
        "Max Lock Range(mm)": "2.0000",
    },
}
# Paired ASITiger stage devices used for actuator classification. Value is the
# device *name* reported by ``getDeviceName`` (a piezo reports as "PiezoStage").
_STAGE_DEVICES: dict[str, str] = {
    "ZStage:Z:32": "ZStage:Z:32",
    "PiezoStage:P:34": "PiezoStage:P:34",
}
# Simulated live error noise (counts, 1-sigma) per axis to demo the fluctuation.
_ERROR_NOISE = {"CRISPAFocus:Z:32": 2.0, "CRISPAFocus:P:34": 15.0}


class MockCMMCorePlus:
    _instance = None
    events = MockEvents()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # --- device enumeration (used by classify_focus_actuator) --------------
    def getLoadedDevicesOfType(self, device_type):
        return list(_STAGE_DEVICES)

    def getDeviceLibrary(self, device):
        return "ASITiger"

    def getDeviceName(self, device):
        return _STAGE_DEVICES.get(device, device)

    def getDeviceDescription(self, dev):
        return "Mock"

    # --- property access ---------------------------------------------------
    def getProperty(self, label, prop):
        props = _AXIS_PROPS[label]  # KeyError -> caller suppresses
        if prop == "Dither Error":
            base = float(props[prop])
            noise = _ERROR_NOISE.get(label, 5.0)
            return f"{base + random.gauss(0, noise):.0f}"
        return props[prop]

    def setProperty(self, label, prop, value):
        print(f"[DEMO] {label} -> {prop}: {value}")
        _AXIS_PROPS.setdefault(label, {})[prop] = str(value)


# 2. Patch pymmcore_plus BEFORE importing our plugin modules
import pymmcore_plus  # noqa: E402

# Import pymmcore-widgets first: its import binds to ``pymmcore_plus.model`` (which
# reads real ``CMMCorePlus`` class attributes at load time), so it must happen
# before we swap in the mock — otherwise the lazy import in ui.py explodes.
import pymmcore_widgets  # noqa: E402, F401

pymmcore_plus.CMMCorePlus = MockCMMCorePlus

# 3. NOW it is safe to import our plugin
from CRISPy.plugin import launch_crisp_plugin  # noqa: E402


def mock_discover(mmcore=None):
    return list(_AXIS_PROPS)


def run_demo():
    app = QApplication.instance() or QApplication(sys.argv)

    # Patch the function in the `plugin` module's namespace
    with patch("CRISPy.plugin.discover_crisp_devices", mock_discover):
        widget = launch_crisp_plugin()

        window = QMainWindow()
        window.setCentralWidget(widget)
        window.setWindowTitle("CRISPy - Demo Mode (Pure Python)")
        window.resize(800, 640)  # wide enough for both axes side-by-side
        window.show()

        sys.exit(app.exec_())


if __name__ == "__main__":
    run_demo()
