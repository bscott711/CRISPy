from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QLabel,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QWidget,
)
from pymmcore_plus import CMMCorePlus
from .controller import ASICrispController
from .discovery import discover_crisp_devices
from .ui import CrispControlPanel


def _scrolled(widget: QWidget) -> QScrollArea:
    """Wrap *widget* in a vertical scroll area (panels can be tall)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def launch_crisp_plugin() -> QWidget:
    mmcore = CMMCorePlus.instance()
    labels = discover_crisp_devices(mmcore)
    num_devices = len(labels)

    if num_devices == 0:
        widget = QLabel("No ASI CRISP devices found in current Hardware Configuration.")
        widget.setMargin(20)
        return widget

    panels = [
        _scrolled(CrispControlPanel(ASICrispController(label, mmcore), mmcore))
        for label in labels
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
