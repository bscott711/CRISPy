"""
Standalone test harness to run the CRISPy UI in demo mode.

The mock below models two CRISP axes taken from a real ``crisp_dump.txt``: a
translation-stage axis (``CRISPAFocus:Z:32`` paired with ``ZStage:Z:32``) and a
piezo axis (``CRISPAFocus:P:34`` paired with ``PiezoStage:P:34``).  This lets the
actuator badge, calibration/sensitivity readouts, µm conversion, and the
fluctuation metric all run without hardware.  The piezo axis is given a noisier
error signal so the fluctuation readout visibly differs between the two.

To try the **Tune** box without hardware, the mock also simulates the physical detector
knob: SNR, Sum and the error noise follow a bump that a virtual knob sweeps back and
forth over ``_SWEEP_PERIOD_S`` seconds, peaking at ``_KNOB_OPTIMUM``. Every property
access sleeps ``_SERIAL_LATENCY_S`` to mimic a Tiger serial round trip (an assumed
figure, not a measurement), so the effect of reading fewer properties shows up as a
higher sample rate. Set ``CRISPY_DEMO_LATENCY_MS`` to change it.
"""

import importlib.util
import os
import random
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from psygnal import Signal
from qtpy.QtCore import QObject, QTimer
from qtpy.QtCore import Signal as QtSignal
from qtpy.QtWidgets import QApplication, QMainWindow

_SERIAL_LATENCY_S = float(os.environ.get("CRISPY_DEMO_LATENCY_MS", "10")) / 1000
_SWEEP_PERIOD_S = 12.0  # one full there-and-back turn of the virtual knob
_KNOB_OPTIMUM = 0.6  # knob position (0..1) where the signal peaks


def _knob_position(t: float) -> float:
    """Virtual knob position in 0..1: a triangle wave, there and back."""
    phase = (t / _SWEEP_PERIOD_S) % 1.0
    return 2 * phase if phase < 0.5 else 2 * (1 - phase)


def _signal_quality(t: float) -> float:
    """0..1 detector signal quality: a bump around the knob's optimum."""
    x = (_knob_position(t) - _KNOB_OPTIMUM) / 0.12
    return 2.718281828 ** (-(x * x))


# 1. Define the Mocks FIRST
class MockEvents:
    propertyChanged = Signal(str, str, str)


class MockMDAEvents:
    sequenceStarted = Signal(object)
    sequenceFinished = Signal(object)


class MockMDA:
    events = MockMDAEvents()


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
_T0 = time.monotonic()  # the virtual knob starts turning at import


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

    mda = MockMDA()

    def getLoadedDevices(self):
        return [*_AXIS_PROPS, *_STAGE_DEVICES]

    def hasProperty(self, label, prop):
        # The demo exposes no PropertyWidget-backed parameters and no
        # RefreshPropertyValues, so the panel skips both.
        return False

    # --- property access ---------------------------------------------------
    def getProperty(self, label, prop):
        props = _AXIS_PROPS[label]  # KeyError -> caller suppresses
        time.sleep(_SERIAL_LATENCY_S)  # one simulated serial round trip
        quality = _signal_quality(time.monotonic() - _T0)
        if prop == "Signal Noise Ratio":
            return f"{0.8 + 9.0 * quality + random.gauss(0, 0.15):.2f}"
        if prop == "Sum":
            return f"{10 + 90 * quality + random.gauss(0, 1.0):.0f}"
        if prop == "Dither Error":
            base = float(props[prop])
            # A better-centred line detector (higher quality) means a quieter error.
            noise = _ERROR_NOISE.get(label, 5.0) * (1.5 - 1.2 * quality)
            return f"{base + random.gauss(0, noise):.0f}"
        return props[prop]

    def setProperty(self, label, prop, value):
        time.sleep(_SERIAL_LATENCY_S)
        print(f"[DEMO] {label} -> {prop}: {value}")
        _AXIS_PROPS.setdefault(label, {})[prop] = str(value)


def _install_async_poller_shim() -> None:
    """Stand in for pymmcore-widgets' ``AsyncPoller`` where it isn't available.

    ``ui.py`` polls telemetry through ``pymmcore_widgets.control._async_poller``, which
    is not in the latest PyPI release (0.12.1), only in a development build of
    pymmcore-widgets. This minimal look-alike (a ``QTimer`` that runs the read on a
    worker thread and applies the result on the GUI thread) lets the demo run on
    released dependencies. It is only used when the real module can't be imported.
    """

    class AsyncPoller(QObject):
        _done = QtSignal(object)

        def __init__(self, read, apply, *, interval_ms=1000, parent=None, active=None):
            super().__init__(parent)
            self._read, self._apply, self._active = read, apply, active
            self._busy = False
            self._pool = ThreadPoolExecutor(max_workers=1)
            self._timer = QTimer(self)
            self._timer.setInterval(interval_ms)
            self._timer.timeout.connect(self.poll_now)
            self._done.connect(self._finish)

        def poll_now(self, force=False):
            visible = self._active is None or self._active()
            if self._busy or not (force is True or visible):
                return
            self._busy = True
            self._pool.submit(self._work)

        def _work(self):
            try:
                result = self._read()
            except Exception:  # noqa: BLE001 - a failed read just skips this tick
                result = None
            self._done.emit(result)

        def _finish(self, result):
            self._busy = False
            if result is not None:
                self._apply(result)

        def start(self):
            self._timer.start()

        def stop(self):
            self._timer.stop()

        def set_interval(self, ms):
            self._timer.setInterval(ms)

    module = types.ModuleType("pymmcore_widgets.control._async_poller")
    module.AsyncPoller = AsyncPoller
    sys.modules[module.__name__] = module


# 2. Patch pymmcore_plus BEFORE importing our plugin modules
import pymmcore_plus  # noqa: E402

# Import pymmcore-widgets first: its import binds to ``pymmcore_plus.model`` (which
# reads real ``CMMCorePlus`` class attributes at load time), so it must happen
# before we swap in the mock — otherwise the lazy import in ui.py explodes.
import pymmcore_widgets  # noqa: E402, F401

pymmcore_plus.CMMCorePlus = MockCMMCorePlus

if importlib.util.find_spec("pymmcore_widgets.control._async_poller") is None:
    _install_async_poller_shim()

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
