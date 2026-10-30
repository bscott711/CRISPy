"""The Qt-free gating rules: who may use the Tiger serial line (poller, tool)."""

from __future__ import annotations

from CRISPy.gating import (
    CURVE,
    DITHER,
    HOLD,
    control_gate,
    poll_may_run,
    tool_may_start,
)


def test_idle_everything_allowed():
    assert poll_may_run(active_tool=None, mda_running=False)
    assert tool_may_start(active_tool=None)
    gate = control_gate(active_tool=None)
    assert all(
        (
            gate.ops,
            gate.led_and_params,
            gate.led,
            gate.dither_watch,
            gate.curve_run,
            gate.hold_start,
        )
    )


def test_poll_paused_by_each_reader_alone():
    for tool in (DITHER, CURVE, HOLD):
        assert not poll_may_run(active_tool=tool, mda_running=False)
    assert not poll_may_run(active_tool=None, mda_running=True)


def test_one_tool_at_a_time():
    for tool in (DITHER, CURVE, HOLD):
        assert not tool_may_start(active_tool=tool)


def test_tool_rules():
    # Focus curve: the writers are off, the curve button (Cancel) stays on.
    gate = control_gate(active_tool=CURVE)
    assert not gate.ops
    assert gate.led_and_params
    assert gate.curve_run and not gate.dither_watch and not gate.hold_start
    # Dither: writers stay on, only the Dither button is available.
    gate = control_gate(active_tool=DITHER)
    assert gate.ops and gate.dither_watch
    assert not gate.curve_run and not gate.hold_start
    # Hold test.
    gate = control_gate(active_tool=HOLD)
    assert gate.ops and gate.hold_start
    assert not gate.curve_run and not gate.dither_watch


def test_led_off_while_crisp_is_locked_whatever_the_tool():
    for tool in (None, DITHER, CURVE, HOLD):
        assert not control_gate(active_tool=tool, crisp_locked=True).led


def test_led_on_while_crisp_is_not_locked():
    for tool in (None, DITHER, CURVE, HOLD):
        assert control_gate(active_tool=tool).led
        assert control_gate(active_tool=tool, crisp_locked=False).led
