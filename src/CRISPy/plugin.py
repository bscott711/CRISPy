from qtpy.QtWidgets import QTabWidget, QLabel
from pymmcore_plus import CMMCorePlus
from .controller import ASICrispController
from .discovery import discover_crisp_devices
from .ui import CrispControlPanel


def launch_crisp_plugin():
    mmcore = CMMCorePlus.instance()
    labels = discover_crisp_devices(mmcore)
    num_devices = len(labels)

    if num_devices == 0:
        widget = QLabel("No ASI CRISP devices found in current Hardware Configuration.")
        widget.setMargin(20)
        return widget

    if num_devices == 1:
        controller = ASICrispController(labels[0], mmcore)
        return CrispControlPanel(controller, mmcore).native

    tab_widget = QTabWidget()
    tab_widget.setUsesScrollButtons(True)

    for label in labels:
        controller = ASICrispController(label, mmcore)
        panel = CrispControlPanel(controller, mmcore)
        tab_widget.addTab(panel.native, f"CRISP: {label}")

    return tab_widget
