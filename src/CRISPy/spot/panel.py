"""Spot focus lock panel (the only O3 focus-lock mode, hosted directly in the dock).

The lock (see :mod:`~CRISPy.spot.spot_focus_service`) watches one
or two straight edges of the CRISP LED patch on a side camera in a worker
process and holds piezo P by nudging it rarely. This panel

- lets the user draw the edge box across the edge that moves with focus (in the
  live view; ONE edge: a saved second box is kept but never used),
  calibrates it (**Calibrate**: a piezo sweep around the present P, on a
  background thread), makes the present edge position the zero (**Zero here**)
  and sets a focus **Offset**;
- locks and unlocks the loop (**Lock** / **Stop**; it runs until Stop,
  independent of any MDA; Lock with no valid calibration calibrates first);
- runs the camera: SDK folder, Preview (which streams into the full-resolution
  :class:`~CRISPy.spot.live_view.SpotLiveView`, docked in the panel's layout or
  popped out into its own window), exposure, gain, channel and ROI, all applied
  live;
- shows a strip chart of the focus error and the commanded piezo over the last
  five minutes, and move statistics;
- lights the CRISP LED (LED button, live LED %; Preview turns it on) and edits the
  loop parameters.

The panel is one plain column: control row, offset / tracking error, ONE message
line (:func:`CRISPy.piezo_status.message`), the live image, toolbar rows, the frame
statistics and four section buttons (Camera, Calibration settings, Diagnostics,
Advanced; one open at a time). A host puts the status words above it from
:meth:`SpotFocusPanel.status_inputs` (re-read on ``statusChanged``).

Everything is written to the settings store's ``spot_focus`` settings
(:mod:`~CRISPy.spot.store`). The service is created
lazily on Preview, Lock, Calibrate or Zero and shut down with the panel or the app.
Its signals come from the reader thread, so each goes through a GUI-thread
:class:`QObject` signal (Qt queues the call).
"""

from __future__ import annotations

