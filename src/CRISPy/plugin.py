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
from .piezo_ui import PiezoSpotWidget
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

    The one piezo that :func:`~CRISPy.spot_choice.choose_spot_index` picks gets a
    :class:`~CRISPy.piezo_ui.PiezoSpotWidget` (the spot edge lock interface) instead
    of a CRISP panel; every other axis gets a :class:`~CRISPy.ui.CrispControlPanel`.
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
            PiezoSpotWidget(c, mmcore)
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
        for i, panel in enumerate(panels):
            splitter.addWidget(panel)
            splitter.setStretchFactor(i, 1)
        # Equal panes: the splitter would otherwise divide by sizeHint.
        splitter.setSizes([1] * num_devices)
        return splitter

    # 3+ devices: fall back to tabs to avoid a cramped layout.
    tabs = QTabWidget()
    tabs.setUsesScrollButtons(True)
    for label, panel in zip(labels, panels):
        tabs.addTab(panel, f"CRISP: {label}")
    return tabs
