"""Qt UI for a single ASI CRISP autofocus axis.

Maps the ASI CRISP feature set (see https://asiimaging.com/docs/crisp_manual) onto
the ASITiger device-adapter properties:

* operation       -> the settable ``CRISP State`` values (Idle / Ready / Lock /
                     Reset Focus Offset / Save to Controller)
* calibration     -> ``loG_cal`` / ``Dither`` / ``gain_Cal``
* live readouts   -> read-only ``Signal Noise Ratio`` / ``Dither Error`` / ``Sum`` /
                     ``LogAmpAGC`` / ``Lock Offset`` / ``Calibration Gain`` /
                     ``Calibration Range(um)`` (polled; the adapter does not emit
                     ``propertyChanged`` for these sensor values)
* derived metrics -> actuator-type badge, error *sensitivity* (counts/µm) from the
                     calibration, the focus error expressed in microns, and a
                     rolling *fluctuation* (std-dev / peak-to-peak) of the error
* fast tune       -> a separate high-rate sampler (SNR / Sum / Dither Error only) with
                     a short rolling window and peak-hold, for turning the CRISP
                     detector knob (see :mod:`CRISPy.tuning`)
* parameters      -> reused ``pymmcore_widgets.PropertyWidget`` controls
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .controller import CAL_GAIN_PROP, CAL_RANGE_PROP, counts_per_um
from .tuning import DEFAULT_WINDOW_S, TuneSample, TuneSampler, TuneTracker

if TYPE_CHECKING:
    from pymmcore_widgets.control._async_poller import AsyncPoller

    from .controller import CrispController

_PARAM_MAX_WIDTH = 190  # keep slider+value controls compact / out of the way

_POLL_INTERVAL_MS = 750
_SNR_GOOD_DB = 4.0  # ASI manual target

# The live focus-error readout we express in physical units and track for
# fluctuation. It is raw controller counts; ``sensitivity`` (counts/µm) converts it.
_ERROR_PROP = "Dither Error"
# Rolling window for the fluctuation metric (~15 s at the default 750 ms poll).
_STABILITY_WINDOW = 20

# Fast tune mode: repaint the labels/meter at ~20 Hz however fast samples arrive.
_TUNE_PAINT_S = 0.05
# Tune shows "no data" if no sample has arrived for this long (link down, sampler
# ended), instead of leaving the last values on screen looking live.
_TUNE_STALE_S = 1.0
_TUNE_WATCHDOG_MS = 500
# The normal readouts hold their last values while the slow poller is paused for Tune.
_READOUTS_TITLE = "Readouts"
_READOUTS_PAUSED_TITLE = "Readouts  (paused while tuning)"
# Tuning meter colour bands, as (minimum fraction of the peak, colour).
_METER_BANDS = ((0.95, "#4caf50"), (0.80, "#ff9800"), (0.0, "#f44336"))

# Read-only telemetry props, in display order: (property, label). "Calibration
# Gain"/"Range" are the per-axis error->motion scaling (differs piezo vs stage);
# surfacing them answers "is the piezo error properly scaled?".
_READOUTS: list[tuple[str, str]] = [
    ("Signal Noise Ratio", "SNR"),
    ("Dither Error", "Dither Err"),
    ("Sum", "Sum"),
    ("LogAmpAGC", "AGC"),
    ("Lock Offset", "Lock Offset"),
    ("Calibration Gain", "Cal Gain"),
    ("Calibration Range(um)", "Cal Range (µm)"),
]

# Actuator badge styling: (background, foreground).
_ACTUATOR_BADGE: dict[str, tuple[str, str]] = {
    "piezo": ("#6a1b9a", "#ffffff"),
    "stage": ("#00695c", "#ffffff"),
    "unknown": ("#616161", "#ffffff"),
}

# Adjustable parameters exposed via pymmcore-widgets: (property, friendly label).
_PARAMS: list[tuple[str, str]] = [
    ("LED Intensity", "LED Intensity (%)"),
    ("GainMultiplier", "Loop Gain"),
    ("Objective NA", "Objective NA"),
    ("Number of Averages", "Averages"),
    ("Max Lock Range(mm)", "Lock Range (mm)"),
    ("In Focus Range(um)", "In-Focus Range (µm)"),
    ("Number of Skips", "Update Skips"),
    ("Wait ms after Lock", "Wait after Lock (ms)"),
]

_LOCKED_STATES = {"Lock", "In Focus"}
_BUSY_STATES = {"loG_cal": "Log Cal…", "Dither": "Dither…", "gain_Cal": "Gain Cal…"}
_BAD_STATES = {"Error", "Inhibit", "Dim"}
_GOOD_STATES = {"Lock", "In Focus", "Ready"}


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


@dataclass(frozen=True)
class CrispSnapshot:
    """A plain snapshot of CRISP telemetry, read off the GUI thread.

    Produced on a worker thread by :meth:`CrispControlPanel._read_device_values` and
    consumed on the GUI thread by :meth:`CrispControlPanel._apply_values`, so it holds
    only raw values (no Qt widgets).
    """

    state: str
    sensitivity: float | None
    readouts: dict[str, str]


class CrispControlPanel(QWidget):
    """Control + telemetry panel for a single CRISP axis."""

    # Carries each fast-tune sample from the sampler's worker thread to the GUI thread
    # (emitting from a non-GUI thread makes Qt queue the delivery).
    _tune_sample = Signal(object)

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
        # Cached error->µm scaling and a rolling history for the fluctuation metric.
        self._sensitivity: float | None = None
        self._error_history: deque[float] = deque(maxlen=_STABILITY_WINDOW)
        # Edge-detector for the auto joystick lockout: starts False so a state
        # already locked when this panel first opens is treated as a lock edge
        # (joystick gets disabled), matching the "always safe on lock" default.
        self._was_locked = False

        # Fast tune mode (see CRISPy.tuning). While tuning, the slow poller is paused
        # so that only one reader uses the Tiger serial port.
        self._tracker = TuneTracker(DEFAULT_WINDOW_S)
        self._tune_sampler: TuneSampler | None = None
        self._tuning = False
        self._last_tune_paint = 0.0
        self._last_tune_sample = 0.0
        self._tune_meter_color = ""
        self._tune_watchdog = QTimer(self)
        self._tune_watchdog.setInterval(_TUNE_WATCHDOG_MS)
        self._tune_watchdog.timeout.connect(self._check_tune_stale)

        # The ASITiger adapter exposes "RefreshPropertyValues" to force a hardware
        # re-read; check once so we don't spam errors on devices that lack it.
        self._can_refresh = False
        with suppress(Exception):
            self._can_refresh = self.mmcore.hasProperty(
                self.controller.label, "RefreshPropertyValues"
            )

        self._build()
        self._tune_sample.connect(self._on_tune_sample)

        # Poll telemetry off the GUI thread so the ~10 blocking serial reads per
        # tick never stall the event loop / live view. See AsyncPoller. Imported
        # here (not at module scope) to keep ``import CRISPy.ui`` lightweight.
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
            "Periodically read SNR / Dither / State from the controller.\n"
            "Polling pauses automatically while an acquisition is running."
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
        ro_box = self.readouts_box = QGroupBox(_READOUTS_TITLE)
        grid = QGridLayout(ro_box)
        for i, (prop, label) in enumerate(_READOUTS):
            row, col = divmod(i, 2)
            name = QLabel(f"{label}:")
            val = QLabel("--")
            val.setStyleSheet("font-weight: bold;")
            grid.addWidget(name, row, col * 2)
            grid.addWidget(val, row, col * 2 + 1)
            self._readouts[prop] = val

        # Derived, full-width rows: physical scaling and fluctuation of the error.
        next_row = (len(_READOUTS) + 1) // 2
        self.sensitivity_lbl = QLabel("Sensitivity: --")
        self.sensitivity_lbl.setToolTip(
            "Error counts per micron of defocus (|Calibration Gain| / range).\n"
            "This is the actuator-specific scaling; piezo and stage differ, so raw\n"
            "error numbers are only comparable once converted to microns."
        )
        grid.addWidget(self.sensitivity_lbl, next_row, 0, 1, 4)
        self.stability_lbl = QLabel("Fluctuation: collecting…")
        self.stability_lbl.setToolTip(
            f"Rolling std-dev / peak-to-peak of {_ERROR_PROP} over the last "
            f"{_STABILITY_WINDOW} polls."
        )
        grid.addWidget(self.stability_lbl, next_row + 1, 0, 1, 4)
        layout.addWidget(ro_box)

        layout.addWidget(self._build_tune_box())

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
        self.btn_save.clicked.connect(self._wrap(self.controller.set_save))
        op.addWidget(self.btn_idle_ready, 0, 0)
        op.addWidget(self.btn_lock, 0, 1)
        op.addWidget(self.btn_offset, 1, 0)
        op.addWidget(self.btn_save, 1, 1)
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
        op.addWidget(self.joystick_chk, 2, 0, 1, 2)
        layout.addWidget(op_box)

        # --- calibration --------------------------------------------------
        cal_box = QGroupBox("Calibration  (1. Log Cal → 2. Dither → 3. Set Gain)")
        cal = QHBoxLayout(cal_box)
        self.btn_log = QPushButton("1. Log Cal")
        self.btn_log.clicked.connect(self._wrap(self.controller.set_log_cal))
        self.btn_dither = QPushButton("2. Dither")
        self.btn_dither.clicked.connect(self._wrap(self.controller.set_dither))
        self.btn_gain = QPushButton("3. Set Gain")
        self.btn_gain.clicked.connect(self._wrap(self.controller.set_gain_cal))
        for b in (self.btn_log, self.btn_dither, self.btn_gain):
            cal.addWidget(b)
        layout.addWidget(cal_box)

        # --- parameters (reused pymmcore-widgets property controls) -------
        param_box = QGroupBox("Parameters")
        form = QFormLayout(param_box)
        self._add_parameter_widgets(form)
        layout.addWidget(param_box)

        layout.addStretch()

    def _build_tune_box(self) -> QGroupBox:
        """Fast sampling + peak-hold, for turning the CRISP detector knob."""
        box = QGroupBox("Tune  (fast, for the detector knob)")
        grid = QGridLayout(box)

        self.tune_btn = QPushButton("Start Tune")
        self.tune_btn.setCheckable(True)
        self.tune_btn.setToolTip(
            "Sample SNR / Sum / Dither Error as fast as the controller allows and\n"
            "hold the best value seen. Sweep the knob, note the peak, then back up\n"
            "until the live value matches it. Needs the LED on (Ready or Lock).\n\n"
            "Normal polling is paused while tuning. Tune stops when this panel is\n"
            "hidden or an acquisition starts.\n\n"
            "The controller's own Number of Averages / Update Skips limit how often\n"
            "these values can change; lower them if the rate is still too slow."
        )
        self.tune_btn.toggled.connect(self._on_tune_toggled)
        self.tune_reset_btn = QPushButton("Reset peak")
        self.tune_reset_btn.setToolTip(
            "Forget the best values and start a fresh sweep."
        )
        self.tune_reset_btn.clicked.connect(self._on_tune_reset)
        grid.addWidget(self.tune_btn, 0, 0)
        grid.addWidget(self.tune_reset_btn, 0, 1)

        opts = QHBoxLayout()
        self.tune_window = QDoubleSpinBox()
        self.tune_window.setRange(0.1, 5.0)
        self.tune_window.setSingleStep(0.1)
        self.tune_window.setDecimals(1)
        self.tune_window.setValue(DEFAULT_WINDOW_S)
        self.tune_window.setSuffix(" s")
        self.tune_window.setToolTip(
            "Averaging window for SNR / Sum / fluctuation. Shorter follows a fast\n"
            "knob turn more closely but is noisier; the lag is about half of it."
        )
        self.tune_window.valueChanged.connect(self._on_tune_window_changed)
        self.tune_metric = QComboBox()
        for text, key in (("SNR", "snr"), ("Sum", "sum"), ("Stability", "stability")):
            self.tune_metric.addItem(text, key)
        self.tune_metric.setToolTip("Which metric the meter follows.")
        self.tune_metric.currentIndexChanged.connect(lambda _i: self._paint_tune(True))
        opts.addWidget(QLabel("Window:"))
        opts.addWidget(self.tune_window)
        opts.addWidget(QLabel("Meter:"))
        opts.addWidget(self.tune_metric)
        opts.addStretch()
        grid.addLayout(opts, 1, 0, 1, 2)

        self.tune_meter = QProgressBar()
        self.tune_meter.setRange(0, 100)
        self.tune_meter.setToolTip(
            "Live value as a fraction of the best seen (100% = at the peak)."
        )
        grid.addWidget(self.tune_meter, 2, 0, 1, 2)

        self.tune_snr_lbl = QLabel()
        self.tune_sum_lbl = QLabel()
        self.tune_fluct_lbl = QLabel()
        self.tune_rate_lbl = QLabel()
        for row, lbl in enumerate(
            (
                self.tune_snr_lbl,
                self.tune_sum_lbl,
                self.tune_fluct_lbl,
                self.tune_rate_lbl,
            ),
            start=3,
        ):
            lbl.setStyleSheet("font-weight: bold;")
            grid.addWidget(lbl, row, 0, 1, 2)
        self._paint_tune(force=True)
        return box

    def _make_actuator_badge(self) -> QLabel:
        actuator = self.controller.actuator_type
        text = {"piezo": "Piezo", "stage": "Stage"}.get(actuator, "Actuator ?")
        bg, fg = _ACTUATOR_BADGE.get(actuator, _ACTUATOR_BADGE["unknown"])
        badge = QLabel(text)
        badge.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 6px;"
            " padding: 1px 6px; font-weight: bold; font-size: 11px;"
        )
        tip = f"Paired focus actuator: {text}."
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

    def _to_um(self, counts: float) -> float | None:
        """Convert raw error counts to microns via the cached sensitivity."""
        sens = self._sensitivity
        if not sens:  # None or 0
            return None
        return counts / sens

    def _add_parameter_widgets(self, form: QFormLayout) -> None:
        # Imported lazily so the module still imports without a Qt app / in demos.
        from pymmcore_widgets import PropertyWidget

        # Keep the value controls at their natural (compact) size instead of
        # stretching the sliders across the whole panel, which would otherwise
        # require a very wide dock and clip the value box on the right.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        label = self.controller.label
        for prop, friendly in _PARAMS:
            try:
                if not self.mmcore.hasProperty(label, prop):
                    continue
                wdg = PropertyWidget(label, prop, mmcore=self.mmcore)
            except Exception:
                continue
            wdg.setMaximumWidth(_PARAM_MAX_WIDTH)
            form.addRow(friendly, wdg)

    def _wrap(self, fn):
        """Return a slot that runs *fn* then refreshes telemetry."""

        def _slot(*_args) -> None:
            with suppress(Exception):
                fn()
            self._poller.poll_now(force=True)

        return _slot

    # -------------------------------------------------------------- handlers
    def _on_idle_ready(self) -> None:
        if self.controller.get_state() == "Idle":
            with suppress(Exception):
                self.controller.set_ready()
        else:
            with suppress(Exception):
                self.controller.set_idle()
        self._poller.poll_now(force=True)

    def _on_lock(self) -> None:
        if self.controller.get_state() in _LOCKED_STATES:
            with suppress(Exception):
                self.controller.set_unlock()
        else:
            with suppress(Exception):
                self.controller.set_lock()
        self._poller.poll_now(force=True)

    def _on_joystick_toggled(self, checked: bool) -> None:
        """Manual joystick override; independent of the auto lock/unlock logic."""
        with suppress(Exception):
            self.controller.set_joystick_enabled(checked)

    # --------------------------------------------------------- poll controls
    def _on_poll_toggled(self, checked: bool) -> None:
        if checked:
            # While tuning, the slow poller stays paused; it resumes when Tune stops.
            if not self._tuning:
                self._poller.start()
                self._poller.poll_now(force=True)
        else:
            self._poller.stop()

    def _on_interval_changed(self, value: int) -> None:
        self._poller.set_interval(int(value))

    def _on_mda_started(self, *_args) -> None:
        self._stop_tune(resume_poll=False)
        with suppress(RuntimeError):
            self._poller.stop()

    def _on_mda_finished(self, *_args) -> None:
        with suppress(RuntimeError):
            if self.poll_chk.isChecked() and not self._tuning:
                self._poller.start()

    # ------------------------------------------------------------------ tune
    def _on_tune_toggled(self, checked: bool) -> None:
        if checked:
            self._start_tune()
        else:
            self._stop_tune()

    def _start_tune(self) -> None:
        if self._tuning:
            return
        self._tuning = True
        self.readouts_box.setTitle(_READOUTS_PAUSED_TITLE)
        with suppress(RuntimeError):
            self._poller.stop()  # one reader on the serial port at a time
        self._tracker.reset()
        self._paint_tune(force=True)
        self._last_tune_sample = time.monotonic()
        self._tune_watchdog.start()
        self._tune_sampler = TuneSampler(
            self.mmcore,
            self.controller.label,
            self._tune_sample.emit,
            refresh=self._can_refresh,
        )
        self._tune_sampler.start()
        self.tune_btn.setText("Stop Tune")

    def _stop_tune(self, *, resume_poll: bool = True) -> None:
        if not self._tuning:
            return
        self._tuning = False
        self._tune_watchdog.stop()
        self.readouts_box.setTitle(_READOUTS_TITLE)
        if self._tune_sampler is not None:
            self._tune_sampler.stop()
            self._tune_sampler = None
        # Sync the button when we were stopped from elsewhere (hide, MDA start).
        self.tune_btn.blockSignals(True)
        self.tune_btn.setChecked(False)
        self.tune_btn.blockSignals(False)
        self.tune_btn.setText("Start Tune")
        if resume_poll and self.poll_chk.isChecked():
            with suppress(RuntimeError):
                self._poller.start()
                if self.isVisible():
                    self._poller.poll_now(force=True)

    def _on_tune_sample(self, sample: TuneSample) -> None:
        """Take one sample from the sampler thread (runs on the GUI thread)."""
        if not self._tuning:  # a straggler queued before Tune was stopped
            return
        self._last_tune_sample = time.monotonic()
        self._tracker.add(sample)
        self._paint_tune()

    def _check_tune_stale(self) -> None:
        if self._tuning and time.monotonic() - self._last_tune_sample > _TUNE_STALE_S:
            self.tune_rate_lbl.setText("Rate: no data from the controller")

    def _on_tune_reset(self) -> None:
        self._tracker.reset()
        self._paint_tune(force=True)

    def _on_tune_window_changed(self, seconds: float) -> None:
        self._tracker.set_window(float(seconds))

    def _paint_tune(self, force: bool = False) -> None:
        """Refresh the Tune labels and meter, at most ~20 Hz unless *force*."""
        now = time.monotonic()
        if not force and now - self._last_tune_paint < _TUNE_PAINT_S:
            return
        self._last_tune_paint = now
        s = self._tracker.stats

        def val(v: float | None, spec: str) -> str:
            return "--" if v is None else format(v, spec)

        self.tune_snr_lbl.setText(
            f"SNR: {val(s.snr, '.2f')} dB    peak {val(s.best_snr, '.2f')}"
        )
        self.tune_sum_lbl.setText(
            f"Sum: {val(s.signal_sum, '.1f')}    peak {val(s.best_signal_sum, '.1f')}"
        )
        fluct = f"Fluctuation: ± {val(s.error_std, '.1f')} cts"
        std_um = None if s.error_std is None else self._to_um(s.error_std)
        if std_um is not None:
            fluct += f" ({abs(std_um):.3f} µm)"
        self.tune_fluct_lbl.setText(f"{fluct}    best {val(s.best_error_std, '.1f')}")
        self.tune_rate_lbl.setText(f"Rate: {val(s.rate_hz, '.0f')} Hz")

        frac = self._tracker.fraction(self.tune_metric.currentData())
        self.tune_meter.setValue(0 if frac is None else round(frac * 100))
        self.tune_meter.setFormat("--" if frac is None else "%p%")
        color = "#9e9e9e"
        if frac is not None:
            color = next(c for floor, c in _METER_BANDS if frac >= floor)
        if color != self._tune_meter_color:  # restyle only when the band changes
            self._tune_meter_color = color
            self.tune_meter.setStyleSheet(
                f"QProgressBar::chunk {{ background: {color}; }}"
                " QProgressBar { text-align: center; }"
            )

    def hideEvent(self, event) -> None:
        # Don't keep hammering the serial port for a panel nobody is looking at.
        self._stop_tune()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._stop_tune(resume_poll=False)
        super().closeEvent(event)

    # ------------------------------------------------------------- telemetry
    def _on_reset_offset(self) -> None:
        with suppress(Exception):
            self.controller.set_reset_offset()
        # The error datum just moved; drop stale fluctuation samples.
        self._error_history.clear()
        self._tracker.clear_error()
        self._paint_tune(force=True)
        self._poller.poll_now(force=True)

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
            return CrispSnapshot(state="", sensitivity=None, readouts={})
        # Force the adapter to re-query the controller for fresh sensor values.
        if self._can_refresh:
            with suppress(Exception):
                self.mmcore.setProperty(label, "RefreshPropertyValues", "Yes")

        state = ""
        with suppress(Exception):
            state = str(self.mmcore.getProperty(label, "CRISP State"))

        readouts: dict[str, str] = {}
        for prop, _ in _READOUTS:
            with suppress(Exception):
                readouts[prop] = str(self.mmcore.getProperty(label, prop))

        # Calibration Gain / Range are already among the readouts, so derive the
        # sensitivity from them rather than reading both again every tick.
        sensitivity = counts_per_um(
            _to_float(readouts.get(CAL_GAIN_PROP)),
            _to_float(readouts.get(CAL_RANGE_PROP)),
        )

        return CrispSnapshot(state=state, sensitivity=sensitivity, readouts=readouts)

    def _apply_values(self, snap: CrispSnapshot) -> None:
        """Apply a telemetry *snap* to the widgets (runs on the GUI thread)."""
        self._update_state(snap.state)
        self._update_sensitivity(snap.sensitivity)
        for prop, _ in _READOUTS:
            if prop in snap.readouts:
                self._update_readout(prop, snap.readouts[prop])
        self._update_stability()

    def _update_sensitivity(self, new: float | None) -> None:
        if not _close(new, self._sensitivity):
            # First read or a recalibration: prior samples are in a different
            # scale, so start the fluctuation window fresh.
            self._error_history.clear()
        self._sensitivity = new
        if new is None:
            self.sensitivity_lbl.setText("Sensitivity: -- (uncalibrated)")
        else:
            self.sensitivity_lbl.setText(f"Sensitivity: {new:.0f} counts/µm")

    def _update_readout(self, prop: str, value: str) -> None:
        lbl = self._readouts.get(prop)
        if lbl is None:
            return
        if prop == "Signal Noise Ratio":
            try:
                snr = float(value)
            except ValueError:
                lbl.setText(str(value))
                return
            lbl.setText(f"{snr:.2f} dB")
            color = "#4caf50" if snr >= _SNR_GOOD_DB else "#f44336"
            lbl.setStyleSheet(f"font-weight: bold; color: {color};")
        elif prop == _ERROR_PROP:
            self._update_error_readout(lbl, value)
        else:
            lbl.setText(str(value))

    def _update_error_readout(self, lbl: QLabel, value: str) -> None:
        try:
            counts = float(value)
        except (TypeError, ValueError):
            lbl.setText(str(value))
            return
        self._error_history.append(counts)
        text = f"{counts:g}"
        um = self._to_um(counts)
        if um is not None:
            text += f"  ({um:+.3f} µm)"
        lbl.setText(text)

    def _update_stability(self) -> None:
        if len(self._error_history) < 2:
            self.stability_lbl.setText("Fluctuation: collecting…")
            return
        std = statistics.pstdev(self._error_history)
        ptp = max(self._error_history) - min(self._error_history)
        std_um = self._to_um(std)
        if std_um is None:
            self.stability_lbl.setText(
                f"Fluctuation: ± {std:.0f} cts rms · pk-pk {ptp:.0f} cts"
            )
        else:
            self.stability_lbl.setText(
                f"Fluctuation: ± {std:.0f} cts ({abs(std_um):.3f} µm) rms · "
                f"pk-pk {ptp:.0f} cts"
            )

    def _update_state(self, state: str) -> None:
        if state in _BUSY_STATES:
            text, color = _BUSY_STATES[state], "#FF9800"
        elif state in _GOOD_STATES:
            text, color = state, "#4caf50"
        elif state in _BAD_STATES:
            text, color = state, "#f44336"
        else:
            text, color = (state or "--"), "#9e9e9e"
        self.state_lbl.setText(f"State: {text}")
        self.state_lbl.setStyleSheet(f"font-weight: bold; color: {color};")

        # Reflect live state on the toggle buttons.
        led_on = bool(state) and state != "Idle"
        self.btn_idle_ready.setText("Set Idle (LED off)" if led_on else "Set Ready")
        locked = state in _LOCKED_STATES
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
