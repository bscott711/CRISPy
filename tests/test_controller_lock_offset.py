"""Tests for ASICrispController's CRISP lock-offset setpoint methods."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pymmcore_plus import DeviceType

from CRISPy.controller import (
    LED_REAPPLY_STATES,
    LOCK_OFFSET_PROP,
    SET_LOCK_OFFSET_PROP,
    ASICrispController,
    should_reapply_led,
)

LABEL = "CRISPAFocus:P:34"
# Piezo-axis calibration from a real crisp_dump.txt: |−377| / 0.675 ≈ 558.5.
_PIEZO_CAL = {"Calibration Gain": "-377", "Calibration Range(um)": "0.6750"}
_SENSITIVITY = 377 / 0.6750


def _controller(
    props: dict[str, str], hubs: list[str] | None = None
) -> tuple[ASICrispController, MagicMock]:
    """Build a controller on a fake core.

    *hubs* are the loaded Hub-type devices (all from the CRISP's library and
    carrying a ``SerialCommand`` property unless a test overrides that). No stage
    devices are loaded, so construction stays hardware-free.
    """
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: props[prop]
    # The rig has no "Parent,..." config lines, so MMCore reports no parent label.
    core.getParentLabel.return_value = ""
    hub_list = list(hubs or [])
    core.getLoadedDevicesOfType.side_effect = lambda dev_type: (
        list(hub_list) if dev_type == DeviceType.HubDevice else []
    )
    core.getDeviceLibrary.return_value = "ASITiger"
    core.hasProperty.return_value = True
    core.getConfigData.side_effect = RuntimeError("no preset in test")
    return ASICrispController(LABEL, core), core


def _wrote(core: MagicMock, prop: str) -> str | None:
    for call in core.setProperty.call_args_list:
        if call.args[0] == LABEL and call.args[1] == prop:
            return call.args[2]
    return None


def _wrote_hub(core: MagicMock, hub: str, prop: str) -> str | None:
    for call in core.setProperty.call_args_list:
        if call.args[0] == hub and call.args[1] == prop:
            return call.args[2]
    return None


def test_set_lock_offset_counts_writes_property() -> None:
    ctrl, core = _controller({})
    ctrl.set_lock_offset_counts(150)
    assert _wrote(core, SET_LOCK_OFFSET_PROP) == "150"


def test_set_lock_offset_counts_rounds_to_int() -> None:
    ctrl, core = _controller({})
    ctrl.set_lock_offset_counts(112.6)  # type: ignore[arg-type]
    assert _wrote(core, SET_LOCK_OFFSET_PROP) == "113"


def test_get_lock_offset_counts_reads_readonly_prop() -> None:
    ctrl, _ = _controller({LOCK_OFFSET_PROP: "7429"})
    assert ctrl.get_lock_offset_counts() == 7429.0


def test_get_set_lock_offset_counts_reads_writable_prop() -> None:
    ctrl, _ = _controller({SET_LOCK_OFFSET_PROP: "150", LOCK_OFFSET_PROP: "9"})
    assert ctrl.get_set_lock_offset_counts() == 150.0


def test_get_lock_offset_counts_returns_none_when_unreadable() -> None:
    ctrl, _ = _controller({})
    assert ctrl.get_lock_offset_counts() is None


def test_set_lock_offset_um_uses_device_sensitivity() -> None:
    ctrl, core = _controller(dict(_PIEZO_CAL))
    ctrl.set_lock_offset_um(0.2)
    assert _wrote(core, SET_LOCK_OFFSET_PROP) == str(round(0.2 * _SENSITIVITY))


def test_set_lock_offset_um_uses_explicit_scale() -> None:
    ctrl, core = _controller({})  # no calibration available
    ctrl.set_lock_offset_um(1.0, counts_per_um=500.0)
    assert _wrote(core, SET_LOCK_OFFSET_PROP) == "500"


def test_get_lock_offset_um_roundtrips() -> None:
    ctrl, _ = _controller({LOCK_OFFSET_PROP: "558"})
    assert ctrl.get_lock_offset_um(counts_per_um=558.0) == pytest.approx(1.0)


def test_set_lock_offset_um_without_calibration_raises_and_writes_nothing() -> None:
    ctrl, core = _controller({})
    with pytest.raises(RuntimeError):
        ctrl.set_lock_offset_um(0.2)
    assert _wrote(core, SET_LOCK_OFFSET_PROP) is None


def test_set_led_on_sends_addressed_command_forcing_resend() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props)
    core.getParentLabel.return_value = "TigerCommHub"
    ctrl.set_led(True)
    writes = [c.args for c in core.setProperty.call_args_list]
    assert writes == [
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "No"),
        ("TigerCommHub", "SerialCommand", "4LK F=90"),
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "Yes"),
    ]


def test_set_led_on_restores_hub_flag_when_send_fails() -> None:
    ctrl, core = _controller(
        {"TigerHexAddress": "32", "OnlySendSerialCommandOnChange": "Yes"}
    )
    core.getParentLabel.return_value = "TigerCommHub"

    def fail(label: str, prop: str, value: str) -> None:
        if prop == "SerialCommand":
            raise RuntimeError("boom")

    core.setProperty.side_effect = fail
    with pytest.raises(RuntimeError):
        ctrl.set_led(True)
    assert core.setProperty.call_args_list[-1].args == (
        "TigerCommHub",
        "OnlySendSerialCommandOnChange",
        "Yes",
    )


def test_set_led_off_sends_raw_lk_f79_through_hub() -> None:
    # Raw (not the adapter's Idle): Idle is skipped when the cached state is Idle.
    props = {"TigerHexAddress": "32", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props)
    core.getParentLabel.return_value = "TigerCommHub"
    ctrl.set_led(False)
    writes = [c.args for c in core.setProperty.call_args_list]
    assert writes == [
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "No"),
        ("TigerCommHub", "SerialCommand", "2LK F=79"),
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "Yes"),
    ]
    assert _wrote(core, "CRISP State") is None


def test_set_led_on_rejects_extended_card_address() -> None:
    ctrl, core = _controller({"TigerHexAddress": "85"})
    core.getParentLabel.return_value = "TigerCommHub"
    with pytest.raises(ValueError):
        ctrl.set_led(True)
    core.setProperty.assert_not_called()


def test_set_led_on_without_parent_label_finds_the_tiger_hub() -> None:
    # Regression: the rig config has no Parent lines, so getParentLabel() is "" and
    # the old code did getProperty("", "OnlySendSerialCommandOnChange") -> raised.
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=["TigerCommHub"])
    ctrl.set_led(True)
    writes = [c.args for c in core.setProperty.call_args_list]
    assert writes == [
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "No"),
        ("TigerCommHub", "SerialCommand", "4LK F=90"),
        ("TigerCommHub", "OnlySendSerialCommandOnChange", "Yes"),
    ]


def test_set_led_off_without_parent_label_finds_the_tiger_hub() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "No"}
    ctrl, core = _controller(props, hubs=["TigerCommHub"])
    ctrl.set_led(False)
    assert ("TigerCommHub", "SerialCommand", "4LK F=79") in [
        c.args for c in core.setProperty.call_args_list
    ]


def test_hub_fallback_skips_hubs_of_other_libraries_or_without_serial_command() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=["ArduinoHub", "OtherTiger", "TigerCommHub"])
    libraries = {
        LABEL: "ASITiger",
        "TigerCommHub": "ASITiger",
        "OtherTiger": "ASITiger",
    }
    core.getDeviceLibrary.side_effect = lambda dev: libraries.get(dev, "Arduino")
    core.hasProperty.side_effect = lambda dev, prop: dev != "OtherTiger"
    ctrl.set_led(True)
    assert _wrote_hub(core, "TigerCommHub", "SerialCommand") == "4LK F=90"


def test_hub_fallback_with_no_matching_hub_raises_and_writes_nothing() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=[])
    with pytest.raises(RuntimeError, match=LABEL):
        ctrl.set_led(True)
    core.setProperty.assert_not_called()


def test_hub_fallback_with_two_matching_hubs_raises_and_writes_nothing() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=["TigerCommHub", "TigerCommHub2"])
    with pytest.raises(RuntimeError, match=LABEL):
        ctrl.set_led(True)
    core.setProperty.assert_not_called()


def test_parent_label_is_used_without_scanning_hubs() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=["SomeOtherHub"])
    core.getParentLabel.return_value = "TigerCommHub"
    ctrl.set_led(True)
    assert _wrote_hub(core, "TigerCommHub", "SerialCommand") == "4LK F=90"
    assert _wrote_hub(core, "SomeOtherHub", "SerialCommand") is None


def test_hub_is_resolved_once_and_cached() -> None:
    props = {"TigerHexAddress": "34", "OnlySendSerialCommandOnChange": "Yes"}
    ctrl, core = _controller(props, hubs=["TigerCommHub"])
    ctrl.set_led(True)
    ctrl.set_led(False)
    scans = [
        c
        for c in core.getLoadedDevicesOfType.call_args_list
        if c.args == (DeviceType.HubDevice,)
    ]
    assert len(scans) == 1


def test_construction_does_not_resolve_the_hub() -> None:
    _, core = _controller({}, hubs=["TigerCommHub"])
    core.getParentLabel.assert_not_called()
    core.setProperty.assert_not_called()


def test_should_reapply_led_only_when_led_on_and_idle_or_ready() -> None:
    assert LED_REAPPLY_STATES == ("Idle", "Ready")
    assert should_reapply_led("Idle", True)
    assert should_reapply_led("Ready", True)
    assert not should_reapply_led("Idle", False)
    assert not should_reapply_led("Ready", False)


@pytest.mark.parametrize(
    "state",
    [
        "Lock",
        "In Focus",
        "Dither",
        "loG_cal",
        "gain_Cal",
        "Calibrating",
        "",
        "Unknown",
        "idle",
    ],
)
def test_should_reapply_led_never_for_other_states(state: str) -> None:
    assert not should_reapply_led(state, True)
    assert not should_reapply_led(state, False)
