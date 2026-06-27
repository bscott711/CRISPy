"""
Standalone test harness to run the CRISPy UI in demo mode.
"""

import sys
import random
from unittest.mock import patch

from psygnal import Signal
from qtpy.QtWidgets import QApplication, QMainWindow


# 1. Define the Mocks FIRST
class MockEvents:
    propertyChanged = Signal(str, str, str)


class MockCMMCorePlus:
    _instance = None
    events = MockEvents()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def getLoadedDevicesOfType(self, device_type):
        return []

    def getDeviceDescription(self, dev):
        return "Mock"

    def setProperty(self, label, prop, value):
        print(f"[DEMO] {label} -> {prop}: {value}")
        if prop == "CRISP State":
            if value == "loG_cal":
                fake_snr = random.uniform(1.0, 5.0)
                self.events.propertyChanged.emit(
                    label, "Signal Noise Ratio", str(fake_snr)
                )
            elif value == "Dither":
                fake_error = random.randint(50, 150)
                self.events.propertyChanged.emit(label, "Dither Error", str(fake_error))


# 2. Patch pymmcore_plus BEFORE importing our plugin modules
import pymmcore_plus  # noqa: E402

pymmcore_plus.CMMCorePlus = MockCMMCorePlus

# 3. NOW it is safe to import our plugin
from CRISPy.plugin import launch_crisp_plugin  # noqa: E402


def mock_discover(mmcore=None):
    return ["MockCRISP_Z1", "MockCRISP_Z2"]


def run_demo():
    app = QApplication.instance() or QApplication(sys.argv)

    # Patch the function in the `plugin` module's namespace
    with patch("CRISPy.plugin.discover_crisp_devices", mock_discover):
        widget = launch_crisp_plugin()

        window = QMainWindow()
        window.setCentralWidget(widget)
        window.setWindowTitle("CRISPy - Demo Mode (Pure Python)")
        window.resize(400, 600)  # Made taller to fit the new cards
        window.show()

        sys.exit(app.exec_())


if __name__ == "__main__":
    run_demo()