import html
import itertools
import logging
import math
import threading
import time
import weakref
from collections import deque
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qtpy.QtCore import (
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from qtpy.QtGui import QColor, QPainter, QPen
from qtpy.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import piezo_status
from .live_view import SpotLiveView
from .spot_focus import (
    EDGE_DETECTOR_LABELS,
    EDGE_DETECTORS,
    SIGMA_PX_RANGE,
    detector_mismatch,
    edge_noise_um,
    snap_box,
)
from .spot_focus_service import SpotFocusService
from .store import get_settings_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from .spot_focus import EdgeCalibration, FocusCalibration

logger = logging.getLogger(__name__)

ALERT_COLOR = "#d0342c"
ATTENTION_COLOR = "#e08a00"
OK_COLOR = "#2e9e44"
#: Per-edge noise (um of P) below which an edge is good (green) ...
NOISE_GOOD_UM = 0.03
#: ... and below which it is usable (attention); above that it is red.
NOISE_WARN_UM = 0.06
#: A box must be this many edge widths long across the edge, per detector.
_BOX_WIDTH_FACTOR = {"halfmax": 4.0, "gaussderiv": 2.0}
#: Shown when Calibrate or Lock has no edge box to measure.
NEED_BOX_TEXT = "Draw an edge box across the edge that moves with focus"

#: Strip chart history, seconds.
CHART_WINDOW_S = 300.0
#: Delay before writing edits to disk; they apply in memory immediately.
_FLUSH_DELAY_MS = 500
#: Readout refresh period (move statistics), ms.
_STATS_PERIOD_MS = 1000
#: How long Preview waits for the camera to open, s.
_OPEN_TIMEOUT_S = 30.0
#: LED intensity, %, when the settings hold none.
DEFAULT_LED_PCT = 50.0
_NO_CRISP = "no CRISP device (no CRISP label)"
#: Smallest height of the docked live view, px.
_VIEW_MIN_H = 240
#: Frame statistics line before the first frame.
_FRAME_STATS_IDLE = "Frame rate: -   Maximum: -   Saturated pixels: -"
#: Telemetry readouts that are not shown (two-edge only, or the tracking error).
_HIDDEN_READOUTS = frozenset({"edge2", "disagreement", "error"})
#: Loop parameters that are not shown (two-edge only); the spin stays for load / save.
_HIDDEN_PARAMS = frozenset({"max_edge_disagreement_um"})

# (settings field, label, min, max, step, decimals, suffix, tooltip)
_PARAMS: tuple[tuple[str, str, float, float, float, int, str, str], ...] = (
    (
        "deadband_um",
        "Deadband",
        0.0,
        5.0,
        0.01,
        3,
        " µm",
        "Focus errors up to this size are left alone",
    ),
    ("kp", "Kp", 0.0, 5.0, 0.05, 2, "", "Proportional gain"),
    ("ki", "Ki", 0.0, 2.0, 0.01, 3, "", "Integral gain"),
    (
        "max_step_um",
        "Max step",
        0.01,
        5.0,
        0.05,
        2,
        " µm",
        "Largest piezo move in one correction",
    ),
    (
        "max_rate_hz",
        "Max rate",
        0.1,
        20.0,
        0.5,
        1,
        " Hz",
        "Most piezo moves per second",
    ),
    (
        "min_contrast",
        "Min contrast",
        1.0,
        4000.0,
        1.0,
        0,
        " counts",
        "Frames whose edge is weaker than this (bright minus dark side, raw "
        "counts) are skipped",
    ),
    (
        "min_valid_fraction",
        "Min valid rows",
        0.1,
        1.0,
        0.05,
        2,
        "",
        "Fraction of the box rows that must show a clear edge crossing",
    ),
    (
        "max_angle_change_deg",
        "Max angle change",
        0.5,
        45.0,
        0.5,
        1,
        " °",
        "Frames whose edge is rotated more than this from the calibration are skipped",
    ),
    (
        "max_edge_residual_px",
        "Max edge residual",
        0.2,
        20.0,
        0.1,
        1,
        " px",
        "Frames whose edge is not straight (RMS off its line) are skipped",
    ),
    (
        "max_edge_disagreement_um",
        "Edge disagreement limit",
        0.05,
        10.0,
        0.1,
        2,
        " µm",
        "Two-edge lock: when the two edges disagree by more than this the "
        "frame is bad, unless one edge is within the hold band of its target "
        "(then the lock holds on that edge)",
    ),
    (
        "hold_band_um",
        "Hold band",
        0.0,
        2.0,
        0.05,
        2,
        " µm",
        "When the edges disagree beyond the limit, an edge this close to its "
        "target (or closer) is trusted and the lock holds on it instead of "
        "faulting. 0 turns holding off",
    ),
)


def source_spec(cfg: Any) -> dict[str, Any]:
    """The worker's frame-source spec for ``SpotFocusSettingsV1`` *cfg*."""
    src = cfg.source
    if src.kind == "fake":
        spec: dict[str, Any] = {"kind": "fake", **src.fake}
    else:
        spec = {"kind": "toupcam"}
        if src.serial:
            spec["serial"] = src.serial
        if src.sdk_dir:
            spec["sdk_dir"] = src.sdk_dir
    spec["analysis"] = {
        "channel": cfg.channel,
        "bayer": cfg.bayer,
        "detector": cfg.detector,
        "detector_sigma_px": cfg.detector_sigma_px,
    }
    return spec


def _rebuild_key(spec: dict | None) -> dict | None:
    """*spec* without the settings that change live (channel, edge detector)."""
    if spec is None:
        return None
    key = dict(spec)
    analysis = dict(key.get("analysis") or {})
    for live in ("channel", "detector", "detector_sigma_px"):
        analysis.pop(live, None)
    key["analysis"] = analysis
    return key


def _colored(text: str, color: str) -> str:
    return f'<span style="color:{color}">{html.escape(text)}</span>'


def noise_color(noise_um: float) -> str:
    """Return the quality colour of an edge with single-measurement *noise_um*.

    Parameters
    ----------
    noise_um : float
        The edge's noise in um of P.

    Returns
    -------
    str
        Green below ``NOISE_GOOD_UM``, attention below ``NOISE_WARN_UM``, red
        above (and for a non-finite value).
    """
    if not math.isfinite(noise_um) or noise_um >= NOISE_WARN_UM:
        return ALERT_COLOR
    return OK_COLOR if noise_um < NOISE_GOOD_UM else ATTENTION_COLOR


def detector_text(e: EdgeCalibration) -> str:
    """The detector *e* was calibrated with, for display ("gaussderiv 6 px")."""
    if e.detector == "gaussderiv":
        return f"gaussderiv {e.detector_sigma_px:g} px"
    return e.detector


def box_warnings(cal: FocusCalibration) -> list[str]:
    """Say which edge boxes of *cal* are too narrow across their edge.

    A box needs at least 4 edge widths along the normal for ``halfmax`` and 2
    for ``gaussderiv``. Edges with an unknown width (old calibrations) or box
    size are skipped.

    Parameters
    ----------
    cal : FocusCalibration
        The calibration.

    Returns
    -------
    list[str]
        One sentence per narrow box.
    """
    out = []
    for i, e in enumerate(cal.edges, 1):
        width, size = e.edge_width_px, e.box_normal_px
        if width is None or not math.isfinite(width) or size <= 0:
            continue
        factor = _BOX_WIDTH_FACTOR.get(e.detector, 4.0)
        if size < factor * width:
            out.append(
                f"box {i} is narrow for this edge ({size} px box, {width:.0f} px "
                "edge): widen it across the edge"
            )
    return out


# ---------------------------------------------------------------------------
# drawing widgets
# ---------------------------------------------------------------------------


class StripChart(QWidget):
    """Focus error and commanded piezo (both in µm) over the last few minutes."""

    def __init__(
        self, window_s: float = CHART_WINDOW_S, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.window_s = window_s
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        #: ``(t, along_um, p_cmd_rel_um)``
        self._points: deque[tuple[float, float, float]] = deque()
        self._p_start: float | None = None

    def __len__(self) -> int:
        return len(self._points)

    def add(self, t: float, along_um: float, p_cmd_um: float) -> None:
        """Append a sample; ``p_cmd_um`` is plotted relative to the first one."""
        if self._p_start is None:
            self._p_start = p_cmd_um
        self._points.append((t, along_um, p_cmd_um - self._p_start))
        while self._points and t - self._points[0][0] > self.window_s:
            self._points.popleft()
        self.update()

    def clear(self) -> None:
        """Drop all samples."""
        self._points.clear()
        self._p_start = None
        self.update()

    def paintEvent(self, event) -> None:
        """Draw the two traces."""
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101010"))
        r = QRectF(self.rect()).adjusted(46, 6, -6, -16)
        p.setPen(QColor("#555555"))
        p.drawRect(r)
        if len(self._points) < 2:
            p.setPen(QColor("#888888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no data")
            return
        pts = list(self._points)
        t1 = pts[-1][0]
        t0 = t1 - self.window_s
        span = max(max(abs(a) for _, a, _ in pts), max(abs(c) for _, _, c in pts))
        span = max(span * 1.1, 0.05)

        def xy(t: float, v: float) -> QPointF:
            return QPointF(
                r.left() + (t - t0) / self.window_s * r.width(),
                r.center().y() - v / span * r.height() / 2,
            )

        p.setPen(QPen(QColor("#555555"), 1, Qt.PenStyle.DashLine))
        p.drawLine(
            QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y())
        )
        p.setPen(QColor("#aaaaaa"))
        p.drawText(2, int(r.top()) + 10, f"+{span:.2f} µm")
        p.drawText(2, int(r.bottom()), f"-{span:.2f} µm")
        p.drawText(
            int(r.left()), int(self.height()) - 3, f"-{self.window_s / 60:g} min"
        )
        p.drawText(int(r.right()) - 22, int(self.height()) - 3, "now")
        for column, color in ((1, "#56b4e9"), (2, "#ff9a1f")):
            p.setPen(QPen(QColor(color), 1.5))
            for a, b in itertools.pairwise(pts):
                p.drawLine(xy(a[0], a[column]), xy(b[0], b[column]))
        p.setPen(QColor("#56b4e9"))
        p.drawText(int(r.left()) + 4, int(r.top()) + 12, "error (along)")
        p.setPen(QColor("#ff9a1f"))
        p.drawText(int(r.left()) + 90, int(r.top()) + 12, "piezo P (from start)")


# ---------------------------------------------------------------------------
# threading helpers
# ---------------------------------------------------------------------------


class _Relay(QObject):
    """Hands service / worker-thread results to the GUI thread."""

    telemetry = Signal(object)
    fault = Signal(str)
    state = Signal(str)
    moved = Signal(object)
    started = Signal()
    startFailed = Signal(str)
    #: The service refused to lock (a calibration/detector mismatch); not a crash.
    startRefused = Signal(str)
    previewStarted = Signal()
    previewFailed = Signal(str)
    calibrated = Signal(object)
    calibrateFailed = Signal(str)
    #: A calibration made on the way to Lock (the lock is still starting).
    calibrationUpdated = Signal(object)
    zeroFailed = Signal(str)
    #: ``(crisp_state_text, led_is_on)`` read once after each LED change.
    crispChanged = Signal(str, bool)
    ledFailed = Signal(str)

    def on_telemetry(self, msg: dict) -> None:
        self.telemetry.emit(msg)

    def on_fault(self, reason: str) -> None:
        self.fault.emit(reason)

    def on_state(self, state: str) -> None:
        self.state.emit(state)

    def on_moved(self, info: dict) -> None:
        self.moved.emit(info)


class _Owner:
    """Owns the service so shutdown can run while the panel is being deleted."""

    def __init__(self, panel: SpotFocusPanel | None = None) -> None:
        # A weak reference: shutdown also runs while the panel is being deleted.
        self._panel = weakref.ref(panel) if panel is not None else None
        self.service: SpotFocusService | None = None
        self.spec: dict | None = None
        self.dead = False
        self.lock = threading.Lock()

    def shutdown(self) -> None:
        with self.lock:
            svc, self.service = self.service, None
        if svc is not None:
            try:
                svc.shutdown()
            except Exception:
                logger.warning("spot focus shutdown failed", exc_info=True)
        panel = self._panel() if self._panel is not None else None
        if panel is not None:
            with suppress(RuntimeError):  # the widget is already deleted
                panel._set_lock_active(False)


# ---------------------------------------------------------------------------
# the panel
# ---------------------------------------------------------------------------


class SpotFocusPanel(QWidget):
    """Draw the edge box, calibrate, zero, lock and watch the spot focus lock.

    Parameters
    ----------
    mmcore : CMMCorePlus-like
        The core whose piezo P the loop moves.
    parent : QWidget | None
        Parent widget.
    service_factory : Callable[[dict, float | None], SpotFocusService] | None
        Builds the service from ``(source_spec, led_intensity)``. The default
        uses *mmcore*, *piezo_label* (``PiezoStage:P:34`` if ``None``) and the
        CRISP device *crisp_label*.
    crisp_label : str | None
        The CRISP device whose state and LED the lock uses; ``None`` for none.
    piezo_label : str | None
        The piezo the loop moves; ``None`` keeps the service's default
        (``PiezoStage:P:34``).

    Attributes
    ----------
    lock_active : bool
        ``True`` while the lock is starting, running, calibrating or zeroing (the
        host must not poll the Tiger then). :attr:`lockActiveChanged` fires when it changes.
    control_extra_layout : QHBoxLayout
        An empty layout in its own row directly under the control row (Calibrate,
        Zero here ... Lock, Stop): the host adds the Joystick checkbox here.
    message_label : QLabel
        The one contextual line (plain text, from :func:`piezo_status.message`).
    tracking_error_label : QLabel
        The tracking error ("+0.012 µm" while locked, else "Not available").
    """

    #: Emitted with the new :attr:`lock_active` value when it changes.
    lockActiveChanged = Signal(bool)
    #: Emitted by :meth:`_refresh_status` after it updated :attr:`message_label` and
    #: :attr:`tracking_error_label`: some input of :meth:`status_inputs` may have
    #: changed, so a host showing the status words should re-read them.
    statusChanged = Signal()
    lock_active: bool = False

    def __init__(
        self,
        mmcore: Any,
        parent: QWidget | None = None,
        *,
        service_factory: Callable[[dict, float | None], SpotFocusService] | None = None,
        crisp_label: str | None = None,
        piezo_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._mmc = mmcore
        self._crisp_label = crisp_label
        self._piezo_label = piezo_label
        self._factory = service_factory or self._default_factory
        self._owner = _Owner(self)
        self._loading = False
        self._busy = ""  # "starting" / "calibrating" / "zeroing" while a thread works
        self._preview_busy = False  # a thread is opening the camera
        self._sensor: tuple[int, int] | None = None  # known once the camera is open
        self.state = "idle"
        self._telemetry: dict | None = None
        self._cal: FocusCalibration | None = None
        self._cal_message = ""  # why there is no calibration (old file, ...)
        self._edge_box: tuple[int, int, int, int] | None = None
        # The UI has ONE edge. A saved ``edge_box_2`` stays in the store untouched but
        # is never read here, so this stays ``None``.
        self._edge_box2: tuple[int, int, int, int] | None = None
        # -- status inputs (see status_inputs / _refresh_status) ------------
        self._fault_reason = ""
        self._start_error = ""
        self._calibrate_error = ""
        self._zero_error = ""
        self._stop_error = ""
        self._camera_error = ""
        self._led_error = ""
        self._notice = ""  # a hint without a status field (shown when no error is)
        self._camera_missing = False
        self._crisp_present: bool | None = None  # None: not known yet
        self._mda_running = False  # cached by the 1 s tick
        self._tracking_error_um: float | None = None

        # -- controls ------------------------------------------------------
        self.calibrate_button = QPushButton("Calibrate")
        self.calibrate_button.setToolTip(
            "Focus on the cells first; sweeps P around here"
        )
        self.calibrate_button.clicked.connect(self.calibrate)
        self.zero_button = QPushButton("Zero here")
        self.zero_button.setToolTip(
            "Make the edge position now the in-focus zero (slope is kept); "
            "resets the offset"
        )
        self.zero_button.clicked.connect(self.zero)
        self.start_button = QPushButton("Lock")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        #: An empty layout in its own row under the control row: the host puts the
        #: Joystick checkbox in it.
        self.control_extra_layout = QHBoxLayout()
        control_row = QHBoxLayout()
        control_row.addWidget(self.calibrate_button)
        control_row.addWidget(self.zero_button)
        control_row.addStretch()
        control_row.addWidget(self.start_button)
        control_row.addWidget(self.stop_button)

        self.offset_spin = self._spin(
            -5.0,
            5.0,
            0.01,
            3,
            " µm",
            "Hold the focus this far from the zero (µm of P; applies at once)",
        )
        self.offset_spin.valueChanged.connect(lambda v: self._set_param("offset_um", v))
        #: "+0.012 µm" while the lock runs, else "Not available" (kept by
        #: :meth:`_refresh_status`).
        self.tracking_error_label = QLabel(piezo_status.NOT_AVAILABLE)
        self.tracking_error_label.setTextFormat(Qt.TextFormat.PlainText)
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("Offset"))
        offset_row.addWidget(self.offset_spin)
        offset_row.addStretch()
        offset_row.addWidget(QLabel("Tracking error:"))
        offset_row.addWidget(self.tracking_error_label)

        #: The ONE contextual line (:func:`piezo_status.message`), plain text.
        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)

        # -- edge box -------------------------------------------------------
        # The Draw edge button is this mirror of the view's: it also starts the
        # preview when needed. The view's own button stays hidden.
        self.draw_edge_button = QPushButton("Draw edge")
        self.draw_edge_button.setCheckable(True)
        self.draw_edge_button.setToolTip(
            "Drag a rectangle in the Spot camera window across the edge of the LED "
            "patch that moves with focus"
        )
        self.draw_edge_button.toggled.connect(self._toggle_draw_edge)
        self.edge_box_label = QLabel()
        self.edge_box_label.setTextFormat(Qt.TextFormat.PlainText)

        # Second edge: not part of this UI. The widgets exist (hidden, unconnected
        # to anything that writes) until the dead code is removed.
        self.draw_edge2_button = QPushButton("Draw edge box 2", self)
        self.draw_edge2_button.setCheckable(True)
        self.draw_edge2_button.toggled.connect(self._toggle_draw_edge2)
        self.clear_edge2_button = QPushButton("Clear box 2", self)
        self.edge_box2_label = QLabel(self)
        self.edge_box2_label.setTextFormat(Qt.TextFormat.PlainText)
        for w in (
            self.draw_edge2_button,
            self.clear_edge2_button,
            self.edge_box2_label,
        ):
            w.hide()

        self.span_spin = self._spin(
            0.2, 20.0, 0.5, 2, " µm", "Total piezo travel of the calibration sweep"
        )
        self.span_spin.valueChanged.connect(
            lambda v: self._set_sweep("sweep_span_um", float(v))
        )
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(5, 51)
        self.steps_spin.setKeyboardTracking(False)
        self.steps_spin.setToolTip("Points of the calibration sweep (at least 5)")
        self.steps_spin.valueChanged.connect(
            lambda v: self._set_sweep("sweep_steps", int(v))
        )
        self.frames_spin = QSpinBox()
        self.frames_spin.setRange(1, 50)
        self.frames_spin.setKeyboardTracking(False)
        self.frames_spin.setToolTip("Fresh frames averaged at each sweep point")
        self.frames_spin.valueChanged.connect(
            lambda v: self._set_sweep("sweep_frames", int(v))
        )

        # Old status widgets: no longer placed (message_label replaces them). They
        # stay hidden children so code that still sets them works.
        self.state_label = QLabel(self)
        self.state_label.setTextFormat(Qt.TextFormat.RichText)
        self.fault_label = QLabel(self)
        self.fault_label.setTextFormat(Qt.TextFormat.RichText)
        self.state_label.hide()
        self.fault_label.hide()
        self.calibration_label = QLabel()
        self.calibration_label.setTextFormat(Qt.TextFormat.RichText)
        self.calibration_label.setWordWrap(True)

        # -- views ---------------------------------------------------------
        self.chart = StripChart()
        self.live_view = SpotLiveView(parent=self)
        self.live_view.roiDrawn.connect(self._on_roi_drawn)
        self.live_view.edgeBoxDrawn.connect(self._on_edge_box_drawn)
        self.live_view.edgeDrawModeChanged.connect(self._on_edge_draw_mode)
        self.live_view.edgeBox2Drawn.connect(self._on_edge_box2_drawn)
        self.live_view.edge2DrawModeChanged.connect(self._on_edge2_draw_mode)
        # Queued: the close handler re-parents the view, which must not happen
        # inside its own closeEvent (Qt hides the widget right after that).
        self.live_view.windowClosed.connect(
            self._on_view_closed, Qt.ConnectionType.QueuedConnection
        )
        self.live_view.statsChanged.connect(self._on_view_stats)
        self.live_view.staleChanged.connect(lambda _: self._refresh_status())
        self.live_view.setMinimumHeight(_VIEW_MIN_H)
        # Takes the spare height (a tall window gives a bigger image); the sizeHint
        # is modest, so a short window scrolls in the host instead.
        self.live_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.live_view.set_polling(False)  # Preview turns it on

        # -- readouts ------------------------------------------------------
        self.readout_labels: dict[str, QLabel] = {}
        readouts = QFormLayout()
        readouts.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for key, text in (
            ("moves", "Moves / min"),
            ("move_ms", "Move time (mean / max)"),
            ("edge", "Edge (px)"),
            ("edge2", "Edge 2 (px)"),
            ("contrast", "Contrast"),
            ("fps", "Frame rate"),
            ("disagreement", "Disagreement (µm)"),
            ("error", "Error (along)"),
        ):
            label = QLabel("-")
            if key in ("edge", "edge2"):
                label.setTextFormat(Qt.TextFormat.RichText)  # coloured noise
            self.readout_labels[key] = label
            if key in _HIDDEN_READOUTS:
                label.setParent(self)  # not a row: see _on_telemetry
                label.hide()
            else:
                readouts.addRow(text, label)
        self.note_label = QLabel()
        self.note_label.setTextFormat(Qt.TextFormat.RichText)
        self.note_label.setWordWrap(True)

        # -- parameters ----------------------------------------------------
        self.param_spins: dict[str, QDoubleSpinBox] = {"offset_um": self.offset_spin}
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for field, label, lo, hi, step, decimals, suffix, tip in _PARAMS:
            spin = self._spin(lo, hi, step, decimals, suffix, tip)
            spin.valueChanged.connect(lambda v, f=field: self._set_param(f, v))
            self.param_spins[field] = spin
            if field in _HIDDEN_PARAMS:
                # Two-edge only. Kept so load / save leaves the setting unchanged.
                spin.setParent(self)
                spin.hide()
            else:
                form.addRow(label, spin)

        # -- sections ------------------------------------------------------
        camera_page = self._build_camera_page()
        calibration_page = self._build_calibration_page()
        diagnostics_page = QWidget()
        diag_lay = QVBoxLayout(diagnostics_page)
        diag_lay.setContentsMargins(0, 0, 0, 0)
        diag_lay.addWidget(self.chart, 1)
        diag_lay.addLayout(readouts)
        diag_lay.addWidget(self.note_label)
        diag_lay.addWidget(self.live_view.cursor_label)
        diag_lay.addWidget(self.live_view.status_label)
        self.live_view.cursor_label.show()
        self.live_view.status_label.show()
        advanced_page = QWidget()
        adv_lay = QVBoxLayout(advanced_page)
        adv_lay.setContentsMargins(0, 0, 0, 0)
        adv_lay.addLayout(form)

        #: Section name -> its checkable button / its content widget. One page is
        #: visible at a time (all closed at start).
        self.section_buttons: dict[str, QPushButton] = {}
        self.section_pages: dict[str, QWidget] = {}
        section_row = QHBoxLayout()
        for name, title, page in (
            ("camera", "Camera", camera_page),
            ("calibration", "Calibration settings", calibration_page),
            ("diagnostics", "Diagnostics", diagnostics_page),
            ("advanced", "Advanced", advanced_page),
        ):
            button = QPushButton(title)
            button.setCheckable(True)
            button.toggled.connect(lambda on, n=name: self._on_section_toggled(n, on))
            page.hide()
            self.section_buttons[name] = button
            self.section_pages[name] = page
            section_row.addWidget(button)

        # -- toolbar rows under the image -----------------------------------
        self.preview_button = QPushButton("Preview")
        self.preview_button.setCheckable(True)
        self.preview_button.setToolTip(
            "Stream the camera into the Spot camera view (full resolution)"
        )
        self.preview_button.clicked.connect(self.set_preview)
        self.pop_out_button = QPushButton("Pop out")
        self.pop_out_button.setCheckable(True)
        self.pop_out_button.setToolTip(
            "Float the Spot camera view as its own window; click again (or close "
            "the window) to dock it back above the controls"
        )
        self.pop_out_button.toggled.connect(self.set_view_floating)
        self.frame_stats_label = QLabel(_FRAME_STATS_IDLE)
        self.frame_stats_label.setTextFormat(Qt.TextFormat.PlainText)
        self.frame_stats_label.setWordWrap(True)
        self.camera_readout = QLabel("-", self)  # old readout: hidden, kept for PR2
        self.camera_readout.hide()
        view = self.live_view
        # A docked child, not the window it was created as. The flags are explicit:
        # setParent(parent) alone does nothing when the parent is unchanged.
        view.setParent(self, Qt.WindowType.Widget)
        self.display_settings_button = QPushButton("Display settings")
        self.display_settings_button.setToolTip(
            "Auto levels, Bayer to grey and the zoom of the image"
        )
        self._display_menu = self._build_display_menu()
        self.display_settings_button.setMenu(self._display_menu)
        for w in (view.fit_button, view.one_to_one_button, view.draw_roi_button):
            w.show()
        toolbar1 = QHBoxLayout()
        toolbar1.addWidget(self.preview_button)
        toolbar1.addWidget(view.fit_button)
        toolbar1.addWidget(view.one_to_one_button)
        toolbar1.addWidget(self.pop_out_button)
        toolbar1.addStretch()
        toolbar2 = QHBoxLayout()
        toolbar2.addWidget(QLabel("Image region:"))
        toolbar2.addWidget(view.draw_roi_button)
        toolbar2.addWidget(self.full_frame_button)
        toolbar2.addStretch()
        toolbar3 = QHBoxLayout()
        toolbar3.addWidget(QLabel("Tracking edge:"))
        toolbar3.addWidget(self.draw_edge_button)
        toolbar3.addStretch()
        toolbar3.addWidget(self.display_settings_button)

        # -- layout --------------------------------------------------------
        self._view_index = 0  # where the view goes back to
        self._float_size = QSize(900, 700)  # the popped-out window's size
        self._float_hidden = False  # the floating view is hidden with the panel
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(control_row)
        outer.addLayout(self.control_extra_layout)
        outer.addLayout(offset_row)
        outer.addWidget(self.message_label)
        outer.addWidget(view, 1)  # re-parents it: docked
        view.show()  # setParent hid it
        outer.addLayout(toolbar1)
        outer.addLayout(toolbar2)
        outer.addLayout(toolbar3)
        outer.addWidget(self.frame_stats_label)
        outer.addLayout(section_row)
        for page in self.section_pages.values():
            outer.addWidget(page)
        self._outer = outer

        # -- timers, relay -------------------------------------------------
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(_FLUSH_DELAY_MS)
        self._flush_timer.timeout.connect(lambda: get_settings_store().save())
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(_STATS_PERIOD_MS)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

        self._relay = _Relay(self)
        self._relay.telemetry.connect(self._on_telemetry)
        self._relay.fault.connect(self._on_fault)
        self._relay.state.connect(self._on_state)
        self._relay.started.connect(self._on_started)
        self._relay.startFailed.connect(self._on_start_failed)
        self._relay.startRefused.connect(self._on_start_refused)
        self._relay.previewStarted.connect(self._on_preview_started)
        self._relay.previewFailed.connect(self._on_preview_failed)
        self._relay.calibrated.connect(self._on_calibrated)
        self._relay.calibrateFailed.connect(self._on_calibrate_failed)
        self._relay.calibrationUpdated.connect(self._apply_calibration)
        self._relay.zeroFailed.connect(self._on_zero_failed)
        self._relay.crispChanged.connect(self._on_crisp_changed)
        self._relay.ledFailed.connect(self._on_led_failed)

        # Closures over the owner, not self: they run while self is going away.
        owner = self._owner
        self.destroyed.connect(lambda *_: owner.shutdown())
        if (app := QApplication.instance()) is not None:
            app.aboutToQuit.connect(owner.shutdown)

        self._set_state("idle")
        self.refresh()

    def _build_camera_page(self) -> QWidget:
        """The Camera section: SDK folder, exposure/gain/channel, ROI, LED."""
        self.sdk_edit = QLineEdit()
        self.sdk_edit.setPlaceholderText("folder holding the vendor's toupcam.py")
        self.sdk_edit.setToolTip(
            "Folder with the ToupTek SDK's toupcam.py and its native library. "
            "Empty uses the TOUPCAM_SDK_DIR environment variable."
        )
        self.sdk_edit.editingFinished.connect(self._set_sdk_dir)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse_sdk)
        sdk_row = QHBoxLayout()
        sdk_row.addWidget(self.sdk_edit, 1)
        sdk_row.addWidget(self.browse_button)

        self.camera_label = QLabel()
        self.camera_label.setTextFormat(Qt.TextFormat.PlainText)
        self.camera_label.setWordWrap(True)

        self.led_button = QPushButton("LED")
        self.led_button.setCheckable(True)
        self.led_button.setToolTip(
            "Light the CRISP LED (CRISP State Ready). Preview lights it too; "
            "the previous CRISP state is restored when it goes off."
        )
        self.led_button.clicked.connect(self.set_led)
        self.led_spin = self._spin(
            0.0, 100.0, 1.0, 0, " %", "CRISP LED intensity (applies at once)"
        )
        self.led_spin.valueChanged.connect(self._set_led)
        # Old CRISP text readout: hidden (the message line reports problems).
        self.crisp_readout = QLabel("CRISP: -", self)
        self.crisp_readout.setTextFormat(Qt.TextFormat.PlainText)
        self.crisp_readout.hide()

        self.exposure_spin = self._spin(
            0.1, 15000.0, 1.0, 2, " ms", "Camera exposure (applies at once)"
        )
        self.exposure_spin.valueChanged.connect(self._set_exposure)
        self.gain_spin = QSpinBox()
        self.gain_spin.setRange(100, 5000)
        self.gain_spin.setSingleStep(100)
        self.gain_spin.setSuffix(" %")
        self.gain_spin.setKeyboardTracking(False)
        self.gain_spin.setToolTip("Camera gain, percent (100 = unity)")
        self.gain_spin.valueChanged.connect(self._set_gain)
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(["red", "sum"])
        self.channel_combo.setToolTip(
            "Bayer sites the spot is measured on: red only (the LED is 720 nm) "
            "or the sum of all"
        )
        self.channel_combo.currentTextChanged.connect(self._set_channel)

        self.roi_spins: dict[str, QSpinBox] = {}
        for name in ("x", "y", "w", "h"):
            spin = QSpinBox()
            spin.setRange(0, 16384)
            spin.setSingleStep(2)
            spin.setKeyboardTracking(False)
            spin.setToolTip(f"ROI {name} in sensor pixels (even); 0 width = full frame")
            self.roi_spins[name] = spin
        self.apply_roi_button = QPushButton("Apply ROI")
        self.apply_roi_button.clicked.connect(self.apply_roi)
        # Placed in the toolbar under the image, not in this section.
        self.full_frame_button = QPushButton("Full frame")
        self.full_frame_button.clicked.connect(self.full_frame)

        grid = QGridLayout()
        grid.addWidget(QLabel("Exposure"), 0, 0)
        grid.addWidget(self.exposure_spin, 0, 1)
        grid.addWidget(QLabel("Gain"), 0, 2)
        grid.addWidget(self.gain_spin, 0, 3)
        grid.addWidget(QLabel("Channel"), 1, 0)
        grid.addWidget(self.channel_combo, 1, 1)
        grid.addWidget(self.led_button, 2, 0)
        grid.addWidget(self.led_spin, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        roi_grid = QGridLayout()
        for i, (name, spin) in enumerate(self.roi_spins.items()):
            roi_grid.addWidget(QLabel(name), i // 2, (i % 2) * 2)
            roi_grid.addWidget(spin, i // 2, (i % 2) * 2 + 1)
        roi_grid.setColumnStretch(1, 1)
        roi_grid.setColumnStretch(3, 1)
        roi_buttons = QHBoxLayout()
        roi_buttons.addWidget(self.apply_roi_button)
        roi_buttons.addStretch()

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("SDK folder"))
        lay.addLayout(sdk_row)
        lay.addWidget(self.camera_label)
        lay.addLayout(grid)
        lay.addWidget(QLabel("ROI (sensor pixels)"))
        lay.addLayout(roi_grid)
        lay.addLayout(roi_buttons)
        return page

    def _build_calibration_page(self) -> QWidget:
        """The Calibration settings section: sweep, edge detector, box, summary."""
        self.detector_combo = QComboBox()
        for name in EDGE_DETECTORS:
            self.detector_combo.addItem(EDGE_DETECTOR_LABELS[name], name)
        self.detector_combo.setToolTip(
            "How the edge position is read from the image.\n"
            "Gaussian derivative: finds the steepest point of the edge; robust to "
            "soft edges and to the box size.\n"
            "Half-max: the 50 % crossing, the original method; needs a box at "
            "least 4x as wide as the edge.\n"
            "A calibration is only valid for the detector it was made with."
        )
        self.detector_combo.currentIndexChanged.connect(self._set_detector)
        self.detector_sigma_spin = self._spin(
            SIGMA_PX_RANGE[0],
            SIGMA_PX_RANGE[1],
            0.5,
            1,
            " px",
            "Smoothing width of the Gaussian derivative, sensor pixels (wider = "
            "less noise, blurrier on sharp edges). Not used by half-max.",
        )
        self.detector_sigma_spin.valueChanged.connect(self._set_detector_sigma)

        grid = QGridLayout()
        grid.addWidget(QLabel("Span"), 0, 0)
        grid.addWidget(self.span_spin, 0, 1)
        grid.addWidget(QLabel("Steps"), 0, 2)
        grid.addWidget(self.steps_spin, 0, 3)
        grid.addWidget(QLabel("Frames per step"), 1, 0)
        grid.addWidget(self.frames_spin, 1, 1)
        grid.addWidget(QLabel("Edge detector"), 2, 0)
        grid.addWidget(self.detector_combo, 2, 1)
        grid.addWidget(QLabel("Detector width"), 3, 0)
        grid.addWidget(self.detector_sigma_spin, 3, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        box_row = QHBoxLayout()
        box_row.addWidget(QLabel("Edge box (x, y, w, h)"))
        box_row.addWidget(self.edge_box_label, 1)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(grid)
        lay.addLayout(box_row)
        lay.addWidget(self.calibration_label)
        return page

    def _build_display_menu(self) -> QMenu:
        """The Display settings popup: Auto levels, Bayer to grey, zoom %."""
        view = self.live_view
        menu = QMenu(self)
        # The view created these hidden; show them first (the menu sizes a widget
        # action from the widget as it is when the action is added).
        for w in (view.auto_levels, view.bayer_grey):
            w.show()
            action = QWidgetAction(menu)
            action.setDefaultWidget(w)
            menu.addAction(action)
        zoom_row = QWidget()
        zoom_lay = QHBoxLayout(zoom_row)
        zoom_lay.addWidget(QLabel("Zoom"))
        view.zoom_label.show()
        zoom_lay.addWidget(view.zoom_label)
        zoom_action = QWidgetAction(menu)
        zoom_action.setDefaultWidget(zoom_row)
        menu.addAction(zoom_action)
        return menu

    def _on_section_toggled(self, name: str, on: bool) -> None:
        """Open one section (closing the others) or close the open one."""
        if on:
            for other, button in self.section_buttons.items():
                if other != name and button.isChecked():
                    button.setChecked(False)  # re-enters here with on=False
        self.section_pages[name].setVisible(on)

    @staticmethod
    def _spin(
        lo: float, hi: float, step: float, decimals: int, suffix: str, tip: str
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setToolTip(tip)
        spin.setKeyboardTracking(False)
        return spin

    # ------------------------------------------------------------------
    # service
    # ------------------------------------------------------------------

    @property
    def service(self) -> SpotFocusService | None:
        """The service, ``None`` until Start or Calibrate created it."""
        return self._owner.service

    def _default_factory(self, spec: dict, led: float | None) -> SpotFocusService:
        crisp = self._crisp_label or None
        if crisp is not None:
            with suppress(Exception):
                if crisp not in self._mmc.getLoadedDevices():
                    crisp = None
        piezo = {"piezo_label": self._piezo_label} if self._piezo_label else {}
        return SpotFocusService(
            self._mmc, crisp_label=crisp, source_spec=spec, led_intensity=led, **piezo
        )

    def _ensure_service(self) -> SpotFocusService:
        """Create (or re-create) the service. Runs on a worker thread."""
        cfg = get_settings_store().settings
        spec = source_spec(cfg)
        owner = self._owner
        with owner.lock:
            svc = owner.service
            if svc is not None and (
                owner.dead or _rebuild_key(owner.spec) != _rebuild_key(spec)
            ):
                with suppress(Exception):
                    svc.shutdown()
                svc = owner.service = None
            if svc is None:
                svc = self._factory(spec, cfg.led_intensity)
                svc.telemetry.connect(self._relay.on_telemetry)
                svc.fault.connect(self._relay.on_fault)
                svc.state.connect(self._relay.on_state)
                svc.moved.connect(self._relay.on_moved)
                owner.service, owner.spec, owner.dead = svc, spec, False
                self._send_preview_state(svc)  # the preview measures the saved boxes
            return svc

    @staticmethod
    def _drawn_boxes(cfg: Any) -> list[tuple[int, int, int, int]]:
        """The saved edge box as a list; none without box 1.

        Only ONE edge is used: a saved ``edge_box_2`` stays in the store but is never
        passed on.
        """
        return [] if cfg.edge_box is None else [cfg.edge_box]

    @staticmethod
    def _single_edge(cal: FocusCalibration | None) -> FocusCalibration | None:
        """*cal* if it was made with exactly one edge, else ``None`` (a two-edge one)."""
        return cal if cal is not None and len(cal.edges) == 1 else None

    def _send_preview_state(self, svc: SpotFocusService | None = None) -> None:
        """Tell the worker the saved calibration and edge boxes.

        The preview measures the one box and shows its error. A calibration made with
        two edges is not sent (the UI is single-edge).
        """
        if svc is None:
            svc = self.service
        if svc is None:
            return
        cfg = get_settings_store().settings
        try:
            svc.set_calibration(self._single_edge(cfg.calibration_status()[0]))
            svc.set_edge_boxes(self._drawn_boxes(cfg))
        except Exception:
            logger.warning("spot focus set_edge_boxes failed", exc_info=True)

    def shutdown(self) -> None:
        """Stop the loop (restoring CRISP) and end the worker process."""
        self.live_view.set_service(None)
        self.live_view.set_polling(False)
        if self.live_view.isWindow():
            self.live_view.hide()  # no orphan window; it is deleted with the panel
        self.preview_button.setChecked(False)
        self._owner.shutdown()
        self._set_state("idle")

    def _run_async(
        self,
        name: str,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[str], None],
    ) -> None:
        def target() -> None:
            try:
                result = work()
            except Exception as err:
                logger.warning("spot focus %s failed", name, exc_info=True)
                failed(str(err) or type(err).__name__)
            else:
                done(result)

        threading.Thread(target=target, name=f"spot-focus-{name}", daemon=True).start()

    # ------------------------------------------------------------------
    # settings <-> widgets
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read everything shown from the store's ``SpotFocusSettingsV1``."""
        cfg = get_settings_store().settings
        self._loading = True
        try:
            for field, spin in self.param_spins.items():
                spin.setValue(getattr(cfg.params, field))
            self.led_spin.setValue(self._led_value())
            self.exposure_spin.setValue(cfg.exposure_ms)
            self.gain_spin.setValue(cfg.gain)
            self.channel_combo.setCurrentText(cfg.channel)
            self.detector_combo.setCurrentIndex(
                max(self.detector_combo.findData(cfg.detector), 0)
            )
            self.detector_sigma_spin.setValue(cfg.detector_sigma_px)
            self.sdk_edit.setText(cfg.source.sdk_dir or "")
            self._load_roi_spins(cfg.roi)
            self.span_spin.setValue(cfg.sweep_span_um)
            self.steps_spin.setValue(cfg.sweep_steps)
            self.frames_spin.setValue(cfg.sweep_frames)
        finally:
            self._loading = False
        # An old / malformed calibration is reported, never raised.
        self._cal, self._cal_message = cfg.calibration_status()
        if self._cal is None and not cfg.calibration:
            self._cal_message = ""
        self._edge_box = cfg.edge_box
        self.live_view.set_edge_box(self._edge_box)
        self.live_view.set_edge_box2(None)  # one edge only; the saved box 2 is kept
        self._show_edge_box_text()
        self._place_zero_marker()
        self._refresh_calibration()
        self._update_enabled()

    def _show_edge_box_text(self) -> None:
        box = self._edge_box
        self.edge_box_label.setText(
            "none" if box is None else ", ".join(str(int(v)) for v in box)
        )

    @staticmethod
    def _zero_point(edge: Any) -> tuple[float, float]:
        """The zero edge point ``(x, y)`` of a calibrated edge, sensor px."""
        x, y, w, h = edge.box
        if edge.axis == "x":
            return (edge.e0_px, y + h / 2)
        return (x + w / 2, edge.e0_px)

    def _place_zero_marker(self) -> None:
        """Put the live view's rings at the zero edge points of the calibration."""
        cal = self._cal
        if cal is None:
            self.live_view.set_setpoint(None)
            self.live_view.set_secondary_setpoint(None)
            return
        self.live_view.set_setpoint(self._zero_point(cal.edges[0]))
        self.live_view.set_secondary_setpoint(None)  # one edge only

    def _refresh_calibration(self) -> None:
        cal = self._cal
        if cal is None:
            self.calibration_label.setText(
                _colored(self._cal_message or "not calibrated", ATTENTION_COLOR)
            )
            self._refresh_status()
            return
        # One edge only: a calibration made with more shows edge 1 and is stale.
        lines = [
            html.escape(
                f"edge {i} axis {e.axis} | {e.slope_px_per_um:+.2f} px/µm | "
                f"R² {e.r2:.3f} | residual {e.residual_px:.2f} px | "
            )
            + _colored(
                f"noise {edge_noise_um(e):.3f} µm", noise_color(edge_noise_um(e))
            )
            for i, e in enumerate(cal.edges[:1], 1)
        ]
        detectors = "/".join(dict.fromkeys(detector_text(e) for e in cal.edges))
        lines.append(
            html.escape(
                f"zero P {cal.p0_um:.3f} µm | ±{cal.linear_range_um:.2f} µm | "
                f"detector {detectors} | {cal.created}"
            )
        )
        text = "<br>".join(lines)
        notes = list(cal.warnings) if len(cal.edges) == 1 else []
        if why := self._mismatch_text():
            notes.insert(0, why)
        notes.extend(w for w in box_warnings(cal) if w.startswith("box 1 "))
        if self._box_changed():
            notes.append(piezo_status.BOX_CHANGED_TEXT)
        if notes:
            text += "<br>" + _colored("; ".join(notes), ATTENTION_COLOR)
        self.calibration_label.setText(text)
        self._refresh_status()

    def _drawn_boxes_now(self) -> list[tuple[int, int, int, int]]:
        """The box drawn now as a list (one edge only); none without a box."""
        return [] if self._edge_box is None else [self._edge_box]

    def _box_changed(self) -> bool:
        """The calibration no longer fits the drawn box.

        True when the drawn (snapped) box differs from the calibration's, and for any
        calibration made with other than exactly one edge.
        """
        cal = self._cal
        if cal is None:
            return False
        if len(cal.edges) != 1:
            return True
        drawn = [snap_box(b) for b in self._drawn_boxes_now()]
        return bool(drawn) and drawn != [tuple(b) for b in cal.boxes]

    def _write(self) -> None:
        self._flush_timer.start()

    def _set_sweep(self, field: str, value: float) -> None:
        if self._loading:
            return
        setattr(get_settings_store().settings, field, value)
        self._write()

    def _reset_offset(self) -> None:
        """Set the offset to 0 (after Zero or a new calibration)."""
        get_settings_store().settings.params.offset_um = 0.0
        self._loading = True
        try:
            self.offset_spin.setValue(0.0)
        finally:
            self._loading = False
        self._write()

    def _set_param(self, field: str, value: float) -> None:
        if self._loading:
            return
        cfg = get_settings_store().settings
        setattr(cfg.params, field, value)
        self._write()
        if self.state == "running" and (svc := self.service) is not None:
            svc.update_params(cfg.params.to_params())

    @staticmethod
    def _led_value() -> float:
        """The saved LED intensity, % (``DEFAULT_LED_PCT`` when unset)."""
        led = get_settings_store().settings.led_intensity
        return DEFAULT_LED_PCT if led is None else float(led)

    def _set_led(self, value: float) -> None:
        if self._loading:
            return
        get_settings_store().settings.led_intensity = float(value)
        self._write()
        svc = self.service
        if svc is not None:
            try:
                svc.set_led_intensity(value)
            except Exception:
                logger.warning("spot focus set_led_intensity failed", exc_info=True)

    def set_led(self, on: bool) -> None:
        """Light or darken the CRISP LED (the LED button)."""
        led = self._led_value()
        if on:
            self._led_async("led on", lambda svc: svc.led_on(led, owner="manual"))
        else:
            # The user's off beats Preview's claim; the lock's claim stays.
            def off(svc: SpotFocusService) -> None:
                svc.led_off("manual")
                svc.led_off("preview")

            self._led_async("led off", off)

    def _led_async(self, name: str, action: Callable[[SpotFocusService], None]) -> None:
        """Run an LED change off the GUI thread, then show CRISP's state."""

        def work() -> None:
            svc = self._ensure_service()
            action(svc)
            self._report_crisp(svc)

        def failed(message: str) -> None:
            self._relay.ledFailed.emit(message)

        self._run_async(name, work, lambda _: None, failed)

    def _report_crisp(self, svc: SpotFocusService) -> None:
        """Read CRISP's state once and hand it to the GUI thread."""
        label = svc.crisp_label
        if label is None:
            self._relay.crispChanged.emit("", False)
            return
        state = svc.crisp_state()
        self._relay.crispChanged.emit(
            f"CRISP: {state if state is not None else '?'} ({label})", svc.led_is_on
        )

    def _on_crisp_changed(self, text: str, led_on: bool) -> None:
        self.led_button.blockSignals(True)
        self.led_button.setChecked(led_on)
        self.led_button.blockSignals(False)
        if not text:
            self.crisp_readout.setText("CRISP: none")
            self._crisp_present = False  # the message line says so (_NO_CRISP)
        else:
            self.crisp_readout.setText(text)
            self._crisp_present = True
        self._refresh_status()

    def _on_led_failed(self, message: str) -> None:
        self.led_button.blockSignals(True)
        self.led_button.setChecked(False)
        self.led_button.blockSignals(False)
        self._set_problem("_led_error", message)

    def _apply_camera(self, **kwargs: Any) -> None:
        """Send a live camera change to the worker (if there is one)."""
        svc = self.service
        if svc is None or self.state == "fault":
            return
        try:
            svc.set_camera(**kwargs)
        except Exception as err:
            logger.warning("spot focus set_camera failed", exc_info=True)
            self._set_problem("_camera_error", str(err))

    def _set_exposure(self, value: float) -> None:
        if self._loading:
            return
        get_settings_store().settings.exposure_ms = value
        self._write()
        self._apply_camera(exposure_ms=value)

    def _set_gain(self, value: int) -> None:
        if self._loading:
            return
        get_settings_store().settings.gain = int(value)
        self._write()
        self._apply_camera(gain=int(value))

    def _set_channel(self, value: str) -> None:
        if self._loading or value not in ("red", "sum"):
            return
        get_settings_store().settings.channel = value  # type: ignore[assignment]
        self._write()
        self._apply_camera(channel=value)

    # -- edge detector ---------------------------------------------------

    def _detector_setting(self) -> tuple[str, float]:
        """The saved edge detector and its Gaussian width (sensor px)."""
        cfg = get_settings_store().settings
        return cfg.detector, float(cfg.detector_sigma_px)

    def _mismatch_text(self) -> str:
        """Why the calibration does not fit the detector setting ("" if it does)."""
        if self._cal is None:
            return ""
        return detector_mismatch(self._cal, *self._detector_setting())

    def _set_detector(self, index: int) -> None:
        if self._loading:
            return
        name = self.detector_combo.itemData(index)
        if name not in EDGE_DETECTORS:
            return
        get_settings_store().settings.detector = name
        self._write()
        self._detector_changed()

    def _set_detector_sigma(self, value: float) -> None:
        if self._loading:
            return
        get_settings_store().settings.detector_sigma_px = float(value)
        self._write()
        self._detector_changed()

    def _detector_changed(self) -> None:
        """Send the detector setting to the worker and re-check the calibration."""
        name, sigma = self._detector_setting()
        self._apply_camera(detector=name, detector_sigma_px=sigma)
        self._refresh_calibration()
        self._update_enabled()

    # -- SDK folder -----------------------------------------------------

    def _set_sdk_dir(self) -> None:
        if self._loading:
            return
        text = self.sdk_edit.text().strip()
        cfg = get_settings_store().settings
        if (text or None) == cfg.source.sdk_dir:
            return
        cfg.source.sdk_dir = text or None
        self._write()
        # A running camera keeps the old folder; the next Preview rebuilds.
        if self.preview_button.isChecked() and self.state != "running":
            self._stop_preview()

    def _browse_sdk(self) -> None:
        start = self.sdk_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Folder with the ToupTek SDK (toupcam.py)", start
        )
        if folder:
            self.sdk_edit.setText(folder)
            self._set_sdk_dir()

    # -- ROI -------------------------------------------------------------

    def _load_roi_spins(self, roi: tuple[int, int, int, int] | None) -> None:
        if roi is None:
            sw, sh = self._sensor or (0, 0)
            roi = (0, 0, sw, sh)
        for spin, value in zip(self.roi_spins.values(), roi, strict=True):
            spin.setValue(int(value))

    def apply_roi(self) -> None:
        """Apply the ROI spins (a zero width or height means full frame)."""
        x, y, w, h = (spin.value() for spin in self.roi_spins.values())
        x, y, w, h = x // 2 * 2, y // 2 * 2, w // 2 * 2, h // 2 * 2
        if w <= 0 or h <= 0:
            self.full_frame()
            return
        if self._sensor is not None:
            sw, sh = self._sensor
            x, y = min(x, sw - 2), min(y, sh - 2)
            w, h = min(w, sw - x) // 2 * 2, min(h, sh - y) // 2 * 2
        self._set_roi((x, y, w, h))

    def full_frame(self) -> None:
        """Use the whole sensor."""
        self._set_roi(None)

    def _set_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        if roi is not None and self._sensor is not None:
            if roi == (0, 0, *self._sensor):
                roi = None
        get_settings_store().settings.roi = roi
        self._write()
        self._loading = True
        try:
            self._load_roi_spins(roi)
        finally:
            self._loading = False
        self._apply_camera(roi=roi or (0, 0, 0, 0))

    def _on_roi_drawn(self, x: int, y: int, w: int, h: int) -> None:
        self._set_roi((x, y, w, h))

    # -- edge box --------------------------------------------------------

    def _toggle_draw_edge(self, on: bool) -> None:
        """The Draw edge box button: put the live view in edge-box draw mode."""
        self.live_view.set_edge_draw_mode(on)
        if not on:
            return
        if self.service is not None and (
            self.preview_button.isChecked() or self.state == "running"
        ):
            self._show_live_view()
        else:
            self._start_preview()  # the box is drawn on the live image

    def _toggle_draw_edge2(self, on: bool) -> None:
        """The Draw edge box 2 button: put the live view in that draw mode."""
        self.live_view.set_edge2_draw_mode(on)
        if not on:
            return
        if self.service is not None and (
            self.preview_button.isChecked() or self.state == "running"
        ):
            self._show_live_view()
        else:
            self._start_preview()  # the box is drawn on the live image

    def _on_edge2_draw_mode(self, on: bool) -> None:
        """The view left (or entered) draw box 2 mode: keep the button in step."""
        self.draw_edge2_button.blockSignals(True)
        self.draw_edge2_button.setChecked(on)
        self.draw_edge2_button.blockSignals(False)

    def _on_edge_draw_mode(self, on: bool) -> None:
        """The view left (or entered) draw mode: keep the button in step."""
        self.draw_edge_button.blockSignals(True)
        self.draw_edge_button.setChecked(on)
        self.draw_edge_button.blockSignals(False)

    def _on_edge_box_drawn(self, x: int, y: int, w: int, h: int) -> None:
        if self._busy or self.state == "running":
            self._set_problem("_notice", "Stop the lock before moving the edge box.")
            return
        self.set_edge_box((x, y, w, h))

    def set_edge_box(self, box: tuple[int, int, int, int] | None) -> None:
        """Use *box* ``(x, y, w, h)`` (sensor px) as the edge box; ``None`` clears it.

        It is saved, drawn on the live view and sent to the worker (which
        measures it in the preview).
        """
        get_settings_store().settings.edge_box = box
        self._write()
        self._edge_box = box
        self.live_view.set_edge_box(box)
        self._show_edge_box_text()
        self._refresh_calibration()
        self._update_enabled()
        if self.state != "running":
            self._send_preview_state()

    def _on_edge_box2_drawn(self, x: int, y: int, w: int, h: int) -> None:
        """Ignored: the UI has one edge (no control can start Draw edge box 2)."""

    def set_edge_box2(self, box: tuple[int, int, int, int] | None) -> None:
        """Use *box* ``(x, y, w, h)`` (sensor px) as the second edge box.

        Not used by the UI (one edge only; nothing calls it, because it would
        overwrite the saved ``edge_box_2``). ``None`` clears it.
        """
        get_settings_store().settings.edge_box_2 = box
        self._write()
        self._edge_box2 = box
        self.live_view.set_edge_box2(box)
        if box is None:
            self.live_view.set_secondary_point(None)
        self._show_edge_box_text()
        self._refresh_calibration()
        self._update_enabled()
        if self.state != "running":
            self._send_preview_state()

    # -- camera info -----------------------------------------------------

    def _apply_camera_info(self, info: dict) -> None:
        """Show the camera and set the spin ranges from what it reported."""
        if not info:
            return
        self._loading = True
        try:
            if rng := info.get("exposure_range_ms"):
                self.exposure_spin.setRange(float(rng[0]), float(rng[1]))
            if rng := info.get("gain_range"):
                self.gain_spin.setRange(int(rng[0]), int(rng[1]))
            sensor = info.get("sensor")
            if sensor:
                self._sensor = (int(sensor[0]), int(sensor[1]))
                sw, sh = self._sensor
                for name, limit in (("x", sw - 2), ("y", sh - 2), ("w", sw), ("h", sh)):
                    self.roi_spins[name].setMaximum(limit)
            cfg = get_settings_store().settings
            self.exposure_spin.setValue(cfg.exposure_ms)
            self.gain_spin.setValue(cfg.gain)
            self._load_roi_spins(cfg.roi)
        finally:
            self._loading = False
        parts = [str(info.get("model") or "camera")]
        if info.get("serial"):
            parts.append(f"serial {info['serial']}")
        if sensor:
            parts.append(f"{sensor[0]} x {sensor[1]}")
        if info.get("bits"):
            parts.append(f"{info['bits']}-bit")
        self.camera_label.setText("  |  ".join(parts))

    # -- preview ---------------------------------------------------------

    def set_preview(self, on: bool) -> None:
        """Turn the camera preview (and the live view window) on or off."""
        if on:
            self._start_preview()
        else:
            self._stop_preview()

    def _start_preview(self) -> None:
        if self._preview_busy:
            return
        if self.state == "running" and self.service is not None:
            # The lock already streams frames; only the window is missing.
            self.preview_button.setChecked(True)
            self._show_live_view()
            return
        cfg = get_settings_store().settings
        exposure, gain, channel = cfg.exposure_ms, cfg.gain, cfg.channel
        roi = cfg.roi or (0, 0, 0, 0)
        det: dict[str, Any] = {
            "detector": cfg.detector,
            "detector_sigma_px": cfg.detector_sigma_px,
        }
        led = self._led_value()
        self._clear_problem(refresh=False)
        self._camera_missing = False
        self._preview_busy = True
        self.preview_button.setChecked(True)
        self.preview_button.setEnabled(False)
        self.camera_label.setText("opening the camera...")
        self._refresh_status()

        def work() -> SpotFocusService:
            svc = self._ensure_service()
            deadline = time.monotonic() + _OPEN_TIMEOUT_S
            while not svc.wait_ready(0.2):
                if svc.current_state == "fault":
                    raise RuntimeError("the camera could not be opened")
                if time.monotonic() > deadline:
                    raise RuntimeError("the camera did not become ready")
            svc.set_camera(
                exposure_ms=exposure, gain=gain, roi=roi, channel=channel, **det
            )
            svc.start_preview()
            try:
                svc.led_on(led, owner="preview")
            except Exception as err:
                logger.warning("spot focus led_on failed", exc_info=True)
                self._relay.ledFailed.emit(str(err) or type(err).__name__)
            self._report_crisp(svc)
            return svc

        self._run_async(
            "preview",
            work,
            lambda _: self._relay.previewStarted.emit(),
            self._relay.previewFailed.emit,
        )

    def _on_preview_started(self) -> None:
        self._preview_busy = False
        self.preview_button.setEnabled(True)
        self.preview_button.setChecked(True)
        svc = self.service
        if svc is None:
            return
        if self.state == "fault":
            self._set_state("idle")
        self._clear_problem(refresh=False)
        self._camera_missing = False
        self._crisp_present = svc.crisp_label is not None
        self._refresh_status()
        self._apply_camera_info(self._camera_info(svc))
        self.live_view.set_service(svc)
        self._show_live_view()

    def _on_preview_failed(self, message: str) -> None:
        self._preview_busy = False
        self.preview_button.setEnabled(True)
        self.preview_button.setChecked(False)
        self.camera_label.setText("no camera")
        self._camera_missing = True
        if self.state != "fault":  # a worker fault already says why
            self._set_problem("_camera_error", message)
        else:
            self._refresh_status()
        self._owner.dead = True  # build a fresh service on the next Preview

    def _stop_preview(self) -> None:
        if self._preview_busy:
            return
        if self.state == "running" or self._busy == "starting":
            # The lock needs the camera: leave it streaming.
            self.preview_button.setChecked(True)
            self._set_problem("_notice", "Preview stays on while the lock runs.")
            return
        self.preview_button.setChecked(False)
        svc = self.service
        if svc is not None:
            try:
                svc.stop_preview()
                svc.led_off("preview")
                self._report_crisp(svc)
            except Exception:
                logger.warning("spot focus stop_preview failed", exc_info=True)
        self.live_view.set_polling(False)
        self.live_view.clear()
        if self.live_view.isWindow():
            # hide(), not close(): no windowClosed echo. A docked view keeps its
            # space (cleared, not polled) so the layout does not jump.
            self.live_view.hide()
        self.camera_readout.setText("-")
        self.frame_stats_label.setText(_FRAME_STATS_IDLE)

    def _show_live_view(self) -> None:
        """Make the image visible (raise the floating window, or show the view)."""
        svc = self.service
        if svc is None:
            return
        view = self.live_view
        view.set_service(svc)
        view.set_polling(True)
        view.show()
        if view.isWindow():
            view.raise_()
            view.activateWindow()

    @property
    def view_floating(self) -> bool:
        """Whether the live view is popped out into its own window."""
        return self.live_view.isWindow()

    def set_view_floating(self, floating: bool) -> None:
        """Pop the live view out into its own window, or dock it back.

        Docked (the default) it sits in the panel's layout above the toolbar. The
        scene and zoom are kept either way. The choice is not persisted.

        Parameters
        ----------
        floating : bool
            ``True`` to float the view, ``False`` to dock it again.
        """
        view = self.live_view
        floating = bool(floating)
        if floating != view.isWindow():
            if floating:
                self._view_index = max(self._outer.indexOf(view), 0)
                self._outer.removeWidget(view)
                # A window owned by the panel: stays above the app, goes with it.
                view.setParent(self, Qt.WindowType.Window)
                view.resize(self._float_size)
                view.show()
                view.raise_()
                view.activateWindow()
            else:
                self._float_size = view.size()
                self._float_hidden = False
                # Drops the window flag (the layout would not; explicit flags because
                # setParent(parent) does nothing for an unchanged parent).
                view.setParent(self, Qt.WindowType.Widget)
                self._outer.insertWidget(self._view_index, view, 1)
                view.show()
        self.pop_out_button.blockSignals(True)
        self.pop_out_button.setChecked(floating)
        self.pop_out_button.blockSignals(False)
        self.pop_out_button.setText("Dock" if floating else "Pop out")

    def _on_view_closed(self) -> None:
        """The floating window was closed: dock the view again.

        Preview keeps running; closing a window is not a request to stop the camera.
        """
        self.set_view_floating(False)
        if self.live_view.isHidden():  # closed while docked: keep its space
            self.live_view.show()

    def hideEvent(self, event: Any) -> None:
        """Hide a floating live view with the panel (a closed dock, a tab switch)."""
        super().hideEvent(event)
        view = self.live_view
        if view.isWindow() and view.isVisible():
            view.hide()
            self._float_hidden = True

    def showEvent(self, event: Any) -> None:
        """Bring back a floating live view that went away with the panel."""
        super().showEvent(event)
        if self._float_hidden:
            self._float_hidden = False
            if self.live_view.isWindow():
                self.live_view.show()

    def _on_view_stats(self, fps: float, max_value: int, saturated_pct: float) -> None:
        sat = f"{saturated_pct:.2f}% saturated"
        if saturated_pct >= 1.0:
            sat = _colored(sat, ATTENTION_COLOR)
        else:
            sat = html.escape(sat)
        self.camera_readout.setText(f"{fps:.1f} fps | max {max_value} | {sat}")
        self.frame_stats_label.setText(
            f"Frame rate: {fps:.0f} fps   Maximum: {max_value}   "
            f"Saturated pixels: {saturated_pct:.1f} %"
        )

    @staticmethod
    def _camera_info(svc: SpotFocusService) -> dict:
        info = getattr(svc, "camera_info", None)
        return dict(info) if info else dict(svc.info)

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state
        if state != "running":
            self._tracking_error_um = None
        alert = state == "fault"
        text = self._busy or state
        self.state_label.setText(
            _colored(text, ALERT_COLOR) if alert else html.escape(text)
        )
        self._set_lock_active(
            state == "running" or self._busy in ("starting", "calibrating", "zeroing")
        )
        self._update_enabled()

    def _set_lock_active(self, active: bool) -> None:
        """Set :attr:`lock_active` and emit :attr:`lockActiveChanged` on a change."""
        if active != self.lock_active:
            self.lock_active = active
            self.lockActiveChanged.emit(active)

    # ------------------------------------------------------------------
    # status (the words and the one message line; see CRISPy.piezo_status)
    # ------------------------------------------------------------------

    _PROBLEM_FIELDS = (
        "_fault_reason",
        "_start_error",
        "_calibrate_error",
        "_zero_error",
        "_stop_error",
        "_camera_error",
        "_led_error",
        "_notice",
    )

    def _clear_problem(self, refresh: bool = True) -> None:
        """Forget every problem text (what ``fault_label.setText("")`` used to do)."""
        for field in self._PROBLEM_FIELDS:
            setattr(self, field, "")
        if refresh:
            self._refresh_status()

    def _set_problem(self, field: str, text: str) -> None:
        """Show *text* as the one problem: it replaces any other, like the old label."""
        keep = self._fault_reason if self.state == "fault" else ""
        self._clear_problem(refresh=False)
        if field != "_fault_reason":
            # A fault outranks every other text; keep its reason in the message.
            self._fault_reason = keep
        setattr(self, field, text)
        self._refresh_status()

    def _show_derived_hint(self) -> None:
        """Drop stale problems so the message line shows the derived hint.

        Lock / Calibrate refused (no edge box, detector mismatch): the line says why
        from the state. A fault is kept (the user acknowledges it with Stop).
        """
        if self.state == "fault":
            self._refresh_status()
        else:
            self._clear_problem()

    def status_inputs(self) -> piezo_status.SpotStatusInputs:
        """The panel's state as plain values for :mod:`CRISPy.piezo_status`."""
        cal = self._cal
        return piezo_status.SpotStatusInputs(
            lock_state=self.state,
            busy=self._busy,
            camera_open=self.preview_button.isChecked() or self.state == "running",
            camera_opening=self._preview_busy,
            camera_missing=self._camera_missing,
            camera_error=self._camera_error,
            crisp_present=self._crisp_present,
            led_on=self.led_button.isChecked(),
            led_error=self._led_error,
            edge_box_set=self._edge_box is not None,
            calibration_present=cal is not None,
            detector_mismatch=self._mismatch_text(),
            box_changed=self._box_changed(),
            fault_reason=self._fault_reason,
            start_error=self._start_error,
            calibrate_error=self._calibrate_error,
            zero_error=self._zero_error,
            stop_error=self._stop_error,
            no_new_frames=self.live_view.no_new_frames,
            mda_running=self._mda_running,
            tracking_error_um=self._tracking_error_um,
        )

    def _refresh_status(self) -> None:
        """Recompute the message line and tracking error, then emit statusChanged."""
        inputs = self.status_inputs()
        # A notice has no status field; a fault outranks it.
        if self._notice and piezo_status.lock_word(inputs) != piezo_status.LOCK_FAULT:
            text = self._notice
        else:
            text = piezo_status.message(inputs)
        if self.message_label.text() != text:
            self.message_label.setText(text)
        tracking = piezo_status.tracking_error_text(inputs)
        if self.tracking_error_label.text() != tracking:
            self.tracking_error_label.setText(tracking)
        self.statusChanged.emit()

    def _update_enabled(self) -> None:
        running = self.state == "running"
        idle = not self._busy and not running
        mismatch = self._mismatch_text()
        # Lock with no valid calibration calibrates first (needs an edge box).
        # A calibration made with another detector cannot be locked on.
        self.start_button.setEnabled(idle and not mismatch)
        self.detector_combo.setEnabled(not self._busy)
        self.detector_sigma_spin.setEnabled(
            not self._busy and self._detector_setting()[0] == "gaussderiv"
        )
        self.stop_button.setEnabled(running or self.state == "fault")
        self.calibrate_button.setEnabled(idle)
        # Zero re-zeroes the calibration's edges: only a one-edge calibration.
        self.zero_button.setEnabled(idle and self._single_edge(self._cal) is not None)
        self.draw_edge_button.setEnabled(idle)
        self.draw_edge2_button.setEnabled(False)  # one edge only (hidden)
        self.clear_edge2_button.setEnabled(False)
        if mismatch:
            self.start_button.setToolTip(mismatch)
        elif self._single_edge(self._cal) is None:
            # No calibration, or an old two-edge one: Lock sweeps first.
            self.start_button.setToolTip(
                "Calibrates first (focus on the cells, draw an edge box), then locks"
            )
        else:
            self.start_button.setToolTip("Hold the edge at its zero until Stop")
        self._refresh_status()

    def _on_state(self, state: str) -> None:
        if state == "preview":
            return  # the camera streams; the lock itself is not running
        if state in ("running", "stopped", "fault", "idle"):
            self._set_state(state)

    def _on_fault(self, reason: str) -> None:
        self._set_problem("_fault_reason", reason)
        if reason == "worker died":
            self._owner.dead = True
        self._set_state("fault")

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Lock (the Lock button); with no valid calibration, calibrate first.

        The user has focused on the cells by eye, so the calibration's centre P
        becomes the zero and the lock starts on success.
        """
        if self._busy or self.state == "running":
            return
        cfg = get_settings_store().settings
        # A calibration made with two edges is not used: calibrate again with one.
        cal = self._single_edge(cfg.calibration_status()[0])
        boxes = self._drawn_boxes(cfg)
        if cal is None and not boxes:
            self._show_derived_hint()  # the message line says NEED_BOX_TEXT
            return
        if self._mismatch_text():
            self._show_derived_hint()  # the message line gives the mismatch
            return
        params = cfg.params.to_params()
        sweep = self._sweep_kwargs(cfg)
        record = self._record_path()
        exposure, gain, led = cfg.exposure_ms, cfg.gain, self._led_value()
        roi, channel = cfg.roi or (0, 0, 0, 0), cfg.channel
        det: dict[str, Any] = {
            "detector": cfg.detector,
            "detector_sigma_px": cfg.detector_sigma_px,
        }
        self._clear_problem(refresh=False)
        self._telemetry = None
        self.chart.clear()
        self._busy = "starting"
        self._set_state("idle")

        def work() -> bool:
            svc = self._ensure_service()
            svc.wait_ready()
            svc.set_camera(
                exposure_ms=exposure, gain=gain, roi=roi, channel=channel, **det
            )
            use, loop = cal, params
            if use is None:
                if not boxes:
                    raise RuntimeError(NEED_BOX_TEXT)
                try:
                    use = svc.calibrate(boxes, record_path=record, **sweep)
                except Exception as err:
                    logger.warning("spot focus calibrate failed", exc_info=True)
                    self._relay.calibrateFailed.emit(str(err) or type(err).__name__)
                    return False
                loop = replace(params, offset_um=0.0)  # a new zero: no offset
                self._relay.calibrationUpdated.emit(use)
            try:
                svc.start(loop, use, led)
            except RuntimeError as err:
                # A calibration/detector mismatch is a refusal to show, not a
                # crash; anything else (a dead worker) rebuilds the service.
                if not detector_mismatch(use, *svc.detector_setting):
                    raise
                self._relay.startRefused.emit(str(err))
                return False
            self._report_crisp(svc)
            return True

        def done(ok: bool) -> None:
            if ok:
                self._relay.started.emit()

        self._run_async("start", work, done, self._relay.startFailed.emit)

    @staticmethod
    def _sweep_kwargs(cfg: Any) -> dict[str, Any]:
        """The saved sweep settings as ``SpotFocusService.calibrate`` keywords."""
        return {
            "span_um": float(cfg.sweep_span_um),
            "steps": int(cfg.sweep_steps),
            "n_frames": int(cfg.sweep_frames),
        }

    @staticmethod
    def _record_path() -> Path | None:
        """Where the sweep record (``.npz``) goes; ``None`` when the store has no folder."""
        data_dir = get_settings_store().data_dir
        if data_dir is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return data_dir / f"spot_calibration_{stamp}.npz"

    def _on_started(self) -> None:
        self._busy = ""
        svc = self.service
        if svc is not None:
            self.live_view.set_service(svc)
            if self.live_view.polling:  # the view was on: Preview stays checked
                self.preview_button.setChecked(True)
        # The service's own state signals may have beaten us here. Decide the state
        # first and set it once: an intermediate "preview"/"idle" would flip
        # lock_active to False for a moment (and the host would re-enable the wheel).
        state = svc.current_state if svc is not None else "idle"
        self._set_state(state if state in ("running", "fault") else "running")

    def _on_start_failed(self, message: str) -> None:
        self._busy = ""
        self._set_problem("_start_error", message)
        self._owner.dead = True  # build a fresh service next time
        self._set_state("idle")

    def _on_start_refused(self, message: str) -> None:
        self._busy = ""
        self._set_problem("_notice", message)
        self._set_state("idle")

    def stop(self) -> None:
        """Stop the loop and restore CRISP (the Stop button). Never moves P."""
        svc = self.service
        if svc is not None:
            try:
                svc.stop()
                self._report_crisp(svc)
            except Exception as err:
                logger.warning("spot focus stop failed", exc_info=True)
                self._set_problem("_stop_error", str(err))
        # After a fault the loop is already silent; Stop acknowledges it.
        if self.state == "fault":
            self._clear_problem(refresh=False)
        self._notice = ""
        self._set_state("stopped")
        # While locked the worker measured the calibration's boxes: show the drawn
        # ones, with the calibration for the preview error.
        self._send_preview_state(svc)

    # ------------------------------------------------------------------
    # calibrate
    # ------------------------------------------------------------------

    def calibrate(self) -> None:
        """Sweep P around its present value and fit the edge (Calibrate button).

        The user has focused on the cells, so the present P becomes the zero.
        Needs an edge box; with a second box both edges are calibrated (an edge
        that cannot be fitted is dropped with a warning).
        """
        if self._busy or self.state == "running":
            return
        cfg = get_settings_store().settings
        boxes = self._drawn_boxes(cfg)
        if not boxes:
            self._show_derived_hint()  # the message line says NEED_BOX_TEXT
            return
        sweep = self._sweep_kwargs(cfg)
        record = self._record_path()
        exposure, gain, channel = cfg.exposure_ms, cfg.gain, cfg.channel
        roi = cfg.roi or (0, 0, 0, 0)
        det: dict[str, Any] = {
            "detector": cfg.detector,
            "detector_sigma_px": cfg.detector_sigma_px,
        }
        self._clear_problem(refresh=False)
        self._busy = "calibrating"
        self._set_state(self.state)

        def work() -> FocusCalibration:
            svc = self._ensure_service()
            svc.wait_ready()
            svc.set_camera(
                exposure_ms=exposure, gain=gain, roi=roi, channel=channel, **det
            )
            return svc.calibrate(boxes, record_path=record, **sweep)

        self._run_async(
            "calibrate",
            work,
            self._relay.calibrated.emit,
            self._relay.calibrateFailed.emit,
        )

    def zero(self) -> None:
        """Make the edge position now the zero, keeping the slope (Zero here)."""
        cal = self._cal
        if self._busy or self.state == "running" or self._single_edge(cal) is None:
            return
        cfg = get_settings_store().settings
        exposure, gain, channel = cfg.exposure_ms, cfg.gain, cfg.channel
        roi = cfg.roi or (0, 0, 0, 0)
        self._clear_problem(refresh=False)
        self._busy = "zeroing"
        self._set_state(self.state)

        def work() -> FocusCalibration:
            svc = self._ensure_service()
            svc.wait_ready()
            svc.set_camera(exposure_ms=exposure, gain=gain, roi=roi, channel=channel)
            return svc.zero(cal)

        self._run_async(
            "zero",
            work,
            self._relay.calibrated.emit,
            self._relay.zeroFailed.emit,
        )

    def _apply_calibration(self, cal: FocusCalibration) -> None:
        """Keep *cal* (a new calibration or zero): save it, reset the offset."""
        store = get_settings_store()
        store.settings.calibration = cal.to_dict()
        store.save()
        self._cal, self._cal_message = cal, ""
        self._reset_offset()
        self._place_zero_marker()
        self._refresh_calibration()
        self._update_enabled()
        # The preview shows the error from the new calibration (the lock keeps
        # the one it started with; Stop sends it again).
        if self.state != "running" and self._busy != "starting":
            self._send_preview_state()

    def _on_calibrated(self, cal: FocusCalibration) -> None:
        self._busy = ""
        self._apply_calibration(cal)
        self._set_state(self.state)

    def _on_calibrate_failed(self, message: str) -> None:
        self._busy = ""
        self._set_problem("_calibrate_error", message)
        self._set_state(self.state)

    def _on_zero_failed(self, message: str) -> None:
        self._busy = ""
        self._set_problem("_zero_error", message)
        self._set_state(self.state)

    # ------------------------------------------------------------------
    # live data
    # ------------------------------------------------------------------

    def _on_telemetry(self, msg: dict) -> None:
        self._telemetry = msg
        along, edge = msg.get("along_um"), msg.get("edge_px")
        contrast = msg.get("contrast")
        if along is not None and msg.get("p_cmd_um") is not None:
            self.chart.add(
                float(msg.get("t") or time.monotonic()),
                float(along),
                float(msg["p_cmd_um"]),
            )
        labels = self.readout_labels
        # One edge only: edge 2, the disagreement and the held edge are not shown.
        labels["error"].setText("—" if along is None else f"{along:+.3f} µm")
        labels["edge"].setText("-" if edge is None else self._edge_text(0, edge))
        self.live_view.set_secondary_point(None)
        err = None if along is None else float(along)
        if err != self._tracking_error_um:
            self._tracking_error_um = err
            if (
                piezo_status.tracking_error_text(self.status_inputs())
                != self.tracking_error_label.text()
            ):
                self._refresh_status()
        note = msg.get("note") or (None if along is not None else msg.get("reason"))
        self.note_label.setText(_colored(str(note), ATTENTION_COLOR) if note else "")
        labels["contrast"].setText("-" if contrast is None else f"{contrast:.0f}")
        labels["fps"].setText(f"{msg.get('fps') or 0.0:.1f}")

    def _edge_text(self, index: int, edge_px: float) -> str:
        """The live position of edge *index* with its calibrated noise, coloured."""
        text = f"{edge_px:.2f} px"
        cal = self._cal
        if cal is None or index >= len(cal.edges):
            return text
        noise = edge_noise_um(cal.edges[index])
        return f"{text} {_colored(f'(noise {noise:.3f} µm)', noise_color(noise))}"

    def _refresh_stats(self) -> None:
        # Once a second: read the host's MDA flag (not per paint) and re-evaluate.
        try:
            self._mda_running = self._mmc.mda.is_running() is True
        except Exception:
            self._mda_running = False
        self._refresh_status()
        svc = self.service
        if svc is None:
            return
        st = svc.stats()
        self.readout_labels["moves"].setText(
            f"{st['moves_per_min']:.1f} ({st['n_moves']} total)"
        )
        self.readout_labels["move_ms"].setText(
            f"{st['mean_dt_ms']:.1f} / {st['max_dt_ms']:.1f} ms"
        )
