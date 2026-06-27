from pymmcore_plus import CMMCorePlus, DeviceType


def discover_crisp_devices(mmcore: CMMCorePlus | None = None) -> list[str]:
    mmcore = mmcore or CMMCorePlus.instance()
    labels = []

    # GetLoadedDevicesOfType requires the DeviceType enum
    for device in mmcore.getLoadedDevicesOfType(DeviceType.AutoFocusDevice):
        try:
            desc = mmcore.getDeviceDescription(device)
            if desc and desc.startswith("ASI CRISP"):
                labels.append(device)
        except Exception:
            continue

    return labels
