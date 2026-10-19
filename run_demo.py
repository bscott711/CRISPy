"""
Standalone test harness to run the CRISPy UI in demo mode.

The mock below models two CRISP axes based on a real ``crisp_dump.txt``: a
translation-stage axis (``CRISPAFocus:Z:32`` paired with ``ZStage:Z:32``) and a piezo
axis (``CRISPAFocus:P:34`` paired with ``PiezoStage:P:34``).

Each axis is a small simulation that follows the ASI CRISP manual's description, so the
diagnostic tools behave the way they should on hardware:

* the error is an S-shaped function of focus position (the "focus curve"); its slope
  is best with the **detector lateral adjustment screw** at its optimum, and the screw
  also unbalances the detector (a large lock offset) and changes Sum;
* SNR is measured once, at Log Cal, and stays fixed afterwards;
* in Dither the error reads the change across the dither move (slope x cal_range),
  with large temporary values for a moment after the screw moves;
* while locked, the actuator jitters by roughly error noise / slope, less with more
  averaging and lower loop gain, and follows a slow thermal drift of the sample;
* unlocked, each actuator only wanders by itself (the piezo by ~20 nm, the stage by
  ~2 nm), so an unlocked hold test shows the actuator's own noise.

A **Demo controls** dock lets you turn each axis's screw and change Averages and Loop
Gain. The piezo starts badly aligned (like this rig's 2026-08-14 log), the stage well
aligned. Every property access sleeps ``_SERIAL_LATENCY_S`` to mimic a Tiger serial
round trip (an assumed figure, not a measurement); set ``CRISPY_DEMO_LATENCY_MS`` to
change it. None of the numbers are measurements; they only make the behaviour visible.
"""

