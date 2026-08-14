from __future__ import annotations

from typing import Literal

from pymmcore_plus import CMMCorePlus, DeviceType

# ASI's Tiger controller exposes CRISP autofocus axes through this device adapter
# library; its autofocus devices are named like ``CRISPAFocus:Z:32``.
_ASI_LIBRARY = "ASITiger"
_CRISP_TOKEN = "CRISP"

#: Result of :func:`classify_focus_actuator`.
ActuatorType = Literal["piezo", "stage", "unknown"]


def discover_crisp_devices(mmcore: CMMCorePlus | None = None) -> list[str]:
    """Return the labels of all loaded ASI CRISP autofocus devices.

    Detection is intentionally robust to device-adapter description strings: a
    device qualifies if its *label* or adapter *name* contains ``"CRISP"``, or if
    it is an :class:`~pymmcore_plus.DeviceType.AutoFocusDevice` provided by the ASI
    ``ASITiger`` library.  (The previous implementation required the C++ device
    description to start with ``"ASI CRISP"``, which is not true for all adapter
    builds and caused real CRISP devices to be missed.)
    """
    mmcore = mmcore or CMMCorePlus.instance()
    labels: list[str] = []

    for device in mmcore.getLoadedDevices():
        try:
            name = mmcore.getDeviceName(device)
            library = mmcore.getDeviceLibrary(device)
            dev_type = mmcore.getDeviceType(device)
        except Exception:
            continue

        name_matches = (
            _CRISP_TOKEN in device.upper() or _CRISP_TOKEN in str(name).upper()
        )
        asi_autofocus = (
            dev_type == DeviceType.AutoFocusDevice and library == _ASI_LIBRARY
        )

        if name_matches or asi_autofocus:
            labels.append(device)

    return labels


def find_paired_actuator_label(
    label: str, mmcore: CMMCorePlus | None = None
) -> str | None:
    """Return the device label of the ASITiger stage paired with a CRISP axis.

    A CRISP axis (e.g. ``CRISPAFocus:P:34``) only servos an error signal; the
    actual actuator is a separate ASITiger stage device that shares the same Tiger
    axis letter and hex address (e.g. ``PiezoStage:P:34`` or ``ZStage:Z:32``).

    Matching on the ``:<AxisLetter>:<HexAddr>`` suffix (rather than assuming a
    particular axis letter means "piezo") keeps this robust across rigs where axis
    letters are assigned arbitrarily.

    Parameters
    ----------
    label : str
        Device label of the CRISP autofocus axis.
    mmcore : CMMCorePlus | None
        Core instance to query; defaults to the shared singleton.

    Returns
    -------
    str | None
        The paired stage device's label, or ``None`` if none was found.
    """
    mmcore = mmcore or CMMCorePlus.instance()

    axis = _read_prop(mmcore, label, "AxisLetter")
    hex_addr = _read_prop(mmcore, label, "TigerHexAddress")
    if not axis or not hex_addr:
        return None
    suffix = f":{axis}:{hex_addr}"

    for device in mmcore.getLoadedDevicesOfType(DeviceType.StageDevice):
        try:
            if mmcore.getDeviceLibrary(device) != _ASI_LIBRARY:
                continue
            name = str(mmcore.getDeviceName(device))
        except Exception:
            continue
        if device.endswith(suffix) or name.endswith(suffix):
            return device

    return None


def classify_focus_actuator(
    label: str, mmcore: CMMCorePlus | None = None
) -> ActuatorType:
    """Classify the physical focus actuator driven by a CRISP autofocus axis.

    Locates the paired stage device (see :func:`find_paired_actuator_label`) and
    classifies it by adapter name: a piezo stage reports as ``PiezoStage``
    whereas a motorized Z drive reports as ``ZStage``.
    """
    mmcore = mmcore or CMMCorePlus.instance()

    stage_label = find_paired_actuator_label(label, mmcore)
    if stage_label is None:
        return "unknown"
    try:
        name = str(mmcore.getDeviceName(stage_label))
    except Exception:
        return "unknown"

    return "piezo" if "PIEZO" in name.upper() else "stage"


def _read_prop(mmcore: CMMCorePlus, label: str, prop: str) -> str:
    try:
        return str(mmcore.getProperty(label, prop)).strip()
    except Exception:
        return ""
