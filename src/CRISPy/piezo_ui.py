"""The piezo axis widget that carries the spot edge lock.

``PiezoSpotWidget`` replaces the ``CrispControlPanel`` for the one piezo
that :func:`~CRISPy.spot_choice.choose_spot_index` picks. It shows a header (device
label, lock word), the four status words, the embedded
:class:`~CRISPy.spot.panel.SpotFocusPanel` (controls, message line, live image,
toolbars, sections) and a Joystick checkbox in the panel's control row.

It holds the ``ASICrispController`` and hands it to the panel's spot service: no reads,
no poller, no serial-line gating. The service lights the LED with raw ``LK F=90/79``
(it never writes CRISP State) and owns the joystick lock: it turns the wheel off while
the lock runs and restores it on unlock, fault, worker death and shutdown. The Joystick
checkbox only shows that state, and is a manual override (a direct write). The panel is
a child, so it shuts the lock down (restoring the wheel) when this widget is destroyed
or the application quits.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import piezo_status
from .controller import ASICrispController
from .spot.panel import SpotFocusPanel
from .spot.store import get_settings_store
from .theme import micro, set_role, set_state_pill, set_tone

# Lock word -> status pill (the same LED logic as the CRISP panel's State pill).
_LOCK_PILL = {
    piezo_status.LOCK_RUNNING: "ok",
    piezo_status.LOCK_STARTING: "busy",
    piezo_status.LOCK_CALIBRATING: "busy",
    piezo_status.LOCK_ZEROING: "busy",
    piezo_status.LOCK_FAULT: "bad",
}
# Status words that mean "nothing yet" (dim), or need attention (tone).
_DIM_WORDS = {"Not set", "Not open", "No camera", "Off", "Not available"}
_WARN_WORDS = {"Required", "Opening"}
_BAD_WORDS = {"Error", "Fault", "No CRISP device"}


class PiezoSpotWidget(QWidget):
    """Header, status words, spot-lock panel and joystick checkbox for one piezo."""

    def __init__(
        self,
        controller: ASICrispController,
        mmcore: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.mmcore = mmcore

        layout = QVBoxLayout(self)

        # The piezo label the panel moves (same derivation as the old section).
        store = get_settings_store()
        piezo = (
            store.piezo_label
            if store.piezo_label is not None
            else controller.actuator_label
        )

        # --- header: same look as the CRISP panel (title, badge, state pill) -
        header = QHBoxLayout()
        self.title = QLabel(piezo or controller.label)
        self.title.setProperty("role", "header")
        header.addWidget(self.title)
        badge = QLabel("PIEZO")
        badge.setProperty("badge", "piezo")
        header.addWidget(badge)
        header.addStretch(1)
        self.lock_status_label = QLabel()
        set_state_pill(self.lock_status_label, "idle")
        header.addWidget(self.lock_status_label)
        layout.addLayout(header)

        # --- readouts: Camera, LED / Edge box, Calibration -----------------
        ro_box = self.readouts_box = QGroupBox()
        grid = QGridLayout(ro_box)
        grid.addWidget(micro("Readouts"), 0, 0, 1, 4)
        for col, stretch in enumerate((3, 2, 3, 2)):
            grid.setColumnStretch(col, stretch)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        self.camera_status_label = QLabel()
        self.led_status_label = QLabel()
        self.edge_box_status_label = QLabel()
        self.calibration_status_label = QLabel()
        for i, (name, val) in enumerate(
            (
                ("Camera:", self.camera_status_label),
                ("LED:", self.led_status_label),
                ("Edge box:", self.edge_box_status_label),
                ("Calibration:", self.calibration_status_label),
            )
        ):
            row, col = divmod(i, 2)
            val.setProperty("role", "read")
            val.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            grid.addWidget(QLabel(name), row + 1, col * 2)
            grid.addWidget(val, row + 1, col * 2 + 1)
        layout.addWidget(ro_box)

        # --- the spot-lock panel ------------------------------------------
        crisp = store.crisp_label if store.crisp_label is not None else controller.label
        # The panel stops its worker when it is destroyed, i.e. with this widget.
        self.panel = SpotFocusPanel(
            mmcore,
            parent=self,
            piezo_label=piezo,
            crisp_label=crisp,
            controller=controller,
            auto_preview=True,  # the docked panel opens with the camera live
        )
        layout.addWidget(self.panel, 1)

        # --- joystick checkbox, in the panel's control row ----------------
        self.joystick_chk = QCheckBox("Joystick enabled")
        self.joystick_chk.setChecked(True)
        self.panel.control_extra_layout.addWidget(self.joystick_chk)
        self.joystick_chk.toggled.connect(self._on_joystick_toggled)
        self._wheel_locked = False  # mirrors the service's joystick lock
        self.panel.lockActiveChanged.connect(self._sync_joystick_box)

        self.panel.statusChanged.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------ joystick
    def _write_joystick(self, enabled: bool) -> None:
        """Write the wheel enable on the paired stage (GUI thread, like ui.py)."""
        with suppress(Exception):
            self.controller.set_joystick_enabled(enabled)

    def _on_joystick_toggled(self, checked: bool) -> None:
        """Manual override; allowed at any time, including while the lock is active."""
        self._write_joystick(checked)

    def _sync_joystick_box(self, *_: object) -> None:
        """Show the service's joystick lock in the box (display only, no write).

        The service writes the wheel itself; this only follows its flag, and only
        when the flag changes, so a manual toggle of the box persists until then.
        """
        svc = self.panel.service
        locked = bool(svc is not None and svc.joystick_locked)
        if locked == self._wheel_locked:
            return
        self._wheel_locked = locked
        blocked = self.joystick_chk.blockSignals(True)
        try:
            self.joystick_chk.setChecked(not locked)
        finally:
            self.joystick_chk.blockSignals(blocked)

    # -------------------------------------------------------------- status
    def _refresh(self) -> None:
        """Update the lock word and the four status words (cheap, no I/O)."""
        self._sync_joystick_box()
        words = piezo_status.status_words(self.panel.status_inputs())
        self.lock_status_label.setText(f"Lock: {words.lock}")
        set_state_pill(self.lock_status_label, _LOCK_PILL.get(words.lock, "idle"))
        for label, word in (
            (self.camera_status_label, words.camera),
            (self.led_status_label, words.led),
            (self.edge_box_status_label, words.edge_box),
            (self.calibration_status_label, words.calibration),
        ):
            label.setText(word)
            if word in _BAD_WORDS:
                tone = "bad"
            elif word in _WARN_WORDS:
                tone = "warn"
            else:
                tone = ""
            set_role(label, "hint" if word in _DIM_WORDS else "read")
            set_tone(label, tone)