import importlib.util
import math
import os
import random
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from psygnal import Signal
from qtpy.QtCore import QObject, Qt, QTimer
from qtpy.QtCore import Signal as QtSignal
from qtpy.QtWidgets import (
    QApplication,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QMainWindow,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_SERIAL_LATENCY_S = float(os.environ.get("CRISPY_DEMO_LATENCY_MS", "10")) / 1000
_LOCKED = {"Lock", "In Focus"}


class SimAxis:
    """One simulated CRISP axis and its focus actuator."""

    def __init__(
        self,
        label: str,
        stage: str,
        *,
        axis: str,
        hex_addr: str,
        z: float,
        screw: float,
        slope_max: float,
        sign: int,
        noise: float,
        actuator_noise_um: float,
        cal_range: float,
        snr: float,
        agc: int,
        max_lock_range: str,
    ) -> None:
        self.lock = threading.RLock()
        self.label, self.stage = label, stage
        self.z = z  # actuator position, µm
        self.z_focus = z  # actuator position at which the surface is in focus
        self.screw = screw  # detector lateral adjustment screw, 0..1
        self.screw_opt = 0.62
        self.slope_max = slope_max  # counts/µm at the best screw position
        self.sign = sign
        self.width = 2.0  # µm from the lock point to the curve's peaks
        self.noise = noise  # error noise per read, counts rms
        self.actuator_noise_um = actuator_noise_um  # the actuator's own wander
        self.own = 0.0  # current own-wander offset, µm
        self.cal_range = cal_range
        self.snr = snr
        self.agc = agc
        self.state = "In Focus"
        self.state_since = time.monotonic()
        self.next_state: tuple[float, str] | None = None
        self.lock_offset = self.diff(0.0)
        self.cal_gain = round(self.sign * self.slope() * cal_range)
        self.jitter = 0.0
        self.t_last = time.monotonic()  # last position read (jitter memory)
        self.t_drift = self.t_last  # last drift update
        self.transient = 0.0
        self.transient_t = 0.0
        self.drift_um_per_s = 0.012 / 60  # the sample drifts 12 nm/min
        self.dither_flip = False
        self.props = {
            "AxisLetter": axis,
            "TigerHexAddress": hex_addr,
            "Max Lock Range(mm)": max_lock_range,
            "Number of Averages": "1",
            "GainMultiplier": "10",
            "Number of Skips": "5",
            "LED Intensity": "50",
            "Objective NA": "1.49",
        }

    # ---------------------------------------------------------------- optics
    def slope(self) -> float:
        x = (self.screw - self.screw_opt) / 0.18
        return self.slope_max * (math.exp(-x * x) + 0.05)

    def diff(self, u: float) -> float:
        """Detector difference signal at defocus *u* µm (before the lock offset)."""
        s = self.sign * self.slope()
        balance = 9000.0 * (self.screw - self.screw_opt)
        return s * u * math.exp(-u * u / (2 * self.width**2)) + balance

    def signal_sum(self) -> float:
        if self.state == "Idle":
            return 0.0
        if self.screw < 0.05 or self.screw > 0.97:
            return 0.0  # a detector half out of range
        return max(1.0, 74.0 - 300.0 * (self.screw - self.screw_opt) ** 2)

    def set_screw(self, value: float) -> None:
        with self.lock:
            if abs(value - self.screw) > 1e-6:
                self.transient = random.choice((-1, 1)) * random.uniform(300, 800)
                self.transient_t = time.monotonic()
            self.screw = value

    # ---------------------------------------------------------------- states
    def _advance(self, now: float) -> None:
        if self.next_state is not None and now >= self.next_state[0]:
            self.state = self.next_state[1]
            self.state_since = now
            self.next_state = None
            if self.state == "loG_cal":
                self.snr = round(2.0 + 0.1 * float(self.props["LED Intensity"]), 1)
            if self.state == "Dither":
                self.cal_gain = round(self.sign * self.slope() * self.cal_range)
        # The sample drifts slowly; a lock follows it.
        dz = self.drift_um_per_s * (now - self.t_drift)
        self.t_drift = now
        self.z_focus += dz
        if self.state in _LOCKED:
            self.z += dz

    def set_state(self, value: str) -> None:
        now = time.monotonic()
        with self.lock:
            self.next_state = None
            if value in ("Ready", "Idle"):
                self.state = value
            elif value == "Lock":
                # Settle onto the point where the error equals the lock offset.
                for _ in range(20):
                    u = self.z - self.z_focus
                    e = self.diff(u) - self.lock_offset
                    self.z -= e / (self.sign * self.slope())
                self.state = "Lock"
                self.next_state = (now + 0.5, "In Focus")
            elif value == "loG_cal":
                self.state = "Calibrating"
                self.next_state = (now + 1.5, "loG_cal")
            elif value == "Dither":
                self.state = "Dither"
            elif value == "gain_Cal":
                self.state = "Calibrating"
                self.next_state = (now + 1.0, "Dither")
            elif value == "Reset Focus Offset":
                self.lock_offset = self.diff(self.z - self.z_focus)
                self.state = "Ready"
            self.state_since = now

    def read_state(self) -> str:
        with self.lock:
            self._advance(time.monotonic())
            if self.state == "Dither":
                # The controller cycles dither sub-states; the adapter reports most
                # of them as "Calibrating".
                self.dither_flip = not self.dither_flip
                return "Dither" if self.dither_flip else "Calibrating"
            return self.state

    # ---------------------------------------------------------------- values
    def _lock_sigma_um(self) -> float:
        n = int(self.props["Number of Averages"])
        gain = float(self.props["GainMultiplier"])
        return self.noise / self.slope() * math.sqrt(gain / 4.0) / math.sqrt(2**n)

    def position(self) -> float:
        with self.lock:
            now = time.monotonic()
            self._advance(now)
            dt, self.t_last = now - self.t_last, now
            if self.state not in _LOCKED:
                # Not driven by CRISP: only the actuator's own slow wander (1 s memory).
                a = math.exp(-dt / 1.0)
                self.own = a * self.own + math.sqrt(1 - a * a) * random.gauss(
                    0, self.actuator_noise_um
                )
                return self.z + self.own
            # The servo chasing sensor noise: correlated jitter with a 0.3 s memory.
            a = math.exp(-dt / 0.3)
            self.jitter = a * self.jitter + math.sqrt(1 - a * a) * random.gauss(
                0, self._lock_sigma_um()
            )
            return self.z + self.jitter

    def error(self) -> float:
        with self.lock:
            now = time.monotonic()
            self._advance(now)
            if self.state == "Idle":
                return 0.0
            if self.state == "Dither" or (
                self.state == "Calibrating" and self.next_state is None
            ):
                age = now - self.transient_t
                bump = self.transient * math.exp(-age / 0.4) if age < 3 else 0.0
                swing = self.sign * self.slope() * self.cal_range
                return swing + bump + random.gauss(0, self.noise)
            if self.state in _LOCKED:
                n = int(self.props["Number of Averages"])
                return random.gauss(0, self.noise / math.sqrt(2**n))
            return (
                self.diff(self.z + self.own - self.z_focus)  # the actuator's own wander
                - self.lock_offset
                + random.gauss(0, self.noise)
            )

    def get(self, prop: str) -> str:
        if prop == "CRISP State":
            return self.read_state()
        if prop == "Dither Error":
            return f"{self.error():.0f}"
        with self.lock:
            if prop == "Sum":
                s = self.signal_sum()
                return f"{s if s == 0 else s + random.gauss(0, 0.6):.0f}"
            if prop == "Signal Noise Ratio":
                return f"{self.snr:.4f}"
            if prop == "LogAmpAGC":
                return str(self.agc)
            if prop == "Lock Offset":
                return f"{self.lock_offset:.0f}"
            if prop == "Calibration Gain":
                return str(self.cal_gain)
            if prop == "Calibration Range(um)":
                return f"{self.cal_range:.4f}"
            return self.props[prop]  # KeyError -> caller suppresses


_AXES: dict[str, SimAxis] = {
    "CRISPAFocus:Z:32": SimAxis(
        "CRISPAFocus:Z:32",
        "ZStage:Z:32",
        axis="Z",
        hex_addr="32",
        z=7.84,
        screw=0.60,
        slope_max=3800.0,
        sign=-1,
        noise=3.0,
        actuator_noise_um=0.002,
        cal_range=0.677,
        snr=4.6,
        agc=41,
        max_lock_range="0.1000",
    ),
    "CRISPAFocus:P:34": SimAxis(
        "CRISPAFocus:P:34",
        "PiezoStage:P:34",
        axis="P",
        hex_addr="34",
        z=-19.79,
        screw=0.30,
        slope_max=450.0,
        sign=-1,
        noise=6.0,
        actuator_noise_um=0.02,
        cal_range=0.675,
        snr=7.1,
        agc=60,
        max_lock_range="2.0000",
    ),
}
_BY_STAGE = {ax.stage: ax for ax in _AXES.values()}


# 1. Define the Mocks FIRST
class MockEvents:
    propertyChanged = Signal(str, str, str)


class MockMDAEvents:
    sequenceStarted = Signal(object)
    sequenceFinished = Signal(object)


class MockMDA:
    events = MockMDAEvents()


class MockCMMCorePlus:
    _instance = None
    events = MockEvents()
    mda = MockMDA()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # --- device enumeration (used by classify_focus_actuator) --------------
    def getLoadedDevicesOfType(self, device_type):
        return list(_BY_STAGE)

    def getDeviceLibrary(self, device):
        return "ASITiger"

    def getDeviceName(self, device):
        return device

    def getDeviceDescription(self, dev):
        return "Mock"

    def getLoadedDevices(self):
        return [*_AXES, *_BY_STAGE]

    def hasProperty(self, label, prop):
        # No PropertyWidget-backed parameters in the demo (the Demo controls dock
        # stands in for them), but the refresh flag exists like on real hardware.
        return prop == "RefreshPropertyValues"

    # --- property access ---------------------------------------------------
    def getProperty(self, label, prop):
        time.sleep(_SERIAL_LATENCY_S)  # one simulated serial round trip
        return _AXES[label].get(prop)  # KeyError -> caller suppresses

    def setProperty(self, label, prop, value):
        time.sleep(_SERIAL_LATENCY_S)
        print(f"[DEMO] {label} -> {prop}: {value}")
        if label in _AXES:
            if prop == "CRISP State":
                _AXES[label].set_state(str(value))
            elif prop in _AXES[label].props:
                _AXES[label].props[prop] = str(value)

    # --- stage positions -----------------------------------------------------
    def getPosition(self, label):
        time.sleep(_SERIAL_LATENCY_S)
        return _BY_STAGE[label].position()

    def setPosition(self, label, z):
        time.sleep(_SERIAL_LATENCY_S)
        ax = _BY_STAGE[label]
        with ax.lock:
            if ax.state in _LOCKED:
                raise RuntimeError("COD 47: the focus axis is locked")
            ax.z = float(z)

    def waitForDevice(self, label):
        time.sleep(0.005)


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
    return list(_AXES)


def build_demo_controls() -> QWidget:
    """Sliders/spin boxes standing in for the physical screw and the parameters."""
    panel = QWidget()
    layout = QVBoxLayout(panel)
    for ax in _AXES.values():
        box = QGroupBox(ax.label)
        form = QFormLayout(box)
        screw = QSlider(Qt.Orientation.Horizontal)
        screw.setRange(0, 100)
        screw.setValue(round(ax.screw * 100))
        screw.valueChanged.connect(lambda v, a=ax: a.set_screw(v / 100))
        form.addRow("Detector screw", screw)
        for prop, label, lo, hi in (
            ("Number of Averages", "Averages (2^N)", 0, 8),
            ("GainMultiplier", "Loop Gain", 1, 50),
        ):
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setValue(int(ax.props[prop]))
            spin.valueChanged.connect(
                lambda v, a=ax, p=prop: a.props.__setitem__(p, str(v))
            )
            form.addRow(label, spin)
        layout.addWidget(box)
    layout.addStretch()
    return panel


def run_demo():
    app = QApplication.instance() or QApplication(sys.argv)

    # Patch the function in the `plugin` module's namespace
    with patch("CRISPy.plugin.discover_crisp_devices", mock_discover):
        widget = launch_crisp_plugin()

        window = QMainWindow()
        window.setCentralWidget(widget)
        dock = QDockWidget("Demo controls")
        dock.setWidget(build_demo_controls())
        window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        window.setWindowTitle("CRISPy - Demo Mode (Pure Python)")
        window.resize(1150, 900)  # wide enough for both axes side-by-side
        window.show()

        sys.exit(app.exec_())


if __name__ == "__main__":
    run_demo()
