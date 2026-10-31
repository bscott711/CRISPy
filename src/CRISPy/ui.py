"""Qt UI for a single ASI CRISP autofocus axis.

Maps the ASI CRISP feature set (see https://docs.asiimaging.com/crisp_manual) onto
the ASITiger device-adapter properties:

* operation       -> the settable ``CRISP State`` values (Idle / Ready / Lock /
                     Reset Focus Offset / Save to Controller)
* calibration     -> ``loG_cal`` / ``Dither`` / ``gain_Cal``
* live readouts   -> read-only ``Signal Noise Ratio`` / ``Dither Error`` / ``Sum`` /
                     ``LogAmpAGC`` / ``Lock Offset`` / ``Calibration Gain`` /
                     ``Calibration Range(um)`` (polled; the adapter does not emit
                     ``propertyChanged`` for these sensor values)
* derived metrics -> actuator-type badge, error *sensitivity* (counts/µm) from the
                     calibration, the focus error expressed in microns, and the
                     in-loop error fluctuation while locked
* diagnostics     -> the Dither assistant (:mod:`CRISPy.dither`, a collapsible
                     section of the panel), the focus curve
                     (:mod:`CRISPy.focus_curve`) and the hold test (:mod:`CRISPy.hold`)
                     (the last two in a window of their own)
* parameters      -> reused ``pymmcore_widgets.PropertyWidget`` controls
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import statistics
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import QEvent, QObject, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QImage, QPixmap, QWheelEvent
from qtpy.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .controller import (
    CAL_GAIN_PROP,
    CAL_RANGE_PROP,
    LED_PROP,
    counts_per_um,
    should_reapply_led,
)
from .dither import (
    DEFAULT_WINDOW_S,
    DITHER_STATES,
    ERR_MANUAL_MIN,
    ERR_PLENTY,
    ERR_TARGET,
    SUM_HIGH,
    SUM_LOW,
    DitherSample,
    DitherTracker,
    read_dither_sample,
    sum_status,
)
from .focus_curve import (
    DEFAULT_HALF_RANGE_UM,
    DEFAULT_PIEZO_HALF_RANGE_UM,
    DEFAULT_READS,
    DEFAULT_SETTLE_S,
    DEFAULT_STEP_UM,
    CurveAnalysis,
    CurvePoint,
    CurveScan,
    analyze_focus_curve,
    run_focus_curve,
    scan_offsets,
    sharpest_position,
    write_curve_csv,
)
from .gating import (
    CURVE,
    DITHER,
    HOLD,
    control_gate,
    poll_may_run,
    tool_may_start,
)
from .hold import (
    DEFAULT_DURATION_S,
    DEFAULT_RATE_HZ,
    HoldResult,
    HoldSample,
    HoldTest,
    write_hold_csv,
)
from .imaging import FrameStore, image_view_factory, snap_images, snap_provider
from .plot import LinePlot, RefLine, Series
from .sampling import Sampler, device_present, read_str

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import numpy as np
    from pymmcore_widgets.control._async_poller import AsyncPoller
    from qtpy.QtGui import QCloseEvent, QHideEvent

    from .controller import CrispController

logger = logging.getLogger(__name__)

_LED_REAPPLY_DELAY_MS = 200  # debounce after a LED Intensity edit
_PARAM_MAX_WIDTH = 190  # keep slider+value controls compact / out of the way
# How long a focus-curve position waits for its snap on the GUI thread.
_SNAP_TIMEOUT_S = 30.0
# The summary suggests moving the lock point to the sharpest image beyond this, µm.
_SHARPEST_OFFSET_UM = 0.3

_POLL_INTERVAL_MS = 750
# The slow poll reads the fast-changing values every tick and the rest (SNR is fixed
# at Log Cal; calibration values change only on recalibration) this often, plus right
# after a button press and whenever the state changes.
_FULL_READ_EVERY_S = 10.0
_SNR_GOOD_DB = 4.0  # ASI manual target

# The live focus-error readout we express in physical units and track for
# fluctuation. It is raw controller counts; ``sensitivity`` (counts/µm) converts it.
_ERROR_PROP = "Dither Error"
_SUM_PROP = "Sum"
_LOCK_OFFSET_PROP = "Lock Offset"
# Rolling window for the in-loop fluctuation readout (~15 s at the default poll).
_STABILITY_WINDOW = 20

# Read-only telemetry props, in display order: (property, label, tooltip).
_READOUTS: list[tuple[str, str, str]] = [
    (
        "Signal Noise Ratio",
        "SNR",
        (
            "Measured during Log Cal (signal with the LED on vs off) and stored;\n"
            "it does not change afterwards. ASI: aim for > 4 dB (at least 2 dB).\n"
            "If it is low, raise LED Intensity and redo Log Cal."
        ),
    ),
    (
        "Dither Error",
        "Dither Err",
        (
            "In the Dither state: the change in focus error across the dither move.\n"
            "Turn the detector lateral adjustment screw for the largest absolute\n"
            "value (ASI: at least ±100; > 200 is plenty). See the Dither section.\n"
            "In other states: the focus error, measured inside the feedback loop."
        ),
    ),
    (
        "Sum",
        "Sum",
        (
            "Light on the detector. ASI target 50-80. 0 or 100 means a detector\n"
            "half is saturated: redo Log Cal."
        ),
    ),
    (
        "LogAmpAGC",
        "AGC",
        (
            "Log-amp gain set by Log Cal; higher with more LED or a more reflective\n"
            "sample. ASI: a minimum of ~20 is acceptable for average samples."
        ),
    ),
    (
        "Lock Offset",
        "Lock Offset",
        (
            "The error value the lock holds (set at calibration, by Set Offset,\n"
            "and by the focus wheel while locked). ASI: CRISP performs best when\n"
            "the detector halves are roughly balanced, i.e. the offset is not too\n"
            "large. A large value suggests redoing the Dither alignment."
        ),
    ),
    (
        "Calibration Gain",
        "Cal Gain",
        (
            "Set by calibration (LR X). ASI: higher numbers mean less overall loop\n"
            "gain."
        ),
    ),
    (
        "Calibration Range(um)",
        "Cal Range (µm)",
        (
            "The calibration / dither move distance, set from Objective NA:\n"
            "1.5 µm / NA² (the NA shown is the one this distance corresponds to).\n"
            "ASI: set Objective NA to the objective this axis moves, before\n"
            "Log Cal; then recalibrate. Too high an NA gives too short a dither\n"
            "move and a small, noisy dither Err."
        ),
    ),
]
_READOUT_PROPS = [prop for prop, _, _ in _READOUTS]
# Read on every slow-poll tick; the rest of _READOUTS only on a full read.
_FAST_PROPS = [_ERROR_PROP, _SUM_PROP]

# Actuator badge styling: (background, foreground).
_ACTUATOR_BADGE: dict[str, tuple[str, str]] = {
    "piezo": ("#6a1b9a", "#ffffff"),
    "stage": ("#00695c", "#ffffff"),
    "unknown": ("#616161", "#ffffff"),
}

# Adjustable parameters exposed via pymmcore-widgets: (property, label, tooltip).
_PARAMS: list[tuple[str, str, str]] = [
    (
        "LED Intensity",
        "LED Intensity (%)",
        (
            "UL X. More light improves signal-to-noise (ASI default 50%).\n"
            "Redo Log Cal after changing it.\n"
            "A new intensity takes effect when the LED is relit: in Idle (LED on)\n"
            "or Ready the panel relights it for you. While locked, unlock and\n"
            "re-lock to apply it (the panel does not relight a locked axis)."
        ),
    ),
    (
        "GainMultiplier",
        "Loop Gain",
        (
            "LR T. Higher responds faster but can oscillate; reduce it if the lock\n"
            "jitters. Raise it when you raise Update rate. ASI: with Averages 5-8,\n"
            "Loop Gain 1-5 can hold the focus very still."
        ),
    ),
    (
        "Objective NA",
        "Objective NA",
        "LR Y. Sets the calibration move (1.5 µm / NA²) and the In-Focus Range.",
    ),
    (
        "Number of Averages",
        "Averages (2^N)",
        (
            "RT F = N: rolling average of 2^N error samples. ASI: N of 5-8 with\n"
            "Loop Gain 1-5 can significantly smooth marginal focus and hold it very\n"
            "still on a stationary sample. Too much averaging adds delay, which can\n"
            "cause oscillation; then reduce N or Loop Gain."
        ),
    ),
    (
        "Max Lock Range(mm)",
        "Lock Range (mm)",
        (
            "LR Z. Maximum travel from the lock point before CRISP unlocks\n"
            "(runaway protection)."
        ),
    ),
    (
        "In Focus Range(um)",
        "In-Focus Range (µm)",
        (
            "AL Z. Only decides when the state reads In Focus rather than Lock; it\n"
            "does not change the servo."
        ),
    ),
    (
        "Number of Skips",
        "Update rate (ms)",
        (
            'UL Y ("Number of Skips" on older firmware; milliseconds on Tiger 3.38+).\n'
            "ASI: increase it to improve stability with a piezo (piezo default 5 ms)\n"
            "and raise Loop Gain as you do."
        ),
    ),
    (
        "Wait ms after Lock",
        "Wait after Lock (ms)",
        (
            "Adapter setting only: how long Micro-Manager's autofocus call waits\n"
            "after locking."
        ),
    ),
]

_LOCKED_STATES = {"Lock", "In Focus"}
# States in which a hold test runs unlocked (CRISP not driving the actuator).
_UNLOCKED_HOLD_STATES = {"Ready", "Idle"}
_BUSY_STATES = {
    "loG_cal": "Log Cal…",
    "Dither": "Dither…",
    "gain_Cal": "Gain Cal…",
    "Calibrating": "Calibrating…",
}
_BAD_STATES = {"Error", "Inhibit", "Dim"}
_GOOD_STATES = {"Lock", "In Focus", "Ready"}

# Diagnostic tools; one runs at a time, and the slow poll is paused while it does.
_DITHER, _CURVE, _HOLD = DITHER, CURVE, HOLD
_TOOL_TITLES = {_DITHER: "Dither", _CURVE: "Focus curve", _HOLD: "Hold test"}
# The Dither assistant re-reads the state this often, and stops after this many reads
# in a row outside the dither states.
_DITHER_STATE_EVERY_S = 0.5
_DITHER_OUTSIDE_TO_STOP = 2
_DITHER_PAINT_S = 0.1
_HOLD_PAINT_S = 0.2
_HOLD_RESULTS_KEPT = 6

_GREEN, _DARK_GREEN, _ORANGE, _RED, _GREY = (
    "#4caf50",
    "#2e7d32",
    "#ff9800",
    "#f44336",
    "#9e9e9e",
)
_BLUE = "#1e88e5"


def _close(a: float | None, b: float | None, *, rtol: float = 1e-3) -> bool:
    """Whether two optional floats are equal within *rtol* (both ``None`` counts)."""
    if a is None or b is None:
        return a is b
    return abs(a - b) <= rtol * max(abs(a), abs(b), 1.0)


def _to_float(text: str | None) -> float | None:
    """Parse a property string, ``None`` if it is missing or not a number."""
    try:
        return float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None, spec: str, missing: str = "--") -> str:
    return missing if value is None else format(value, spec)


def _err_color(level: float | None) -> str:
    if level is None:
        return _GREY
    if level >= ERR_PLENTY:
        return _DARK_GREEN
    if level >= ERR_TARGET:
        return _GREEN
    if level >= ERR_MANUAL_MIN:
        return _ORANGE
    return _RED


class _WheelGuard(QObject):
    """Event filter: a wheel event on an unfocused control scrolls the panel instead."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(obj, QWidget)
            and not obj.hasFocus()
        ):
            parent = obj.parentWidget()
            while parent is not None and not isinstance(parent, QAbstractScrollArea):
                parent = parent.parentWidget()
            if parent is not None and isinstance(event, QWheelEvent):
                # Scroll the panel by what the wheel would normally scroll it by.
                bar = parent.verticalScrollBar()
                notches = event.angleDelta().y() / 120.0
                lines = QApplication.wheelScrollLines()
                bar.setValue(bar.value() - round(notches * lines * bar.singleStep()))
            return True  # never reaches the control
        return False


