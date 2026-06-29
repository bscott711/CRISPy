from pymmcore_plus import CMMCorePlus, DeviceType

# ASI's Tiger controller exposes CRISP autofocus axes through this device adapter
# library; its autofocus devices are named like ``CRISPAFocus:Z:32``.
_ASI_LIBRARY = "ASITiger"
_CRISP_TOKEN = "CRISP"


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

        name_matches = _CRISP_TOKEN in device.upper() or _CRISP_TOKEN in str(
            name
        ).upper()
        asi_autofocus = (
            dev_type == DeviceType.AutoFocusDevice and library == _ASI_LIBRARY
        )

        if name_matches or asi_autofocus:
            labels.append(device)

    return labels
