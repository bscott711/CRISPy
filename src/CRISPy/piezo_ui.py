"""The piezo axis widget that carries the spot edge lock.

``PiezoSpotWidget`` replaces the ``CrispControlPanel`` for the one piezo
that :func:`~CRISPy.spot_choice.choose_spot_index` picks. It shows a header (device
label, lock word), the four status words, the embedded
:class:`~CRISPy.spot.panel.SpotFocusPanel` (controls, message line, live image,
toolbars, sections) and a Joystick checkbox in the panel's control row.

It holds the ``ASICrispController`` and hands it to the panel's spot service: no reads,
no poller, no serial-line gating. The service lights the LED with raw ``LK F=90/79``
(it never writes CRISP State) and owns the joystick lock: it turns the wheel off while
the lock runs and restores it on Stop, fault, worker death and shutdown. The Joystick
checkbox only shows that state, and is a manual override (a direct write). The panel is
a child, so it shuts the lock down (restoring the wheel) when this widget is destroyed
or the application quits.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from qtpy.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import piezo_status
from .controller import ASICrispController
from .spot.panel import SpotFocusPanel
from .spot.store import get_settings_store


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

        # --- header: piezo label left, lock word right --------------------
        header = QHBoxLayout()
        self.title = QLabel(piezo or controller.label)
        header.addWidget(self.title)
        header.addStretch(1)
        self.lock_status_label = QLabel()
        header.addWidget(self.lock_status_label)
        layout.addLayout(header)

        # --- status words: Camera, LED / Edge box, Calibration ------------
        grid = QGridLayout()
        self.camera_status_label = QLabel()
        self.led_status_label = QLabel()
        self.edge_box_status_label = QLabel()
        self.calibration_status_label = QLabel()
        grid.addWidget(self.camera_status_label, 0, 0)
        grid.addWidget(self.led_status_label, 0, 1)
        grid.addWidget(self.edge_box_status_label, 1, 0)
        grid.addWidget(self.calibration_status_label, 1, 1)
        layout.addLayout(grid)

        # --- the spot-lock panel ------------------------------------------
        crisp = store.crisp_label if store.crisp_label is not None else controller.label
        # The panel stops its worker when it is destroyed, i.e. with this widget.
        self.panel = SpotFocusPanel(
            mmcore,
            parent=self,
            piezo_label=piezo,
            crisp_label=crisp,
            controller=controller,
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
        self.camera_status_label.setText(f"Camera: {words.camera}")
        self.led_status_label.setText(f"LED: {words.led}")
        self.edge_box_status_label.setText(f"Edge box: {words.edge_box}")
        self.calibration_status_label.setText(f"Calibration: {words.calibration}")
