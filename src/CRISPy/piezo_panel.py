"""The camera-lock-only panel for the piezo CRISP axis.

The piezo axis is focused by the camera "Spot lock" (:mod:`CRISPy.spot`) alone: this
panel holds a header and the :class:`~CRISPy.spot.panel.SpotFocusPanel`, with none of
the hardware-CRISP controls (those stay in :class:`~CRISPy.ui.CrispControlPanel`, used
for the Z-stage axis).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from .controller import CrispController

_BADGE_COLORS = ("#6a1b9a", "#ffffff")  # the piezo badge of CrispControlPanel


class PiezoSpotPanel(QWidget):
    """The piezo axis: a header and the camera spot lock, nothing else.

    Parameters
    ----------
    controller
        The CRISP controller of the piezo axis.
    mmcore
        The core; the singleton if omitted.
    parent
        Optional parent widget.
    """

    def __init__(
        self,
        controller: CrispController,
        mmcore: CMMCorePlus | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.mmcore = mmcore or CMMCorePlus.instance()

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(controller.label)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        header.addWidget(title)
        header.addWidget(self._make_badge())
        header.addStretch()
        layout.addLayout(header)

        # Imported here (not at module scope) to keep ``import CRISPy`` lightweight.
        from .spot.panel import SpotFocusPanel
        from .spot.store import get_settings_store

        store = get_settings_store()
        piezo = (
            store.piezo_label
            if store.piezo_label is not None
            else controller.actuator_label
        )
        crisp = store.crisp_label if store.crisp_label is not None else controller.label
        # The panel stops its worker when it is destroyed, i.e. with this panel.
        self.spot_panel = SpotFocusPanel(
            self.mmcore, piezo_label=piezo, crisp_label=crisp, controller=controller
        )
        layout.addWidget(self.spot_panel, 1)

    def _make_badge(self) -> QLabel:
        bg, fg = _BADGE_COLORS
        badge = QLabel("Piezo")
        badge.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 6px;"
            " padding: 1px 6px; font-weight: bold; font-size: 11px;"
        )
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setToolTip(
            f"Paired focus actuator: {self.controller.actuator_label or 'Piezo'}."
        )
        return badge
