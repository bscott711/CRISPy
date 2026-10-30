from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QLabel,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QWidget,
)

from .controller import ASICrispController
from .discovery import discover_crisp_devices
from .piezo_panel import PiezoSpotPanel
from .spot.store import get_settings_store
from .spot_choice import choose_spot_index
from .ui import CrispControlPanel


def _scrolled(widget: QWidget) -> QScrollArea:
    """Wrap *widget* in a vertical scroll area (panels can be tall)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def launch_crisp_plugin() -> QWidget:
    """Build the CRISPy panels, one per CRISP axis.

    One piezo axis (see :func:`~CRISPy.spot_choice.choose_spot_index`) gets the
    camera-lock-only :class:`~CRISPy.piezo_panel.PiezoSpotPanel`; every other axis
    (the Z stage) gets the hardware-CRISP :class:`~CRISPy.ui.CrispControlPanel`.
    """
    mmcore = CMMCorePlus.instance()
    labels = discover_crisp_devices(mmcore)
    num_devices = len(labels)

    if num_devices == 0:
        widget = QLabel("No ASI CRISP devices found in current Hardware Configuration.")
        widget.setMargin(20)
        return widget

    controllers = [ASICrispController(label, mmcore) for label in labels]
    spot_index = choose_spot_index(
        [(c.actuator_type, c.actuator_label) for c in controllers],
        get_settings_store().piezo_label,
    )
    panels = [
        _scrolled(
            PiezoSpotPanel(c, mmcore)
            if i == spot_index
            else CrispControlPanel(c, mmcore)
        )
        for i, c in enumerate(controllers)
    ]

    if num_devices == 1:
        return panels[0]

    if num_devices == 2:
        # Show both axes at once so both locks are visible / controllable
        # simultaneously.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        for panel in panels:
            splitter.addWidget(panel)
        return splitter

    # 3+ devices: fall back to tabs to avoid a cramped layout.
    tabs = QTabWidget()
    tabs.setUsesScrollButtons(True)
    for label, panel in zip(labels, panels):
        tabs.addTab(panel, f"CRISP: {label}")
    return tabs
