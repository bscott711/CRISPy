"""Qt UI for a single ASI CRISP autofocus axis.

Maps the ASI CRISP feature set (see https://asiimaging.com/docs/crisp_manual) onto
the ASITiger device-adapter properties:

* operation       -> the settable ``CRISP State`` values (Idle / Ready / Lock /
                     Reset Focus Offset / Save to Controller)
* calibration     -> ``loG_cal`` / ``Dither`` / ``gain_Cal``
* live readouts   -> read-only ``Signal Noise Ratio`` / ``Dither Error`` / ``Sum`` /
                     ``LogAmpAGC`` / ``Lock Offset`` (polled; the adapter does not
                     emit ``propertyChanged`` for these sensor values)
* parameters      -> reused ``pymmcore_widgets.PropertyWidget`` controls
"""

from __future__ import annotations

from contextlib import suppress

from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_PARAM_MAX_WIDTH = 190  # keep slider+value controls compact / out of the way

from .controller import CrispController

_POLL_INTERVAL_MS = 750
_SNR_GOOD_DB = 4.0  # ASI manual target

# Read-only telemetry props, in display order: (property, label).
_READOUTS: list[tuple[str, str]] = [
    ("Signal Noise Ratio", "SNR"),
    ("Dither Error", "Dither Err"),
    ("Sum", "Sum"),
    ("LogAmpAGC", "AGC"),
    ("Lock Offset", "Lock Offset"),
]

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


class CrispControlPanel(QWidget):
    """Control + telemetry panel for a single CRISP axis."""

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

        # The ASITiger adapter exposes "RefreshPropertyValues" to force a hardware
        # re-read; check once so we don't spam errors on devices that lack it.
        self._can_refresh = False
        with suppress(Exception):
            self._can_refresh = self.mmcore.hasProperty(
                self.controller.label, "RefreshPropertyValues"
            )

        self._build()
        self._read_and_update()

        self._timer = QTimer(self)
        self._timer.setInterval(self.poll_interval.value())
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        # Pause polling automatically during an MDA so the Tiger serial port
        # isn't loaded while acquiring (the lock itself is held by the card).
        with suppress(Exception):
            self.mmcore.mda.events.sequenceStarted.connect(self._on_mda_started)
            self.mmcore.mda.events.sequenceFinished.connect(self._on_mda_finished)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # --- header: device + current state -------------------------------
        header = QHBoxLayout()
        title = QLabel(self.controller.label)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.state_lbl = QLabel("State: --")
        self.state_lbl.setStyleSheet("font-weight: bold;")
        self.state_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(title)
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
        ro_box = QGroupBox("Readouts")
        grid = QGridLayout(ro_box)
        for i, (prop, label) in enumerate(_READOUTS):
            row, col = divmod(i, 2)
            name = QLabel(f"{label}:")
            val = QLabel("--")
            val.setStyleSheet("font-weight: bold;")
            grid.addWidget(name, row, col * 2)
            grid.addWidget(val, row, col * 2 + 1)
            self._readouts[prop] = val
        layout.addWidget(ro_box)

        # --- operation (2x2 grid to stay compact) -------------------------
        op_box = QGroupBox("Operation")
        op = QGridLayout(op_box)
        self.btn_idle_ready = QPushButton("Set Ready")
        self.btn_idle_ready.clicked.connect(self._on_idle_ready)
        self.btn_lock = QPushButton("Lock")
        self.btn_lock.clicked.connect(self._on_lock)
        self.btn_offset = QPushButton("Set Offset")
        self.btn_offset.clicked.connect(self._wrap(self.controller.set_reset_offset))
        self.btn_save = QPushButton("Save to Controller")
        self.btn_save.clicked.connect(self._wrap(self.controller.set_save))
        op.addWidget(self.btn_idle_ready, 0, 0)
        op.addWidget(self.btn_lock, 0, 1)
        op.addWidget(self.btn_offset, 1, 0)
        op.addWidget(self.btn_save, 1, 1)
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
            self._read_and_update()

        return _slot

    # -------------------------------------------------------------- handlers
    def _on_idle_ready(self) -> None:
        if self.controller.get_state() == "Idle":
            with suppress(Exception):
                self.controller.set_ready()
        else:
            with suppress(Exception):
                self.controller.set_idle()
        self._read_and_update()

    def _on_lock(self) -> None:
        if self.controller.get_state() in _LOCKED_STATES:
            with suppress(Exception):
                self.controller.set_unlock()
        else:
            with suppress(Exception):
                self.controller.set_lock()
        self._read_and_update()

    # --------------------------------------------------------- poll controls
    def _on_poll_toggled(self, checked: bool) -> None:
        if checked:
            self._timer.start()
            self._read_and_update()
        else:
            self._timer.stop()

    def _on_interval_changed(self, value: int) -> None:
        self._timer.setInterval(int(value))

    def _on_mda_started(self, *_args) -> None:
        with suppress(RuntimeError):
            self._timer.stop()

    def _on_mda_finished(self, *_args) -> None:
        with suppress(RuntimeError):
            if self.poll_chk.isChecked():
                self._timer.start()

    # ------------------------------------------------------------- telemetry
    def _poll(self) -> None:
        try:
            visible = self.isVisible()
        except RuntimeError:
            return
        if visible:
            self._read_and_update()

    def _read_and_update(self) -> None:
        label = self.controller.label
        # Force the adapter to re-query the controller for fresh sensor values.
        if self._can_refresh:
            with suppress(Exception):
                self.mmcore.setProperty(label, "RefreshPropertyValues", "Yes")

        state = ""
        with suppress(Exception):
            state = str(self.mmcore.getProperty(label, "CRISP State"))
        self._update_state(state)

        for prop, _ in _READOUTS:
            with suppress(Exception):
                self._update_readout(prop, self.mmcore.getProperty(label, prop))

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
        else:
            lbl.setText(str(value))

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