@dataclass(frozen=True)
class CrispSnapshot:
    """A plain snapshot of CRISP telemetry, read off the GUI thread.

    Produced on a worker thread by :meth:`CrispControlPanel._read_device_values` and
    consumed on the GUI thread by :meth:`CrispControlPanel._apply_values`, so it holds
    only raw values (no Qt widgets). ``full`` is ``True`` when every readout was read;
    otherwise only the fast-changing ones are present.
    """

    state: str
    readouts: dict[str, str]
    full: bool = False


@dataclass
class _Job:
    fn: Callable[[], object]
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Exception | None = None


class _GuiCall(QObject):
    """Run a function on the GUI thread for a worker thread, which waits for it.

    The focus curve snaps the way a Snap button would, on the GUI thread (the host's
    Snap expects it, and image viewers update there); the scan's worker blocks until
    the frames, or the error, come back.
    """

    _request = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._request.connect(self._run)

    def call(self, fn: Callable[[], object], timeout: float) -> object:
        job = _Job(fn)
        self._request.emit(job)  # queued: this runs on a worker thread
        if not job.done.wait(timeout):
            raise TimeoutError(f"no image within {timeout:.0f} s")
        if job.error is not None:
            raise job.error
        return job.result

    def _run(self, job: _Job) -> None:
        try:
            job.result = job.fn()
        except Exception as e:  # noqa: BLE001 - handed back to the waiting worker
            job.error = e
        finally:
            job.done.set()


