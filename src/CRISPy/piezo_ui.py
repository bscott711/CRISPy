"""The piezo axis widget that carries the spot edge lock.

``PiezoSpotWidget`` replaces ``CrispControlPanel(spot_lock=True)`` for the one piezo
that :func:`~CRISPy.spot_choice.choose_spot_index` picks. It shows a header (device
label, lock word), the four status words, the embedded
:class:`~CRISPy.spot.panel.SpotFocusPanel` (controls, message line, live image,
toolbars, sections) and a Joystick checkbox in the panel's control row.

It holds the ``ASICrispController`` only for ``set_joystick_enabled``: no reads, no
poller, no serial-line gating. The spot service is the only writer of CRISP State and
LED. The panel is a child, so it shuts the lock down (restoring CRISP) when this
widget is destroyed or the application quits.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from qtpy.QtWidgets import (
    QApplication,
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


class _WheelGuard:
    """Re-enables the wheel at teardown if the lock had disabled it (no Qt refs)."""

    def __init__(self, controller: ASICrispController) -> None:
        self.controller = controller
        self.lock_active = False

    def restore(self) -> None:
        if self.lock_active:
            self.lock_active = False
            with suppress(Exception):
                self.controller.set_joystick_enabled(True)


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
            mmcore, parent=self, piezo_label=piezo, crisp_label=crisp
        )
        layout.addWidget(self.panel, 1)

        # --- joystick checkbox, in the panel's control row ----------------
        self.joystick_chk = QCheckBox("Joystick enabled")
        self.joystick_chk.setChecked(True)
        self.panel.control_extra_layout.addWidget(self.joystick_chk)
        self.joystick_chk.toggled.connect(self._on_joystick_toggled)
        self.panel.lockActiveChanged.connect(self._on_lock_active)

        # The lock turns the wheel off on lock-active. If this widget is destroyed
        # (config reload) or the app quits while the lock is active, nothing else
        # would turn it back on and the wheel would stay off with no UI. The holder
        # does not reference self, so the destroyed/aboutToQuit connections stay
        # valid after the C++ object is gone.
        self._wheel_guard = holder = _WheelGuard(controller)
        app = QApplication.instance()

        def _on_destroyed(*_: object) -> None:
            holder.restore()
            if app is not None:
                # Drop the quit hook so reloads do not pile up connections.
                with suppress(Exception):
                    app.aboutToQuit.disconnect(holder.restore)

        self.destroyed.connect(_on_destroyed)
        if app is not None:
            app.aboutToQuit.connect(holder.restore)

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

    def _on_lock_active(self, active: bool) -> None:
        """Set the box and the wheel from the lock's activity (a lock edge).

        The wheel is written explicitly on every edge, not only when the box
        changes: the box and the wheel can disagree (the user may have toggled the
        box while locked, or the box may already show the wanted value).
        """
        want = piezo_status.joystick_for_lock(active)
        blocked = self.joystick_chk.blockSignals(True)
        try:
            self.joystick_chk.setChecked(want)
        finally:
            self.joystick_chk.blockSignals(blocked)
        self._write_joystick(want)
        self._wheel_guard.lock_active = bool(active)

    # -------------------------------------------------------------- status
    def _refresh(self) -> None:
        """Update the lock word and the four status words (cheap, no I/O)."""
        words = piezo_status.status_words(self.panel.status_inputs())
        self.lock_status_label.setText(f"Lock: {words.lock}")
        self.camera_status_label.setText(f"Camera: {words.camera}")
        self.led_status_label.setText(f"LED: {words.led}")
        self.edge_box_status_label.setText(f"Edge box: {words.edge_box}")
        self.calibration_status_label.setText(f"Calibration: {words.calibration}")
