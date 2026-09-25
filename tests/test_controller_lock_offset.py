"""Tests for ASICrispController's CRISP lock-offset setpoint methods."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from CRISPy.controller import (
    LOCK_OFFSET_PROP,
    SET_LOCK_OFFSET_PROP,
    ASICrispController,
)

LABEL = "CRISPAFocus:P:34"
# Piezo-axis calibration from a real crisp_dump.txt: |−377| / 0.675 ≈ 558.5.
_PIEZO_CAL = {"Calibration Gain": "-377", "Calibration Range(um)": "0.6750"}
_SENSITIVITY = 377 / 0.6750


def _controller(props: dict[str, str]) -> tuple[ASICrispController, MagicMock]:
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: props[prop]
    # No paired stage / joystick preset -> construction stays hardware-free.
    core.getLoadedDevicesOfType.return_value = []
    core.getConfigData.side_effect = RuntimeError("no preset in test")
    return ASICrispController(LABEL, core), core


def _wrote(core: MagicMock, prop: str) -> str | None:
    for call in core.setProperty.call_args_list:
        if call.args[0] == LABEL and call.args[1] == prop:
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