class _FrameView(QGraphicsView):
    """One full image, shown the way the host's preview windows show it.

    Grey, with the contrast stretched from the frame's minimum to its maximum, at full
    resolution; the wheel zooms and dragging pans. Used when the host provides no
    viewer of its own (:func:`.imaging.set_image_view_factory`).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self.scene().addItem(self._item)
        self._shape: tuple[int, int] | None = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(40, 40, 40))
        self.setMinimumSize(320, 320)

    def append(self, img: np.ndarray) -> None:
        a = img[..., :3].mean(axis=-1) if img.ndim == 3 else img
        lo, hi = float(a.min()), float(a.max())
        scale = 255.0 / (hi - lo) if hi > lo else 0.0
        a8 = ((a - lo) * scale).astype("uint8")
        h, w = a8.shape
        qimg = QImage(a8.tobytes(), w, h, w, QImage.Format.Format_Grayscale8)
        self._item.setPixmap(QPixmap.fromImage(qimg.copy()))
        if self._shape != (h, w):
            self._shape = (h, w)
            self.scene().setSceneRect(0, 0, w, h)
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is None:
            return
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)


class _CurveImagesWindow(QWidget):
    """The focus curve's images: one view per camera and a slider over the positions.

    Each view is the host's own (the widget of its preview windows, through
    :func:`.imaging.set_image_view_factory`) or :class:`_FrameView`. A window of its
    own, not part of the panel: a GPU canvas inside the panel's scroll area wouldn't
    clip when the panel scrolls.

    Parameters
    ----------
    title
        The window title.
    frame_at
        Returns ``(z, {camera: frame})`` for a position index.
    parent
        The panel; the window is closed and deleted with it.
    """

    def __init__(
        self,
        title: str,
        frame_at: Callable[[int], tuple[float, dict[str, np.ndarray]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(title)
        self._frame_at = frame_at
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setToolTip("Step through the images by position.")
        self.slider.valueChanged.connect(self._display)
        self.caption = QLabel()
        top.addWidget(self.slider, 1)
        top.addWidget(self.caption)
        v.addLayout(top)
        self._row = QHBoxLayout()
        v.addLayout(self._row, 1)
        self._views: dict[str, QWidget] = {}
        self._count = 0
        self.resize(1000, 560)

    def reset(self) -> None:
        """A new scan: no positions yet (the views stay, showing the last image)."""
        self._count = 0
        self.slider.blockSignals(True)
        self.slider.setRange(0, 0)
        self.slider.blockSignals(False)
        self.caption.setText("waiting for the first image…")

    def set_count(self, count: int) -> None:
        self._count = count
        self.slider.setRange(0, max(0, count - 1))

    def show_index(self, index: int) -> None:
        if self.slider.value() == index:
            self._display(index)
        else:
            self.slider.setValue(index)

    def _view(self, camera: str) -> QWidget:
        view = self._views.get(camera)
        if view is not None:
            return view
        view = None
        factory = image_view_factory()
        if factory is not None:
            with suppress(Exception):
                view = factory(camera)
        if not isinstance(view, QWidget) or not callable(getattr(view, "append", None)):
            view = _FrameView()
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(QLabel(camera))
        col.addWidget(view, 1)
        self._row.addWidget(box, 1)
        self._views[camera] = view
        detach = getattr(view, "detach", None)  # host previews listen to the core
        if callable(detach):
            self.destroyed.connect(lambda *_: detach())
        return view

    def _display(self, index: int) -> None:
        if not 0 <= index < self._count:
            return
        z, frames = self._frame_at(index)
        for camera, frame in frames.items():
            self._view(camera).append(frame)  # type: ignore[attr-defined]
        self.caption.setText(
            f"z = {z:+.2f} µm from the start ({index + 1} / {self._count})"
        )


class _Section(QWidget):
    """A titled section whose body shows or hides when the title is clicked.

    Parameters
    ----------
    title
        The clickable title.
    body
        The widget shown when the section is expanded.
    expanded
        Whether the section starts expanded.
    parent
        Optional parent widget.
    """

    def __init__(
        self,
        title: str,
        body: QWidget,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._body = body
        self.toggle_btn = QToolButton()
        self.toggle_btn.setText(title)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_btn.setStyleSheet(
            "QToolButton { border: none; font-weight: bold; }"
        )
        self.toggle_btn.toggled.connect(self._set_expanded)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.toggle_btn)
        v.addWidget(body)
        self.toggle_btn.setChecked(expanded)
        self._set_expanded(expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle_btn.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._body.setVisible(expanded)


class _ToolsWindow(QWidget):
    """The Focus curve and Hold test in a window of their own.

    Not part of the panel: with their plots and the hold table they would make the
    dock too big to leave room for the images. Closing the window stops a running
    tool, as hiding the panel does. (The small Dither assistant stays in the panel.)

    Parameters
    ----------
    title
        The window title.
    tabs
        The tab widget holding the tools.
    on_close
        Called when the window is closed.
    parent
        The panel; the window is closed and deleted with it.
    """

    def __init__(
        self,
        title: str,
        tabs: QTabWidget,
        on_close: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(title)
        self._tabs = tabs
        self._on_close = on_close
        QVBoxLayout(self).addWidget(tabs)
        self.resize(540, 520)  # min. 506 x 435 (offscreen); the plots take the rest

    def show_tab(self, tab: QWidget | None = None) -> None:
        """Select *tab* (if given) and bring the window up."""
        if tab is not None:
            self._tabs.setCurrentWidget(tab)
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self._on_close()
        super().closeEvent(event)


class CrispControlPanel(QWidget):
    """Control + telemetry panel for a single CRISP axis."""

    # Carries tool events from worker threads to the GUI thread as (kind, payload).
    # Emitting from a non-GUI thread makes Qt queue the delivery.
    _tool_event = Signal(str, object)

    def __init__(
        self,
        controller: CrispController,
        mmcore: CMMCorePlus | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.mmcore = mmcore or CMMCorePlus.instance()
        self._readouts: dict[str, QLabel] = {}
        self._last_readouts: dict[str, str] = {}
        # Cached error->µm scaling and a rolling history for the fluctuation metric.
        self._sensitivity: float | None = None  # from Cal Gain / Cal Range (assumed)
        # Slope measured by the last focus curve, counts/µm (signed). It replaces the
        # calibration-derived figure for µm conversions until Log Cal / Dither.
        self._measured_slope: float | None = None
        self._wheel_guard = _WheelGuard(self)
        self._error_history: deque[float] = deque(maxlen=_STABILITY_WINDOW)
        self._last_state = ""
        # Edge-detector for the auto joystick lockout: starts False so a state
        # already locked when this panel first opens is treated as a lock edge
        # (joystick gets disabled), matching the "always safe on lock" default.
        self._was_locked = False

        # Slow-poll bookkeeping. _poll_* is only touched by the poll's worker (reads
        # run one at a time); _full_read_due is set by the GUI to ask for a full read.
        self._poll_last_state: str | None = None
        self._poll_last_full = float("-inf")
        self._full_read_due = True
        self._mda_running = False

        # Diagnostic tools.
        self._active_tool: str | None = None
        self._dither_tracker = DitherTracker(DEFAULT_WINDOW_S)
        self._dither_sampler: Sampler[DitherSample] | None = None
        self._dither_next_state = 0.0  # worker-only
        self._dither_outside = 0
        self._dither_last_in = float("-inf")  # time of the last read in a dither state
        self._last_dither_paint = 0.0
        self._curve_stop = threading.Event()
        self._curve_points: list[CurvePoint] = []
        self._curve_scan: CurveScan | None = None
        self._curve_analysis: CurveAnalysis | None = None
        # The last scan's full frames (kept on disk), the z of each, their window.
        self._curve_frames: FrameStore | None = None
        self._curve_frame_z: list[float] = []
        self._curve_images: _CurveImagesWindow | None = None
        self._gui_call = _GuiCall(self)
        self._hold: HoldTest | None = None
        self._hold_samples: list[HoldSample] = []
        self._hold_results: list[HoldResult] = []
        self._last_hold_paint = 0.0

        # The ASITiger adapter's "RefreshPropertyValues" is a software-only flag that
        # makes later reads of the cached properties (Lock Offset, Calibration
        # Gain/Range, the parameters) query the controller; it stays set once set.
        self._can_refresh = False
        with suppress(Exception):
            self._can_refresh = self.mmcore.hasProperty(
                self.controller.label, "RefreshPropertyValues"
            )
        if self._can_refresh:
            with suppress(Exception):
                self.mmcore.setProperty(
                    self.controller.label, "RefreshPropertyValues", "Yes"
                )

        self._build()
        self._tool_event.connect(self._on_tool_event)

        # Poll telemetry off the GUI thread so the blocking serial reads never stall
        # the event loop / live view. See AsyncPoller. Imported here (not at module
        # scope) to keep ``import CRISPy.ui`` lightweight.
        from pymmcore_widgets.control._async_poller import AsyncPoller

        self._poller: AsyncPoller[CrispSnapshot] = AsyncPoller(
            self._read_device_values,
            self._apply_values,
            interval_ms=self.poll_interval.value(),
            parent=self,
            active=self.isVisible,
        )
        # Populate immediately (ignoring the visibility gate), then poll periodically.
        self._poller.poll_now(force=True)
        self._poller.start()

        # Pause polling automatically during an MDA so the Tiger serial port
        # isn't loaded while acquiring (the lock itself is held by the card).
        with suppress(Exception):
            self.mmcore.mda.events.sequenceStarted.connect(self._on_mda_started)
            self.mmcore.mda.events.sequenceFinished.connect(self._on_mda_finished)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # --- header: device + actuator badge + current state --------------
        header = QHBoxLayout()
        title = QLabel(self.controller.label)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        header.addWidget(title)
        header.addWidget(self._make_actuator_badge())
        self.state_lbl = QLabel("State: --")
        self.state_lbl.setStyleSheet("font-weight: bold;")
        self.state_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addStretch()
        header.addWidget(self.state_lbl)
        layout.addLayout(header)

        # --- polling controls (pause/slow to avoid lag during acquisition) -
        poll_row = QHBoxLayout()
        self.poll_chk = QCheckBox("Poll")
        self.poll_chk.setChecked(True)
        self.poll_chk.setToolTip(
            "Periodically read the state, Dither Error and Sum (the other readouts\n"
            "every 10 s, after a button press and when the state changes).\n"
            "Polling pauses automatically while an acquisition or a diagnostic\n"
            "tool is running. ASI's own plugin suggests turning polling off once\n"
            "calibration is done."
        )
        self.poll_chk.toggled.connect(self._on_poll_toggled)
        self.poll_interval = QSpinBox()
        self.poll_interval.setRange(100, 10000)
        self.poll_interval.setSingleStep(50)
        self.poll_interval.setValue(_POLL_INTERVAL_MS)
        self.poll_interval.setSuffix(" ms")
        self.poll_interval.setMaximumWidth(110)
        self.poll_interval.valueChanged.connect(self._on_interval_changed)
        poll_row.addWidget(self.poll_chk)
        poll_row.addWidget(self.poll_interval)
        poll_row.addStretch()
        layout.addLayout(poll_row)

        # --- readouts -----------------------------------------------------
        ro_box = self.readouts_box = QGroupBox("Readouts")
        grid = QGridLayout(ro_box)
        for i, (prop, label, tip) in enumerate(_READOUTS):
            row, col = divmod(i, 2)
            name = QLabel(f"{label}:")
            val = QLabel("--")
            val.setStyleSheet("font-weight: bold;")
            name.setToolTip(tip)
            val.setToolTip(tip)
            grid.addWidget(name, row, col * 2)
            grid.addWidget(val, row, col * 2 + 1)
            self._readouts[prop] = val

        # Derived, full-width rows: physical scaling and fluctuation of the error.
        next_row = (len(_READOUTS) + 1) // 2
        self.sensitivity_lbl = QLabel("Sensitivity: --")
        self.sensitivity_lbl.setToolTip(
            "How many counts of the error readout one µm of focus makes, measured by\n"
            "the Focus curve (until the next Log Cal or Dither). The µm figures in\n"
            "this panel use it.\n\n"
            "|Calibration Gain| / Calibration Range is shown too, but it is on a\n"
            "different, internal scale (on the rig ~300× the measured slope), so it\n"
            "is not used for conversions."
        )
        self.sensitivity_lbl.setWordWrap(True)
        grid.addWidget(self.sensitivity_lbl, next_row, 0, 1, 4)
        self.stability_lbl = QLabel("In-loop error: --")
        self.stability_lbl.setToolTip(
            f"Std-dev / peak-to-peak of {_ERROR_PROP} over the last "
            f"{_STABILITY_WINDOW} polls while locked.\n"
            "This is the error inside the feedback loop: it can look quiet while\n"
            "the actuator moves. Use the Hold test to measure the focus motion."
        )
        self.stability_lbl.setWordWrap(True)
        grid.addWidget(self.stability_lbl, next_row + 1, 0, 1, 4)
        layout.addWidget(ro_box)

        # --- operation (2x2 grid to stay compact) -------------------------
        op_box = QGroupBox("Operation")
        op = QGridLayout(op_box)
        self.btn_idle_ready = QPushButton("Set Ready")
        self.btn_idle_ready.clicked.connect(self._on_idle_ready)
        self.btn_lock = QPushButton("Lock")
        self.btn_lock.clicked.connect(self._on_lock)
        self.btn_offset = QPushButton("Set Offset")
        self.btn_offset.clicked.connect(self._on_reset_offset)
        self.btn_save = QPushButton("Save to Controller")
        self.btn_save.clicked.connect(lambda: self._do(self.controller.set_save))
        op.addWidget(self.btn_idle_ready, 0, 0)
        op.addWidget(self.btn_lock, 0, 1)
        op.addWidget(self.btn_offset, 1, 0)
        op.addWidget(self.btn_save, 1, 1)
        self.btn_led = QPushButton("LED On")
        self.btn_led.setCheckable(True)
        self.btn_led.setToolTip(
            "Turn this axis's LED on or off by itself (no recalibration, no lock).\n"
            "Addressed to this axis's own card, so the Z and P axes are independent.\n"
            "A new LED Intensity takes effect when the LED is relit: in Idle (LED on)\n"
            "or Ready the panel relights it after you change it; while locked, unlock\n"
            "and re-lock to apply it (the panel does not relight a locked axis)."
        )
        self.btn_led.clicked.connect(self._on_led_clicked)
        op.addWidget(self.btn_led, 2, 0, 1, 2)
        # Shown only while the last LED write failed; cleared on the next success.
        self.led_error_lbl = QLabel()
        self.led_error_lbl.setWordWrap(True)
        self.led_error_lbl.setStyleSheet(f"color: {_RED};")
        self.led_error_lbl.hide()
        op.addWidget(self.led_error_lbl, 3, 0, 1, 2)
        # Debounces LED Intensity edits into one relight (see _on_led_intensity_edited).
        self._led_timer = QTimer(self)
        self._led_timer.setSingleShot(True)
        self._led_timer.setInterval(_LED_REAPPLY_DELAY_MS)
        self._led_timer.timeout.connect(self._reapply_led)
        self.joystick_chk = QCheckBox("Joystick enabled")
        self.joystick_chk.setChecked(True)
        self.joystick_chk.setToolTip(
            "Manual wheel/joystick input for this axis's actuator.\n"
            "Auto-disabled the moment this axis locks (so it can't fight the\n"
            "servo and drag focus, ASI 'J' command) and auto-re-enabled on\n"
            "unlock. Toggle here to override at any time, including while"
            " locked."
        )
        self.joystick_chk.toggled.connect(self._on_joystick_toggled)
        op.addWidget(self.joystick_chk, 4, 0, 1, 2)
        layout.addWidget(op_box)

        # --- calibration --------------------------------------------------
        cal_box = QGroupBox("Calibration")
        cal_box.setToolTip("In order: 1. Log Cal → 2. Dither → 3. Set Gain.")
        cal = QHBoxLayout(cal_box)
        self.btn_log = QPushButton("1. Log Cal")
        self.btn_log.clicked.connect(self._on_log_cal_clicked)
        self.btn_dither = QPushButton("2. Dither")
        self.btn_dither.clicked.connect(self._on_dither_clicked)
        self.btn_gain = QPushButton("3. Set Gain")
        self.btn_gain.clicked.connect(lambda: self._do(self.controller.set_gain_cal))
        for b in (self.btn_log, self.btn_dither, self.btn_gain):
            cal.addWidget(b)
        layout.addWidget(cal_box)

        # --- Dither assistant (compact, in the panel; collapsed until used) ---
        self.dither_section = _Section("Dither", self._build_dither_body())
        self.dither_section.toggle_btn.toggled.connect(self._on_dither_section_toggled)
        layout.addWidget(self.dither_section)

        # --- Focus curve / Hold test (in a window of their own; _ToolsWindow) -
        self.tools_tabs = QTabWidget()
        self._curve_tab = self._build_curve_tab()
        self._hold_tab = self._build_hold_tab()
        self.tools_tabs.addTab(self._curve_tab, "Focus curve")
        self.tools_tabs.addTab(self._hold_tab, "Hold test")
        self.tools_window = _ToolsWindow(
            f"CRISP focus curve / hold test: {self.controller.label}",
            self.tools_tabs,
            self._stop_window_tools,
            parent=self,
        )
        tools_row = QHBoxLayout()
        self.tools_btn = QPushButton("Focus curve / Hold test…")
        self.tools_btn.setToolTip(
            "Opens the Focus curve and the Hold test in a window of their own.\n"
            "Closing it stops a running tool. (The Dither assistant is the\n"
            "Dither section above.)"
        )
        self.tools_btn.clicked.connect(lambda: self.tools_window.show_tab())
        self.tools_running_lbl = QLabel()
        self.tools_running_lbl.setStyleSheet(f"color: {_ORANGE}; font-weight: bold;")
        tools_row.addWidget(self.tools_btn)
        tools_row.addWidget(self.tools_running_lbl)
        tools_row.addStretch()
        layout.addLayout(tools_row)

        # --- parameters (reused pymmcore-widgets property controls) -------
        param_body = QWidget()
        form = QFormLayout(param_body)
        self._add_parameter_widgets(form)
        self._param_body = param_body
        self.params_section = _Section("Parameters", param_body)
        layout.addWidget(self.params_section)

        layout.addStretch()
        self._sync_tool_controls()

    def _build_dither_body(self) -> QWidget:
        """Build the Dither assistant's widgets (kept narrow: it sits in the panel)."""
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        self.dither_status = QLabel()
        self.dither_status.setWordWrap(True)
        v.addWidget(self.dither_status)

        row = QHBoxLayout()
        self.dither_level_lbl = QLabel("|Err| --")
        self.dither_level_lbl.setWordWrap(True)
        self.dither_level_lbl.setStyleSheet("font-weight: bold; font-size: 22px;")
        self.dither_level_lbl.setToolTip(
            "Median of |Dither Err| over the smoothing window, so the brief jumps\n"
            "from moving the detector are ignored."
        )
        self.dither_peak_lbl = QLabel("peak --")
        self.dither_peak_lbl.setStyleSheet("font-weight: bold;")
        row.addWidget(self.dither_level_lbl)
        row.addStretch()
        row.addWidget(self.dither_peak_lbl)
        v.addLayout(row)

        self.dither_sum_lbl = QLabel("Sum --")
        self.dither_sum_lbl.setWordWrap(True)
        self.dither_detail_lbl = QLabel()
        self.dither_detail_lbl.setWordWrap(True)
        self.dither_detail_lbl.setToolTip(
            "Scatter: the standard deviation of |Err| over the smoothing window, in\n"
            "counts and as % of |Err|. Counts compare directly between axes; a large\n"
            "% means the peak and the readings move by that much from noise alone."
        )
        v.addWidget(self.dither_sum_lbl)
        v.addWidget(self.dither_detail_lbl)

        self.dither_plot = LinePlot(
            x_label="seconds ago", y_label="|Err|", min_height=120
        )
        v.addWidget(self.dither_plot)

        ctl = QHBoxLayout()
        self.dither_watch_btn = QPushButton("Watch")
        self.dither_watch_btn.setCheckable(True)
        self.dither_watch_btn.setToolTip(
            "Start reading the dither Err fast. It starts by itself when you press\n"
            "2. Dither and stops when the state leaves Dither."
        )
        self.dither_watch_btn.toggled.connect(self._on_dither_watch_toggled)
        self.dither_reset_btn = QPushButton("Reset peak")
        self.dither_reset_btn.clicked.connect(self._on_dither_reset)
        self.dither_window = QDoubleSpinBox()
        self.dither_window.setRange(0.3, 5.0)
        self.dither_window.setSingleStep(0.1)
        self.dither_window.setDecimals(1)
        self.dither_window.setValue(DEFAULT_WINDOW_S)
        self.dither_window.setSuffix(" s")
        self.dither_window.setToolTip(
            "Smoothing window. Longer ignores longer-lasting jumps from moving the\n"
            "detector, but lags the screw more."
        )
        self.dither_window.valueChanged.connect(
            lambda s: self._dither_tracker.set_window(float(s))
        )
        ctl.addWidget(self.dither_watch_btn)
        ctl.addWidget(self.dither_reset_btn)
        ctl.addStretch()
        smooth_lbl = QLabel("Smooth:")  # short: the row must stay narrow
        smooth_lbl.setToolTip(self.dither_window.toolTip())
        ctl.addWidget(smooth_lbl)
        ctl.addWidget(self.dither_window)
        v.addLayout(ctl)
        self._paint_dither(force=True)
        return body

    def _build_curve_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout(tab)
        intro = QLabel(
            "Steps the focus actuator around its current position with CRISP in "
            "Ready (unlocked), reads the error at each step, then returns it to "
            "the start. Shows the slope at the lock point, how far the lock point "
            "is from the curve's peaks, the capture range and the noise floor."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        opts = QGridLayout()
        self.curve_range = QDoubleSpinBox()
        self.curve_range.setRange(0.5, 50.0)
        self.curve_range.setDecimals(1)
        self.curve_range.setValue(
            DEFAULT_PIEZO_HALF_RANGE_UM
            if self.controller.actuator_type == "piezo"
            else DEFAULT_HALF_RANGE_UM
        )
        self.curve_range.setPrefix("± ")
        self.curve_range.setSuffix(" µm")
        self.curve_step = QDoubleSpinBox()
        self.curve_step.setRange(0.02, 5.0)
        self.curve_step.setDecimals(2)
        self.curve_step.setSingleStep(0.05)
        self.curve_step.setValue(DEFAULT_STEP_UM)
        self.curve_step.setSuffix(" µm")
        self.curve_settle = QSpinBox()
        self.curve_settle.setRange(0, 2000)
        self.curve_settle.setSingleStep(10)
        self.curve_settle.setValue(round(DEFAULT_SETTLE_S * 1000))
        self.curve_settle.setSuffix(" ms")
        self.curve_settle.setToolTip("Wait after each move before reading.")
        self.curve_reads = QSpinBox()
        self.curve_reads.setRange(1, 20)
        self.curve_reads.setValue(DEFAULT_READS)
        self.curve_reads.setToolTip(
            "Error reads per position. Their spread gives the noise estimate\n"
            "(needs at least 2)."
        )
        for col, (text, w) in enumerate(
            (
                ("Range", self.curve_range),
                ("Step", self.curve_step),
                ("Settle", self.curve_settle),
                ("Reads", self.curve_reads),
            )
        ):
            opts.addWidget(QLabel(text), 0, col)
            opts.addWidget(w, 1, col)
        v.addLayout(opts)

        btns = QHBoxLayout()
        self.curve_run_btn = QPushButton("Run focus curve")
        self.curve_run_btn.clicked.connect(self._on_curve_run)
        self.curve_save_btn = QPushButton("Save CSV…")
        self.curve_save_btn.clicked.connect(self._on_curve_save)
        btns.addWidget(self.curve_run_btn)
        btns.addWidget(self.curve_save_btn)
        btns.addStretch()
        v.addLayout(btns)
        self.curve_snap = QCheckBox("Snap an image at each position")
        self.curve_snap.setToolTip(
            "Snaps the way the host's Snap does (its cameras and illumination), or\n"
            "with the core's current camera when the host doesn't provide that. Each\n"
            "position takes an exposure (plus the camera's overhead) longer. The\n"
            "images show the focus going in and out; their sharpness marks where the\n"
            "image is in focus, to compare with the lock point."
        )
        v.addWidget(self.curve_snap)

        self.curve_status = QLabel("Needs the Ready state (LED on, unlocked).")
        self.curve_status.setWordWrap(True)
        v.addWidget(self.curve_status)
        self.curve_plot = LinePlot(x_label="z from start (µm)", y_label="error")
        v.addWidget(self.curve_plot)

        self.curve_images_box = QWidget()
        box = QVBoxLayout(self.curve_images_box)
        box.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Sharpness of"))
        self.curve_camera = QComboBox()
        self.curve_camera.setToolTip("Which camera's sharpness to plot and report.")
        self.curve_camera.currentIndexChanged.connect(self._on_curve_camera)
        row.addWidget(self.curve_camera)
        row.addStretch()
        self.curve_images_btn = QPushButton("Show images")
        self.curve_images_btn.setToolTip(
            "The images at every position, full size, one view per camera."
        )
        self.curve_images_btn.clicked.connect(self._on_curve_show_images)
        row.addWidget(self.curve_images_btn)
        box.addLayout(row)
        self.curve_sharp_plot = LinePlot(
            x_label="z from start (µm)",
            y_label="image sharpness (relative)",
            min_height=110,
        )
        box.addWidget(self.curve_sharp_plot)
        self.curve_images_box.setVisible(False)
        v.addWidget(self.curve_images_box)
        self.curve_result_lbl = QLabel()
        self.curve_result_lbl.setWordWrap(True)
        self.curve_result_lbl.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.curve_result_lbl)
        return tab

    def _build_hold_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout(tab)
        intro = QLabel(
            "Records the actuator's own position and splits it into drift (the "
            "trend), wander (how much the 1 s averages move) and jitter (the spread "
            "within each second). Run it unlocked (Ready) to see the actuator by "
            "itself, then locked to see what the lock adds. It changes no settings: "
            "change one Parameter at a time and run it again to compare."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        opts = QHBoxLayout()
        self.hold_duration = QSpinBox()
        self.hold_duration.setRange(5, 3600)
        self.hold_duration.setValue(round(DEFAULT_DURATION_S))
        self.hold_duration.setSuffix(" s")
        self.hold_rate = QDoubleSpinBox()
        self.hold_rate.setRange(1.0, 50.0)
        self.hold_rate.setDecimals(0)
        self.hold_rate.setValue(DEFAULT_RATE_HZ)
        self.hold_rate.setSuffix(" Hz")
        self.hold_rate.setToolTip(
            "Samples per second (each is 2 reads). Run once at a low and once at a\n"
            "high rate to see whether the polling itself disturbs the lock."
        )
        self.hold_start_btn = QPushButton("Start hold test")
        self.hold_start_btn.clicked.connect(self._on_hold_start)
        self.hold_save_btn = QPushButton("Save CSV…")
        self.hold_save_btn.clicked.connect(self._on_hold_save)
        opts.addWidget(QLabel("Duration"))
        opts.addWidget(self.hold_duration)
        opts.addWidget(QLabel("Rate"))
        opts.addWidget(self.hold_rate)
        opts.addStretch()
        v.addLayout(opts)
        btns = QHBoxLayout()
        btns.addWidget(self.hold_start_btn)
        btns.addWidget(self.hold_save_btn)
        btns.addStretch()
        v.addLayout(btns)

        self.hold_status = QLabel(
            "Runs locked (Lock / In Focus) or unlocked (Ready / Idle). Unlocked, "
            "CRISP isn't driving the actuator, so it shows the actuator's own noise."
        )
        self.hold_status.setWordWrap(True)
        v.addWidget(self.hold_status)
        self.hold_plot = LinePlot(x_label="seconds", y_label="position (nm)")
        v.addWidget(self.hold_plot)

        cols = [
            "Started",
            "Mode",
            "Drift nm/min",
            "Jitter nm",
            "Wander nm",
            "pk-pk nm",
            "Err rms",
            "Err /min",
            "Averages",
            "Loop Gain",
            "Update (ms)",
            "LED %",
            "Note",
        ]
        self.hold_table = QTableWidget(0, len(cols))
        self.hold_table.setHorizontalHeaderLabels(cols)
        self.hold_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hold_table.verticalHeader().setVisible(False)
        self.hold_table.setMinimumHeight(120)
        self.hold_table.setToolTip(
            "Newest first. All after removing the drift (the straight-line trend).\n"
            "Jitter: rms spread within each second. Wander: rms of the 1 s averages.\n"
            "pk-pk: largest minus smallest position.\n"
            "Err rms: the error about its trend. Err /min: the error's trend (counts/min)."
        )
        v.addWidget(self.hold_table)
        return tab

    def _make_actuator_badge(self) -> QLabel:
        actuator = self.controller.actuator_type
        text = {"piezo": "Piezo", "stage": "Stage"}.get(actuator, "Actuator ?")
        bg, fg = _ACTUATOR_BADGE.get(actuator, _ACTUATOR_BADGE["unknown"])
        badge = QLabel(text)
        badge.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 6px;"
            " padding: 1px 6px; font-weight: bold; font-size: 11px;"
        )
        tip = f"Paired focus actuator: {self.controller.actuator_label or text}."
        if actuator == "piezo":
            rng = self._read_float_prop("Max Lock Range(mm)")
            if rng is not None and rng > 1.0:
                tip += (
                    f"\n⚠ Max Lock Range is {rng:g} mm — unusually large for a "
                    "piezo; verify it matches the piezo's travel range."
                )
        badge.setToolTip(tip)
        return badge

    def _read_float_prop(self, prop: str) -> float | None:
        try:
            return float(self.mmcore.getProperty(self.controller.label, prop))
        except Exception:
            return None

    def _counts_per_um(self) -> float | None:
        """Error-readout counts per µm, from the last focus curve; else ``None``.

        The Cal Gain / Cal Range figure is on a different, internal scale (on the rig
        the Z axis's was ~300× the measured slope), so it is never used to convert
        the error readout to µm.
        """
        if self._measured_slope:
            return abs(self._measured_slope)
        return None

    def _to_um(self, counts: float) -> float | None:
        """Convert raw error counts to microns (see :meth:`_counts_per_um`)."""
        sens = self._counts_per_um()
        if not sens:  # None or 0
            return None
        return counts / sens

    def _set_measured_slope(self, slope: float | None) -> None:
        if slope != self._measured_slope:
            self._error_history.clear()  # a different scale for the nm figures
        self._measured_slope = slope
        self._render_sensitivity()

    def _render_sensitivity(self) -> None:
        cal = self._sensitivity
        cal_txt = f"Cal Gain / Range {cal:.0f} (internal scale)" if cal else ""
        if self._measured_slope:
            text = (
                f"Error readout: {abs(self._measured_slope):.1f} counts/µm "
                "(focus curve)"
            )
            if cal_txt:
                text += f" · {cal_txt}"
        elif cal_txt:
            text = f"{cal_txt} · run a Focus curve for the error's counts/µm"
        else:
            text = "Sensitivity: -- (uncalibrated; run a Focus curve)"
        self.sensitivity_lbl.setText(text)

    def _add_parameter_widgets(self, form: QFormLayout) -> None:
        # Imported lazily so the module still imports without a Qt app / in demos.
        from pymmcore_widgets import PropertyWidget

        # Keep the value controls at their natural (compact) size instead of
        # stretching the sliders across the whole panel, which would otherwise
        # require a very wide dock and clip the value box on the right.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        label = self.controller.label
        for prop, friendly, tip in _PARAMS:
            try:
                if not self.mmcore.hasProperty(label, prop):
                    continue
                wdg = PropertyWidget(label, prop, mmcore=self.mmcore)
            except Exception:
                continue
            wdg.setMaximumWidth(_PARAM_MAX_WIDTH)
            wdg.setToolTip(tip)
            self._install_wheel_guard(wdg)
            if prop == LED_PROP:
                # Emitted after the widget wrote the property, for user edits only
                # (a refresh from the core blocks the value widget's signals).
                wdg.valueChanged.connect(self._on_led_intensity_edited)
            name = QLabel(friendly)
            name.setToolTip(tip)
            form.addRow(name, wdg)

    def _install_wheel_guard(self, widget: QWidget) -> None:
        """Make *widget* (and its children) ignore the wheel unless clicked first.

        The parameter controls are sliders and spin boxes inside a scrolling panel:
        without this, scrolling the panel over one silently changes a controller
        setting (Objective NA, LED, Loop Gain, ...). The wheel scrolls the panel
        instead; click a control first to use the wheel on it.
        """
        for w in (widget, *widget.findChildren(QWidget)):
            if w.focusPolicy() == Qt.FocusPolicy.WheelFocus:
                w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # no focus by wheel
            w.installEventFilter(self._wheel_guard)

    # -------------------------------------------------------------- handlers
    def _do(self, fn: Callable[[], None], *, refresh: bool = True) -> None:
        """Run a state-changing command, then (if *refresh*) refresh the readouts."""
        self._stop_hold()  # changing the state ends a hold test
        with suppress(Exception):
            fn()
        self._full_read_due = True
        if refresh and self._poll_now_allowed():
            self._poller.poll_now(force=True)

    def _on_idle_ready(self) -> None:
        if self.controller.get_state() == "Idle":
            self._do(self.controller.set_ready)
        else:
            self._do(self.controller.set_idle)

    def _set_led_button(self, on: bool) -> None:
        self.btn_led.blockSignals(True)
        self.btn_led.setChecked(on)
        self.btn_led.blockSignals(False)
        self.btn_led.setText("LED Off" if on else "LED On")

    def _led_write(self, on: bool) -> bool:
        """Light or darken this axis's LED; show the error in the panel if it fails.

        Unlike `_do`, a failure is logged and shown (not swallowed): the LED button
        used to flip to "on" even when nothing was sent. Returns True on success.
        """
        self._stop_hold()  # changing the state ends a hold test
        ok = True
        try:
            self.controller.set_led(on)
        except (RuntimeError, ValueError) as exc:
            ok = False
            logger.warning("CRISP %s: LED write failed: %s", self.controller.label, exc)
            self.led_error_lbl.setText(f"LED command failed: {exc}")
            self.led_error_lbl.show()
        else:
            self.led_error_lbl.clear()
            self.led_error_lbl.hide()
        self._full_read_due = True
        if self._poll_now_allowed():
            self._poller.poll_now(force=True)
        return ok

    def _on_led_clicked(self, checked: bool) -> None:
        ok = self._led_write(checked)
        self._set_led_button(checked if ok else not checked)

    def _on_led_intensity_edited(self, _value: object = None) -> None:
        """A new LED Intensity only lights up once the LED is relit: debounce it."""
        self._led_timer.start()

    def _reapply_led(self) -> None:
        """Relight the LED so a new intensity takes effect (Idle with LED on, Ready).

        Guarded on the cached state (no serial read on the GUI thread); never sent
        while locked, calibrating or dithering, or while a diagnostic tool runs.
        """
        if self._active_tool is not None:
            return
        if should_reapply_led(self._last_state, self.btn_led.isChecked()):
            self._led_write(True)

    def _on_lock(self) -> None:
        if self.controller.get_state() in _LOCKED_STATES:
            self._do(self.controller.set_unlock)
        else:
            self._do(self.controller.set_lock)

    def _on_log_cal_clicked(self) -> None:
        # Log Cal sets a new log-amp gain, which rescales the error: a measured slope
        # no longer applies.
        self._set_measured_slope(None)
        self._do(self.controller.set_log_cal)

    def _on_dither_clicked(self) -> None:
        # Re-aligning the detector changes the slope too.
        self._set_measured_slope(None)
        # The assistant takes over the serial port straight away, so skip the
        # slow poll's refresh.
        self._do(self.controller.set_dither, refresh=False)
        self._start_dither()

    def _on_joystick_toggled(self, checked: bool) -> None:
        """Manual joystick override; independent of the auto lock/unlock logic."""
        with suppress(Exception):
            self.controller.set_joystick_enabled(checked)

    def _on_reset_offset(self) -> None:
        self._do(self.controller.set_reset_offset)
        # The error datum just moved; drop stale fluctuation samples.
        self._error_history.clear()

    # --------------------------------------------------------- poll controls
    def _on_poll_toggled(self, checked: bool) -> None:
        if checked:
            # While a tool runs, the slow poller stays paused; it resumes after.
            if self._poll_may_run():
                self._full_read_due = True
                self._poller.start()
                self._poller.poll_now(force=True)
        else:
            self._poller.stop()

    def _on_interval_changed(self, value: int) -> None:
        self._poller.set_interval(int(value))

    def _on_mda_started(self, *_args) -> None:
        self._mda_running = True
        self._stop_tools()
        with suppress(RuntimeError):
            self._poller.stop()

    def _on_mda_finished(self, *_args) -> None:
        self._mda_running = False
        with suppress(RuntimeError):
            if self.poll_chk.isChecked() and self._poll_may_run():
                self._full_read_due = True
                self._poller.start()

    def hideEvent(self, event: QHideEvent) -> None:
        # Don't keep hammering the serial port for a panel nobody is looking at.
        self._stop_tools()
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_tools()
        self.tools_window.close()
        if self._curve_images is not None:
            self._curve_images.close()
        if self._curve_frames is not None:
            self._curve_frames.close()
        super().closeEvent(event)

    # ------------------------------------------------------------- tool core
    def _poll_may_run(self) -> bool:
        """The slow poll runs only with no tool and no MDA on the line."""
        return poll_may_run(
            active_tool=self._active_tool,
            mda_running=self._mda_running,
        )

    def _poll_now_allowed(self) -> bool:
        """A one-off refresh is read only when no tool has the line."""
        return tool_may_start(active_tool=self._active_tool)

    def _set_readouts_title(self) -> None:
        if self._active_tool is not None:
            self.readouts_box.setTitle(
                f"Readouts  (paused: {_TOOL_TITLES[self._active_tool]})"
            )
        else:
            self.readouts_box.setTitle("Readouts")

    def _begin_tool(self, name: str) -> bool:
        """Claim the serial port for tool *name*; ``False`` if another one has it."""
        if not tool_may_start(active_tool=self._active_tool):
            return False
        self._active_tool = name
        self._set_readouts_title()
        with suppress(RuntimeError):
            self._poller.stop()  # one reader on the serial port at a time
        self._sync_tool_controls()
        return True

    def _end_tool(self, name: str) -> None:
        if self._active_tool != name:
            return
        self._active_tool = None
        self._set_readouts_title()
        self._sync_tool_controls()
        if self.poll_chk.isChecked() and self._poll_may_run():
            self._full_read_due = True
            with suppress(RuntimeError):
                self._poller.start()
                if self.isVisible():
                    self._poller.poll_now(force=True)

    def _stop_tools(self) -> None:
        self._stop_dither()
        self._stop_window_tools()

    def _stop_window_tools(self) -> None:
        """Stop the tools of the Focus curve / Hold test window (not the Dither)."""
        self._stop_hold()
        if self._active_tool == _CURVE:
            # The worker returns the actuator to its start, then reports back.
            self._curve_stop.set()

    def _sync_tool_controls(self) -> None:
        tool = self._active_tool
        curve = tool == _CURVE
        if tool is not None:
            self.tools_running_lbl.setText(f"{_TOOL_TITLES[tool]} running")
        else:
            self.tools_running_lbl.setText("")
        gate = control_gate(
            active_tool=tool,
            crisp_locked=self._last_state in _LOCKED_STATES,
        )
        # The focus curve moves the actuator; nothing else may change state meanwhile.
        for b in (
            self.btn_idle_ready,
            self.btn_lock,
            self.btn_offset,
            self.btn_save,
            self.btn_log,
            self.btn_dither,
            self.btn_gain,
        ):
            b.setEnabled(gate.ops)
        self.btn_led.setEnabled(gate.led)
        for w in (self.joystick_chk, self._param_body):
            w.setEnabled(gate.led_and_params)
        self.dither_watch_btn.blockSignals(True)
        self.dither_watch_btn.setChecked(tool == _DITHER)
        self.dither_watch_btn.blockSignals(False)
        self.dither_watch_btn.setEnabled(gate.dither_watch)
        self.curve_run_btn.setText("Cancel" if curve else "Run focus curve")
        self.curve_run_btn.setEnabled(gate.curve_run)
        self.hold_start_btn.setText("Stop" if tool == _HOLD else "Start hold test")
        self.hold_start_btn.setEnabled(gate.hold_start)
        self.curve_save_btn.setEnabled(self._curve_scan is not None and not curve)
        self.hold_save_btn.setEnabled(bool(self._hold_results) and tool != _HOLD)

    def _emit(self, kind: str, payload: object) -> None:
        """Hand a worker-thread event to the GUI thread (a no-op once destroyed)."""
        with suppress(RuntimeError):
            self._tool_event.emit(kind, payload)

    def _on_tool_event(self, kind: str, payload: object) -> None:
        if kind == "dither_sample":
            self._on_dither_sample(payload)  # type: ignore[arg-type]
        elif kind == "curve_point":
            self._on_curve_point(payload)  # type: ignore[arg-type]
        elif kind == "curve_images":
            self._on_curve_images(payload)  # type: ignore[arg-type]
        elif kind == "curve_done":
            self._on_curve_done(payload)  # type: ignore[arg-type]
        elif kind == "curve_error":
            self._on_curve_error(str(payload))
        elif kind == "hold_sample":
            self._on_hold_sample(payload)  # type: ignore[arg-type]
        elif kind == "hold_done":
            self._on_hold_done(payload)  # type: ignore[arg-type]

    # ------------------------------------------------------- dither assistant
    def _on_dither_section_toggled(self, expanded: bool) -> None:
        """Collapsing the section stops a running assistant (as hiding the panel)."""
        if not expanded:
            self._stop_dither()

    def _on_dither_watch_toggled(self, checked: bool) -> None:
        if checked:
            self._start_dither()
        else:
            self._stop_dither()

    def _start_dither(self) -> None:
        if self._active_tool == _DITHER:
            return
        if not self._begin_tool(_DITHER):
            return
        self._dither_tracker.reset()
        self._dither_outside = 0
        self._dither_last_in = float("-inf")
        self._dither_next_state = 0.0
        self.dither_section.toggle_btn.setChecked(True)  # show it, no pop-up
        label = self.controller.label
        self._dither_sampler = Sampler(
            self._read_dither,
            lambda s: self._emit("dither_sample", s),
            present=lambda: device_present(self.mmcore, label),
            name=f"CRISPy-dither-{label}",
        )
        self._dither_sampler.start()
        self._paint_dither(force=True)

    def _read_dither(self) -> DitherSample | None:
        """One fast read for the Dither assistant (runs on its worker thread)."""
        now = time.monotonic()
        with_state = now >= self._dither_next_state
        if with_state:
            self._dither_next_state = now + _DITHER_STATE_EVERY_S
        return read_dither_sample(
            self.mmcore, self.controller.label, with_state=with_state
        )

    def _stop_dither(self) -> None:
        if self._active_tool != _DITHER:
            return
        if self._dither_sampler is not None:
            self._dither_sampler.stop()
            self._dither_sampler = None
        self._end_tool(_DITHER)
        self._paint_dither(force=True)

    def _on_dither_sample(self, sample: DitherSample) -> None:
        if self._active_tool != _DITHER:  # a straggler queued before it stopped
            return
        if sample.state is not None:
            self._update_state(sample.state)
            if sample.state in DITHER_STATES:
                self._dither_outside = 0
                self._dither_last_in = sample.t
            else:
                if self._dither_outside == 0:
                    # The state is only read every so often: drop what was read
                    # since it last read as dithering (Ready errors near 0).
                    self._dither_tracker.discard_after(self._dither_last_in)
                self._dither_outside += 1
                if self._dither_outside >= _DITHER_OUTSIDE_TO_STOP:
                    self._stop_dither()
                    return
        if self._dither_outside == 0:
            self._dither_tracker.add(sample)
        self._paint_dither()

    def _on_dither_reset(self) -> None:
        self._dither_tracker.reset_peak()
        self._paint_dither(force=True)

    def _paint_dither(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_dither_paint < _DITHER_PAINT_S:
            return
        self._last_dither_paint = now
        active = self._active_tool == _DITHER
        if active:
            self.dither_status.setText(
                "Dithering. Turn the detector lateral adjustment screw slowly and "
                "pause after each turn. Aim for |Err| ≥ 100 (> 200 is plenty) with "
                "Sum in 50-80, then press 3. Set Gain."
            )
        else:
            self.dither_status.setText(
                "Not dithering. After 1. Log Cal, press 2. Dither (or Watch if it "
                "is already dithering)."
            )
        s = self._dither_tracker.stats
        color = _err_color(s.level)
        sign = " (negative)" if s.sign < 0 else ""
        settling = (
            "  settling…" if active and s.level is not None and not s.steady else ""
        )
        self.dither_level_lbl.setText(f"|Err| {_fmt(s.level, '.0f')}{sign}{settling}")
        self.dither_level_lbl.setStyleSheet(
            f"font-weight: bold; font-size: 22px; color: {color};"
        )
        peak = f"peak {_fmt(s.peak, '.0f')}"
        if s.peak_t is not None and active:
            peak += f"  ({max(0.0, now - s.peak_t):.0f} s ago)"
        self.dither_peak_lbl.setText(peak)

        status = s.sum_status
        sum_text = f"Sum {_fmt(s.signal_sum, '.0f')}"
        sum_color = _GREY
        if status == "ok":
            sum_text += f"  (in the {SUM_LOW:.0f}-{SUM_HIGH:.0f} target)"
            sum_color = _GREEN
        elif status == "saturated":
            sum_text += "  — a detector half is saturated: redo Log Cal"
            sum_color = _RED
        elif status in ("low", "high"):
            sum_text += (
                f"  — outside {SUM_LOW:.0f}-{SUM_HIGH:.0f}: redo Log Cal once aligned"
            )
            sum_color = _ORANGE
        self.dither_sum_lbl.setText(sum_text)
        self.dither_sum_lbl.setStyleSheet(f"color: {sum_color};")
        rel = None
        if s.noise is not None and s.level:
            rel = s.noise / s.level * 100
        self.dither_detail_lbl.setText(
            f"raw {_fmt(s.raw_err, '+.0f')}  ·  scatter σ {_fmt(s.noise, '.1f')} counts "
            f"({_fmt(rel, '.0f')}%)  ·  {_fmt(s.rate_hz, '.0f')} reads/s"
        )

        hlines = [
            RefLine(ERR_TARGET, _GREEN, f"{ERR_TARGET:.0f}"),
            RefLine(ERR_PLENTY, _DARK_GREEN, f"{ERR_PLENTY:.0f}"),
        ]
        trace = self._dither_tracker.trace()
        if not trace:
            self.dither_plot.set_data(
                hlines=hlines, x_range=(-30.0, 0.0), y_range=(0.0, 250.0)
            )
            return
        t_end = trace[-1][0]
        xs = [t - t_end for t, _, _ in trace]
        if s.peak is not None:
            hlines.append(
                RefLine(
                    s.peak, _BLUE, f"peak {s.peak:.0f}", dashed=False, label_left=True
                )
            )
        filtered = [(x, lv) for x, (_, _, lv) in zip(xs, trace) if lv is not None]
        span = max(10.0, min(30.0, -xs[0]))  # grow to 30 s, then scroll
        self.dither_plot.set_data(
            [
                Series(xs, [raw for _, raw, _ in trace], "#90a4ae", dots=True),
                Series(
                    [x for x, _ in filtered], [lv for _, lv in filtered], color, 2.0
                ),
            ],
            hlines=hlines,
            x_range=(-span, 0.0),
        )

    # ------------------------------------------------------------ focus curve
    def _confirm_move(self, text: str) -> bool:
        """Ask before moving the actuator (replaced in tests)."""
        answer = QMessageBox.question(
            self,
            "Run focus curve",
            text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Ok

    def _on_curve_run(self) -> None:
        if self._active_tool == _CURVE:
            self._curve_stop.set()
            self.curve_status.setText("Cancelling — returning to the start position…")
            return
        actuator = self.controller.actuator_label
        if actuator is None:
            self.curve_status.setText(
                "No paired focus actuator was found for this CRISP axis."
            )
            return
        state = self.controller.get_state()
        if state != "Ready":
            self.curve_status.setText(
                f"The focus curve needs the Ready state (LED on, unlocked); the "
                f"state is {state or 'unknown'}."
            )
            return
        half, step = self.curve_range.value(), self.curve_step.value()
        n = len(scan_offsets(half, step))
        snap = self.curve_snap.isChecked()
        if snap and snap_provider() is None and self.mmcore.isSequenceRunning():
            self.curve_status.setText("Stop Live first: the scan snaps its own images.")
            return
        also = " and snap an image at each" if snap else ""
        if not self._confirm_move(
            f"Move {actuator} from {-half:g} to +{half:g} µm around its current "
            f"position in {step:g} µm steps ({n} positions){also}, then return it to "
            "the start?\n\nThe scan stays inside the actuator's travel limits and "
            "leaves out positions it can't reach. Don't touch the focus wheel while "
            "it runs."
        ):
            return
        if not self._begin_tool(_CURVE):
            return
        self._curve_stop.clear()
        self._curve_points = []
        self._curve_analysis = None
        if self._curve_frames is not None:
            self._curve_frames.close()
        self._curve_frames = FrameStore(n) if snap else None
        self._curve_frame_z = []
        self.curve_images_box.setVisible(False)
        self.curve_sharp_plot.clear()
        if snap:
            window = self._images_window()
            window.reset()
            window.show()
            window.raise_()
        self.tools_tabs.setCurrentWidget(self._curve_tab)
        self.curve_status.setStyleSheet("")
        self.curve_status.setText(f"Scanning {n} positions…")
        self.curve_result_lbl.setText("")
        self._paint_curve()
        threading.Thread(
            target=self._curve_worker,
            args=(
                actuator,
                half,
                step,
                self.curve_settle.value() / 1000.0,
                self.curve_reads.value(),
                snap,
            ),
            name=f"CRISPy-curve-{self.controller.label}",
            daemon=True,
        ).start()

    def _curve_worker(
        self,
        actuator: str,
        half: float,
        step: float,
        settle: float,
        reads: int,
        snap: bool,
    ) -> None:
        store = self._curve_frames
        count = itertools.count()

        def keep_frames(point: CurvePoint, frames: Mapping[str, np.ndarray]) -> None:
            index = next(count)
            if store is not None:
                store.put(index, frames)  # full size, to disk, on this thread
            self._emit("curve_images", (index, point.z_um, tuple(frames)))

        def snap_on_gui_thread() -> Mapping[str, np.ndarray]:
            frames = self._gui_call.call(
                lambda: snap_images(self.mmcore), _SNAP_TIMEOUT_S
            )
            return cast("Mapping[str, np.ndarray]", frames)

        try:
            scan = run_focus_curve(
                self.mmcore,
                self.controller.label,
                actuator,
                half_range_um=half,
                step_um=step,
                settle_s=settle,
                reads_per_point=reads,
                should_stop=self._curve_stop.is_set,
                on_point=lambda p: self._emit("curve_point", p),
                snap=snap_on_gui_thread if snap else None,
                on_images=keep_frames,
            )
        except Exception as e:  # noqa: BLE001 - reported in the panel
            self._emit("curve_error", str(e))
            return
        self._emit("curve_done", scan)

    def _on_curve_point(self, point: CurvePoint) -> None:
        if self._active_tool != _CURVE:
            return
        self._curve_points.append(point)
        self.curve_status.setText(
            f"Scanning… {len(self._curve_points)} positions, at {point.z_um:+.2f} µm"
        )
        self._paint_curve()

    def _images_window(self) -> _CurveImagesWindow:
        if self._curve_images is None:
            self._curve_images = _CurveImagesWindow(
                f"Focus curve images: {self.controller.label}",
                self._curve_frame_at,
                self,
            )
        return self._curve_images

    def _curve_frame_at(self, index: int) -> tuple[float, dict[str, np.ndarray]]:
        store = self._curve_frames
        z = self._curve_frame_z[index] if index < len(self._curve_frame_z) else 0.0
        if store is None:
            return z, {}
        frames = {}
        for camera in store.cameras:
            frame = store.get(index, camera)
            if frame is not None:
                frames[camera] = frame
        return z, frames

    def _on_curve_images(self, payload: tuple[int, float, tuple[str, ...]]) -> None:
        if self._active_tool != _CURVE:
            return
        index, z, cameras = payload
        self._curve_frame_z.append(z)
        for camera in cameras:
            if self.curve_camera.findText(camera) < 0:
                self.curve_camera.addItem(camera)
        self.curve_images_box.setVisible(True)
        window = self._images_window()
        window.set_count(len(self._curve_frame_z))
        window.show_index(index)  # follow the scan

    def _on_curve_show_images(self) -> None:
        window = self._images_window()
        window.show()
        window.raise_()
        window.activateWindow()

    def _on_curve_camera(self) -> None:
        self._paint_curve()
        if self._curve_analysis is not None and self._active_tool != _CURVE:
            self.curve_result_lbl.setText(self._curve_summary(self._curve_analysis))

    def _on_curve_done(self, scan: CurveScan) -> None:
        self._curve_scan = scan
        self._curve_points = list(scan.points)
        self._curve_analysis = analyze_focus_curve(scan.points)
        done = "Done" if scan.completed else "Cancelled"
        self.curve_status.setText(
            f"{done}: {len(scan.points)} positions. {scan.actuator} is back at "
            f"{scan.z0_um:.3f} µm."
        )
        self._end_tool(_CURVE)
        self._paint_curve()
        self.curve_result_lbl.setText(self._curve_summary(self._curve_analysis))
        if scan.completed and self._curve_analysis.slope:
            self._set_measured_slope(self._curve_analysis.slope)
        sharp = sharpest_position(scan.points, self.curve_camera.currentText())
        if sharp is not None and self._curve_frame_z:
            # Show the sharpest image to start with.
            nearest = min(
                range(len(self._curve_frame_z)),
                key=lambda i: abs(self._curve_frame_z[i] - sharp[0]),
            )
            self._images_window().show_index(nearest)

    def _on_curve_error(self, message: str) -> None:
        self.curve_status.setText(message)
        self.curve_status.setStyleSheet(f"color: {_RED};")
        self._end_tool(_CURVE)

    def _paint_curve(self) -> None:
        pts = sorted(self._curve_points, key=lambda p: p.z_um)
        if not pts:
            self.curve_plot.clear()
            return
        a = self._curve_analysis
        vlines = [RefLine(0.0, _GREY, "start", label_bottom=True)]
        markers: list[tuple[float, float, str]] = []
        if a is not None and a.crossing_um is not None:
            vlines.append(RefLine(a.crossing_um, _BLUE, "lock point", dashed=False))
            vlines.extend(
                RefLine(z, _ORANGE, "other crossing") for z, _ in a.other_crossings
            )
            for pk in (a.peak_low, a.peak_high):
                if pk is not None:
                    markers.append((pk[0], pk[1], _ORANGE))
        camera = self.curve_camera.currentText()
        sharp = sharpest_position(pts, camera) if a is not None and camera else None
        if sharp is not None:
            vlines.append(RefLine(sharp[0], _GREEN))
        self._paint_sharpness(pts, camera, a, sharp)
        scan = self._curve_scan
        if a is not None and scan is not None and scan.limits_um is not None:
            for cut, limit in zip(
                (scan.limited_low, scan.limited_high), scan.limits_um
            ):
                if cut:
                    vlines.append(
                        RefLine(
                            limit - scan.z0_um, _RED, "travel limit", label_bottom=True
                        )
                    )
        self.curve_plot.set_data(
            [Series([p.z_um for p in pts], [p.error for p in pts], _BLUE, 1.8)],
            hlines=[RefLine(0.0, _GREY)],
            vlines=vlines,
            markers=markers,
        )

    def _paint_sharpness(
        self,
        pts: list[CurvePoint],
        camera: str,
        a: CurveAnalysis | None,
        sharp: tuple[float, bool] | None,
    ) -> None:
        scored = [(p.z_um, p.scores[camera]) for p in pts if camera in p.scores]
        if not scored:
            self.curve_sharp_plot.clear()
            return
        top = max(score for _, score in scored) or 1.0
        vlines = []
        if sharp is not None:
            vlines.append(RefLine(sharp[0], _GREEN, "sharpest", label_bottom=True))
        if a is not None and a.crossing_um is not None:
            vlines.append(RefLine(a.crossing_um, _BLUE, "lock point", dashed=False))
        self.curve_sharp_plot.set_data(
            [Series([z for z, _ in scored], [v / top for _, v in scored], _GREEN, 1.6)],
            vlines=vlines,
            x_range=(pts[0].z_um, pts[-1].z_um),
        )

    def _curve_sharpness_text(self, a: CurveAnalysis) -> str | None:
        camera = self.curve_camera.currentText()
        sharp = sharpest_position(self._curve_points, camera) if camera else None
        if sharp is None:
            return None
        z, inside = sharp
        if not inside:
            end = "upper" if z > 0 else "lower"
            return (
                f"<b>Image sharpness</b> ({camera}) is highest at the {end} end of the "
                f"scan ({z:+.1f} µm): the sharpest image is beyond it."
            )
        text = f"<b>Sharpest image</b> ({camera}, green line) at {z:+.2f} µm"
        if a.crossing_um is None:
            return text + " from the start."
        d = z - a.crossing_um
        text += f", {d:+.2f} µm from the lock point"
        if abs(d) >= _SHARPEST_OFFSET_UM:
            text += (
                ". To lock at the sharpest image, move there in Ready and Set Offset"
            )
        return text + "."

    def _curve_summary(self, a: CurveAnalysis) -> str:
        if a.crossing_um is None:
            return (
                "No zero crossing in the scanned range: the lock point is outside it, "
                "or there is no usable signal. Check Sum and the Dither alignment."
            )
        lines = [
            (
                f"<b>Lock point</b> at {a.crossing_um:+.2f} µm from the start: where "
                "CRISP would hold focus with the current lock offset. (Set Offset in "
                "Ready at best focus moves it to 0.)"
            )
        ]
        sharpness = self._curve_sharpness_text(a)
        if sharpness:
            lines.append(sharpness)
        res = a.resolution_nm
        slope = f"<b>Slope</b> {_fmt(a.slope, '+.1f')} counts/µm of the error readout"
        if res is not None:
            slope += f": one count = {res:.0f} nm of focus"
        lines.append(slope + ".")
        cal = self._sensitivity
        if cal and a.slope:
            ratio = cal / abs(a.slope)
            lines.append(
                f"Cal Gain / Cal Range ({cal:.0f}) is {ratio:.0f}× this: it is on a "
                "different (internal) scale, so it can't convert the error readout "
                "to µm. The panel uses this measured slope instead, until the next "
                "Log Cal or Dither."
            )
        lo, hi = a.margin_low_um, a.margin_high_um
        z_lo = min(p.z_um for p in self._curve_points) if self._curve_points else None
        z_hi = max(p.z_um for p in self._curve_points) if self._curve_points else None
        found = [
            f"{m:.2f} µm {side}"
            for m, side in ((lo, "below"), (hi, "above"))
            if m is not None
        ]
        if found:
            lines.append(
                f"<b>Peak{'s' if len(found) > 1 else ''}</b> {' and '.join(found)} the "
                "lock point (a lock point close to a peak is hard to hold)."
            )
        missing = [
            side
            for side, pk in (("below", a.peak_low), ("above", a.peak_high))
            if pk is None
        ]
        scan = self._curve_scan
        cut = {
            "below": scan is not None and scan.limited_low,
            "above": scan is not None and scan.limited_high,
        }
        if missing:
            span = ""
            if z_lo is not None and z_hi is not None:
                span = f" ({z_lo:+.1f} … {z_hi:+.1f} µm)"
            text = (
                f"<b>No turnover {' or '.join(missing)} the lock point within the "
                f"scan</b>{span}: the error levels off or keeps growing to the end."
            )
            if not found and not all(cut[side] for side in missing):
                text += " Scan wider (if safe) to find the peaks."
            lines.append(text)
        if scan is not None and z_lo is not None and z_hi is not None:
            reached = [
                f"{abs(z):.1f} µm {side}"
                for side, z in (("below", z_lo), ("above", z_hi))
                if cut[side]
            ]
            if reached:
                text = (
                    f"<b>Travel limit:</b> {scan.actuator} could only go "
                    f"{' and '.join(reached)} the start"
                )
                if scan.limits_um is not None:
                    lo_lim, hi_lim = scan.limits_um
                    text += (
                        f" (its limits are {lo_lim:.1f} … {hi_lim:.1f} µm; it started "
                        f"at {scan.z0_um:.1f} µm)"
                    )
                if scan.skipped:
                    text += (
                        f"; {scan.skipped} position{'s' if scan.skipped > 1 else ''} "
                        "it didn't reach were left out"
                    )
                lines.append(text + ". The curve past that wasn't measured.")
        for z, s in a.other_crossings:
            if s is None or not a.slope:
                continue
            steep = abs(s) / abs(a.slope)
            if (s > 0) != (a.slope > 0):
                meaning = (
                    "opposite sign: probably another reflecting surface coming into "
                    "focus. Past it the error pushes the wrong way, so the capture "
                    "range ends there"
                )
            else:
                meaning = "same sign: a second point the lock could settle on"
            lines.append(
                f"<b>Another zero crossing</b> at {z:+.1f} µm, slope {s:+.0f} counts/µm "
                f"({steep:.1f}× as steep as the lock point; {meaning})."
            )
        cap_lo = _fmt(a.capture_low_um, "+.2f")
        cap_hi = _fmt(a.capture_high_um, "+.2f")
        lines.append(
            f"<b>Capture range</b> {'≤ ' if a.capture_low_open else ''}{cap_lo} … "
            f"{'≥ ' if a.capture_high_open else ''}{cap_hi} µm."
        )
        lines.append(self._curve_noise_text(a))
        return "<br>".join(lines)

    @staticmethod
    def _curve_noise_text(a: CurveAnalysis) -> str:
        reads = ""
        if a.noise_counts is not None:
            reads = f"; the reads at one position agreed to ±{a.noise_counts:.1f}"
        if a.scatter_counts is not None and a.scatter_counts > 1.0 and a.scatter_nm:
            return (
                f"<b>Error noise</b> ±{a.scatter_counts:.1f} counts rms from one "
                f"position to the next, ≈ {a.scatter_nm:.0f} nm of focus at this "
                f"slope{reads}."
            )
        biggest = max(
            (n for n in (a.scatter_counts, a.noise_counts) if n is not None),
            default=None,
        )
        if biggest is None:
            return "Use 2 or more reads per position to estimate the noise floor."
        if biggest <= 1.0:
            one = (
                f" (one count = {a.resolution_nm:.0f} nm here)"
                if a.resolution_nm
                else ""
            )
            return (
                "<b>Error noise</b> about one count or less, i.e. mostly the readout's "
                f"rounding: it can't show focus changes smaller than one count{one}. "
                "The Hold test (actuator position) measures the real noise."
            )
        floor = (
            f" → focus noise floor ≈ {a.noise_floor_nm:.0f} nm rms (noise / slope)"
            if a.noise_floor_nm is not None
            else ""
        )
        return f"<b>Error noise</b> ±{biggest:.1f} counts{floor}."

    def _on_curve_save(self) -> None:
        if self._curve_scan is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save focus curve",
            str(self._default_csv_path("curve")),
            "CSV (*.csv)",
        )
        if path:
            self.save_curve_csv(path)

    def save_curve_csv(self, path: str | Path) -> None:
        """Write the last focus curve to *path*."""
        if self._curve_scan is None:
            return
        meta = {
            "crisp": self.controller.label,
            "saved": datetime.now().astimezone().isoformat(),
        }
        a = self._curve_analysis
        if a is not None:
            meta.update(
                crossing_um=_fmt(a.crossing_um, ".4f", ""),
                slope_counts_per_um=_fmt(a.slope, ".2f", ""),
                noise_floor_nm=_fmt(a.noise_floor_nm, ".1f", ""),
            )
        meta.update({k: v for k, v in self._last_readouts.items()})
        write_curve_csv(path, self._curve_scan, meta)
        self.curve_status.setText(f"Saved {Path(path).name}")
        self.curve_status.setToolTip(str(path))

    def _default_csv_path(self, kind: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.controller.label)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        return Path.home() / f"crisp_{kind}_{safe}_{stamp}.csv"

    # -------------------------------------------------------------- hold test
    def _on_hold_start(self) -> None:
        if self._active_tool == _HOLD:
            self._stop_hold()
            return
        actuator = self.controller.actuator_label
        if actuator is None:
            self.hold_status.setText(
                "No paired focus actuator was found for this CRISP axis."
            )
            return
        state = self.controller.get_state()
        if state in _LOCKED_STATES:
            locked = True
        elif state in _UNLOCKED_HOLD_STATES:
            locked = False  # the actuator by itself, not driven by CRISP
        else:
            self.hold_status.setText(
                f"The hold test runs locked (Lock / In Focus) or unlocked (Ready / "
                f"Idle); the state is {state or 'unknown'}."
            )
            return
        if not self._begin_tool(_HOLD):
            return
        self._hold_samples = []
        self.tools_tabs.setCurrentWidget(self._hold_tab)
        duration = float(self.hold_duration.value())
        mode = "locked" if locked else "unlocked: the actuator by itself"
        self.hold_status.setText(f"Recording for {duration:.0f} s ({mode})…")
        self.hold_plot.clear()
        self._hold = HoldTest(
            self.mmcore,
            self.controller.label,
            actuator,
            duration_s=duration,
            rate_hz=self.hold_rate.value(),
            locked=locked,
            on_sample=lambda s: self._emit("hold_sample", s),
            on_done=lambda r: self._emit("hold_done", r),
        )
        self._hold.start()

    def _stop_hold(self) -> None:
        if self._active_tool != _HOLD or self._hold is None:
            return
        # stop() waits for the worker (one read at most); the partial result still
        # arrives as "hold_done". Free the port now, so a command that stopped the
        # test (e.g. 2. Dither) can start its own tool straight away.
        self._hold.stop()
        self._end_tool(_HOLD)

    def _on_hold_sample(self, sample: HoldSample) -> None:
        if self._active_tool != _HOLD:
            return
        self._hold_samples.append(sample)
        if sample.state is not None:
            self._update_state(sample.state)
        now = time.monotonic()
        if now - self._last_hold_paint >= _HOLD_PAINT_S:
            self._last_hold_paint = now
            self.hold_status.setText(
                f"Recording… {sample.t:.0f} / {self.hold_duration.value()} s"
            )
            locked = self._hold.locked if self._hold is not None else True
            self._paint_hold(self._hold_samples, locked=locked)

    def _paint_hold(self, samples: list[HoldSample], *, locked: bool) -> None:
        """Plot the position over time, and for an unlocked run the error too.

        Unlocked, the actuator stands still, so the error is how the focus moves
        without the lock: with a measured slope it goes on the position's nm axis as the
        move a lock would make to cancel it (-error / slope), otherwise in counts. A
        locked run's error is in-loop (mostly sensor noise the lock averages out), so
        drawn as focus it would look like motion that isn't there; only the position.
        """
        pos = [(s.t, s.position_um) for s in samples if s.position_um is not None]
        err = [] if locked else [(s.t, s.error) for s in samples if s.error is not None]
        slope = self._measured_slope
        series: list[Series] = []
        label = "position (nm)"
        if slope and err:
            label = "nm: position (blue), error ÷ slope (orange)"
        elif err:
            label = "error (counts); run a Focus curve to see it in nm"
            pos = []
        if pos:
            z_first = pos[0][1]
            series.append(
                Series(
                    [t for t, _ in pos],
                    [(z - z_first) * 1000.0 for _, z in pos],  # type: ignore[operator]
                    _BLUE,
                    1.4,
                )
            )
        if err:
            scale = -1000.0 / slope if slope else 1.0
            series.append(
                Series(
                    [t for t, _ in err],
                    [e * scale for _, e in err],  # type: ignore[operator]
                    _ORANGE,
                    1.0,
                )
            )
        if not series:
            self.hold_plot.clear()
            return
        self.hold_plot.set_y_label(label)
        self.hold_plot.set_data(series, hlines=[RefLine(0.0, _GREY)])

    def _on_hold_done(self, result: HoldResult) -> None:
        self._hold = None
        self._hold_results.insert(0, result)
        del self._hold_results[_HOLD_RESULTS_KEPT:]
        self._end_tool(_HOLD)
        self._paint_hold(result.samples, locked=result.locked)
        self._fill_hold_table()
        s = result.stats
        mode = "Locked" if result.locked else "Unlocked"
        summary = (
            f"{mode}, {'done' if result.completed else result.note}: "
            f"{s.duration_s:.0f} s, {_fmt(s.rate_hz, '.0f')} samples/s. "
            f"Drift {_fmt(s.drift_nm_per_min, '+.1f')} nm/min, jitter "
            f"{_fmt(s.jitter_nm, '.0f')} nm, wander {_fmt(s.wander_nm, '.0f')} nm rms, "
            f"pk-pk {_fmt(s.pkpk_nm, '.0f')} nm."
        )
        if s.rms_nm is None:
            summary += f" No position readings from {result.actuator}."
        if not result.locked:
            summary += (
                " CRISP wasn't driving the actuator, so this is its own motion"
                " (piezo + drive)."
            )
            sens = self._measured_slope
            if s.error_rms is not None and sens:
                summary += (
                    f" The error's {s.error_rms:.1f} counts rms (about its trend) is"
                    " the sensor and optics noise: ≈"
                    f" {s.error_rms / abs(sens) * 1000:.0f} nm at the measured slope."
                )
            if s.error_drift_per_min is not None and sens:
                follow = -s.error_drift_per_min / sens * 1000
                summary += (
                    f" The error drifted {s.error_drift_per_min:+.1f} counts/min: a lock"
                    f" would move the actuator ≈ {follow:+.0f} nm/min to follow it."
                )
            elif s.error_rms is not None and s.error_drift_per_min is not None:
                summary += (
                    f" The error scattered {s.error_rms:.1f} counts rms about its"
                    f" trend and drifted {s.error_drift_per_min:+.1f} counts/min (run a"
                    " Focus curve to see these in nm)."
                )
        else:
            sens = self._measured_slope
            if s.error_rms is not None and sens and s.rms_nm is not None:
                summary += (
                    f" The in-loop error was {s.error_rms:.1f} counts rms (≈"
                    f" {s.error_rms / abs(sens) * 1000:.0f} nm if it were focus); the"
                    f" actuator moved {s.rms_nm:.0f} nm rms, so most of that is sensor"
                    " noise the lock averages out."
                )
            if s.drift_nm_per_min is not None and abs(s.drift_nm_per_min) >= 200:
                summary += (
                    " A steady drift while locked is the lock following something: if"
                    " the image stays sharp, the focus itself drifted (mechanics,"
                    " temperature); if it blurs, CRISP's reference drifted."
                )
        change = result.lock_offset_change
        if change:
            summary += (
                f" The lock offset changed by {change:+.0f} during the run (the focus "
                "wheel moves it while locked)."
            )
        self.hold_status.setText(summary)

    def _fill_hold_table(self) -> None:
        self.hold_table.setRowCount(len(self._hold_results))
        for row, r in enumerate(self._hold_results):
            s = r.stats
            note = r.note
            change = r.lock_offset_change
            if change:
                note = f"{note + '; ' if note else ''}offset {change:+.0f}"
            cells = [
                r.started.strftime("%H:%M:%S"),
                "locked" if r.locked else "unlocked",
                _fmt(s.drift_nm_per_min, "+.1f", ""),
                _fmt(s.jitter_nm, ".0f", ""),
                _fmt(s.wander_nm, ".0f", ""),
                _fmt(s.pkpk_nm, ".0f", ""),
                _fmt(s.error_rms, ".1f", ""),
                _fmt(s.error_drift_per_min, "+.1f", ""),
                r.settings.get("Averages", ""),
                r.settings.get("Loop Gain", ""),
                r.settings.get("Update rate (ms)", ""),
                r.settings.get("LED (%)", ""),
                note,
            ]
            for col, text in enumerate(cells):
                self.hold_table.setItem(row, col, QTableWidgetItem(text))
        self.hold_table.resizeColumnsToContents()
        self._sync_tool_controls()

    def _on_hold_save(self) -> None:
        if not self._hold_results:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save hold test", str(self._default_csv_path("hold")), "CSV (*.csv)"
        )
        if path:
            self.save_hold_csv(path)

    def save_hold_csv(self, path: str | Path) -> None:
        """Write the newest hold test to *path*."""
        if self._hold_results:
            write_hold_csv(path, self._hold_results[0])
            self.hold_status.setText(f"Saved {Path(path).name}")
            self.hold_status.setToolTip(str(path))

    # ------------------------------------------------------------- telemetry
    def _read_device_values(self) -> CrispSnapshot:
        """Read CRISP telemetry from the controller (runs on a worker thread).

        Performs only ``mmcore`` / ``controller`` access and returns a plain
        snapshot; it must not touch any Qt widget or mutate GUI-owned state (that
        is done in :meth:`_apply_values` on the GUI thread).
        """
        label = self.controller.label
        # The device can vanish out from under this worker thread: on app close
        # the core unloads devices on another thread, and a config reload rebuilds
        # the panels. Bail before any core access so we never setProperty/
        # getProperty a missing label (pymmcore-plus logs "No device with label
        # ..." warnings, which no suppress() around the call can catch).
        if label not in self.mmcore.getLoadedDevices():
            return CrispSnapshot(state="", readouts={})

        state = read_str(self.mmcore, label, "CRISP State") or ""
        now = time.monotonic()
        full = (
            self._full_read_due
            or state != self._poll_last_state
            or now - self._poll_last_full >= _FULL_READ_EVERY_S
        )
        if full:
            # Cleared before reading, so a request made during the read isn't lost.
            self._full_read_due = False
            self._poll_last_full = now
        self._poll_last_state = state

        readouts: dict[str, str] = {}
        for prop in _READOUT_PROPS if full else _FAST_PROPS:
            value = read_str(self.mmcore, label, prop)
            if value is not None:
                readouts[prop] = value
        return CrispSnapshot(state=state, readouts=readouts, full=full)

    def _apply_values(self, snap: CrispSnapshot) -> None:
        """Apply a telemetry *snap* to the widgets (runs on the GUI thread)."""
        if self._active_tool is not None:
            return  # a read that was in flight when a tool took over
        self._update_state(snap.state)
        if snap.full:
            # Calibration Gain / Range are among the full readouts, so derive the
            # sensitivity from them rather than reading both again.
            self._update_sensitivity(
                counts_per_um(
                    _to_float(snap.readouts.get(CAL_GAIN_PROP)),
                    _to_float(snap.readouts.get(CAL_RANGE_PROP)),
                )
            )
        self._last_readouts.update(snap.readouts)
        for prop in _READOUT_PROPS:
            if prop in snap.readouts:
                self._update_readout(prop, snap.readouts[prop])
        self._update_stability()
        if snap.state == "Dither" and self.isVisible() and not self._mda_running:
            self._start_dither()

    def _update_sensitivity(self, new: float | None) -> None:
        if not _close(new, self._sensitivity):
            # First read or a recalibration: prior samples are in a different
            # scale, so start the fluctuation window fresh.
            self._error_history.clear()
        self._sensitivity = new
        self._render_sensitivity()

    def _update_readout(self, prop: str, value: str) -> None:
        lbl = self._readouts.get(prop)
        if lbl is None:
            return
        if prop == CAL_RANGE_PROP:
            # Shown with the NA it corresponds to (cal range = 1.5 µm / NA²), so a
            # setting that doesn't match the objective this axis moves stands out.
            rng = _to_float(value)
            if rng and rng > 0:
                lbl.setText(f"{rng:.3f}  (NA {math.sqrt(1.5 / rng):.2f})")
            else:
                lbl.setText(str(value))
        elif prop == "Signal Noise Ratio":
            snr = _to_float(value)
            if snr is None:
                lbl.setText(str(value))
                return
            lbl.setText(f"{snr:.2f} dB")
            color = _GREEN if snr >= _SNR_GOOD_DB else _RED
            lbl.setStyleSheet(f"font-weight: bold; color: {color};")
        elif prop == _SUM_PROP:
            lbl.setText(str(value))
            status = sum_status(_to_float(value))
            color = {"ok": _GREEN, "saturated": _RED, "low": _ORANGE, "high": _ORANGE}
            lbl.setStyleSheet(
                f"font-weight: bold; color: {color.get(status or '', _GREY)};"
            )
        elif prop == _ERROR_PROP:
            self._update_error_readout(lbl, value)
        else:
            lbl.setText(str(value))

    def _update_error_readout(self, lbl: QLabel, value: str) -> None:
        counts = _to_float(value)
        if counts is None:
            lbl.setText(str(value))
            return
        # Only the locked error is a focus error; in Dither it is the dither swing.
        if self._last_state in _LOCKED_STATES:
            self._error_history.append(counts)
        text = f"{counts:g}"
        um = self._to_um(counts)
        if um is not None and self._last_state not in DITHER_STATES:
            text += f"  ({um:+.3f} µm)"
        lbl.setText(text)

    def _update_stability(self) -> None:
        if self._last_state not in _LOCKED_STATES:
            self.stability_lbl.setText("In-loop error: -- (lock to measure)")
            return
        if len(self._error_history) < 2:
            self.stability_lbl.setText("In-loop error: collecting…")
            return
        std = statistics.pstdev(self._error_history)
        ptp = max(self._error_history) - min(self._error_history)
        std_um = self._to_um(std)
        text = f"In-loop error: ± {std:.1f} cts"
        if std_um is not None:
            nm = abs(std_um) * 1000
            text += f" ({nm:.1f} nm)" if nm < 10 else f" ({nm:.0f} nm)"
        self.stability_lbl.setText(f"{text} rms · pk-pk {ptp:.0f} cts")

    def _update_state(self, state: str) -> None:
        previous_state = self._last_state
        if state != self._last_state:
            # Errors from different states aren't comparable (Dither shows its swing).
            self._error_history.clear()
            self._last_state = state
        if state in _BUSY_STATES:
            text, color = _BUSY_STATES[state], _ORANGE
        elif state in _GOOD_STATES:
            text, color = state, _GREEN
        elif state in _BAD_STATES:
            text, color = state, _RED
        else:
            text, color = (state or "--"), _GREY
        self.state_lbl.setText(f"State: {text}")
        self.state_lbl.setStyleSheet(f"font-weight: bold; color: {color};")

        # Reflect live state on the toggle buttons.
        led_on = bool(state) and state != "Idle"
        self.btn_idle_ready.setText("Set Idle (LED off)" if led_on else "Set Ready")
        locked = state in _LOCKED_STATES
        # A direct LED-on leaves the state at "Idle", so there the button keeps the
        # user's choice; entering or leaving Idle resets it (every other state is lit).
        if state and state != "Idle":
            self._set_led_button(True)
        elif state == "Idle" and previous_state != "Idle":
            self._set_led_button(False)
        self.btn_led.setEnabled(
            control_gate(
                active_tool=self._active_tool,
                crisp_locked=locked,
            ).led
        )
        self.btn_lock.setText("Unlock" if locked else "Lock")

        # Auto joystick lockout: only act on a lock/unlock *transition*, so a
        # manual override via the checkbox isn't immediately fought while the
        # lock state hasn't actually changed.
        if locked != self._was_locked:
            self._was_locked = locked
            self._set_joystick_for_lock(locked)

    def _set_joystick_for_lock(self, locked: bool) -> None:
        enabled = not locked
        with suppress(Exception):
            self.controller.set_joystick_enabled(enabled)
        self.joystick_chk.blockSignals(True)
        self.joystick_chk.setChecked(enabled)
        self.joystick_chk.blockSignals(False)
