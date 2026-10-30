"""The Qt-free gating rules: who may use the Tiger serial line (poller, tool, spot lock)."""

from __future__ import annotations

from CRISPy.gating import (
    CURVE,
    DITHER,
    HOLD,
    control_gate,
    poll_may_run,
    spot_section_enabled,
    tool_may_start,
)


class _Line:
    """The panel's three inputs, driven the way ui.py drives them."""

    def __init__(self) -> None:
        self.tool: str | None = None
        self.mda = False
        self.lock = False

    @property
    def poll(self) -> bool:
        return poll_may_run(
            active_tool=self.tool, mda_running=self.mda, spot_lock=self.lock
        )


def test_idle_everything_allowed():
    line = _Line()
    assert line.poll
    assert tool_may_start(active_tool=None, spot_lock=False)
    assert spot_section_enabled(active_tool=None)
    gate = control_gate(active_tool=None, spot_lock=False)
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


def test_mda_ends_while_lock_active_poller_stays_paused():
    line = _Line()
    line.mda = True
    line.lock = True
    assert not line.poll
    line.mda = False  # MDA finished, the lock still owns the line
    assert not line.poll
    line.lock = False
    assert line.poll


def test_lock_ends_during_mda_poller_stays_paused():
    line = _Line()
    line.lock = True
    line.mda = True
    line.lock = False  # lock ended, the acquisition is still running
    assert not line.poll
    line.mda = False
    assert line.poll


def test_poll_paused_by_each_reader_alone():
    for tool in (DITHER, CURVE, HOLD):
        assert not poll_may_run(active_tool=tool, mda_running=False, spot_lock=False)
    assert not poll_may_run(active_tool=None, mda_running=True, spot_lock=False)
    assert not poll_may_run(active_tool=None, mda_running=False, spot_lock=True)


def test_tool_refused_while_locked():
    assert not tool_may_start(active_tool=None, spot_lock=True)
    assert not tool_may_start(active_tool=DITHER, spot_lock=True)


def test_one_tool_at_a_time():
    for tool in (DITHER, CURVE, HOLD):
        assert not tool_may_start(active_tool=tool, spot_lock=False)


def test_spot_section_disabled_while_a_tool_runs():
    for tool in (DITHER, CURVE, HOLD):
        assert not spot_section_enabled(active_tool=tool)
    assert spot_section_enabled(active_tool=None)


def test_spot_lock_disables_every_writer_and_every_tool_button():
    gate = control_gate(active_tool=None, spot_lock=True)
    assert not any(
        (
            gate.ops,
            gate.led_and_params,
            gate.led,
            gate.dither_watch,
            gate.curve_run,
            gate.hold_start,
        )
    )


def test_calibrating_lock_state_is_not_special():
    # The panel reports calibrating/zeroing as lock_active; the gate just sees True.
    assert not poll_may_run(active_tool=None, mda_running=False, spot_lock=True)
    assert not tool_may_start(active_tool=CURVE, spot_lock=True)


def test_gate_reenables_after_lock_ends():
    locked = control_gate(active_tool=None, spot_lock=True)
    free = control_gate(active_tool=None, spot_lock=False)
    assert not locked.ops and free.ops
    assert not locked.led_and_params and free.led_and_params


def test_original_tool_rules_without_the_lock():
    # Focus curve: the writers are off, the curve button (Cancel) stays on.
    gate = control_gate(active_tool=CURVE, spot_lock=False)
    assert not gate.ops
    assert gate.led_and_params
    assert gate.curve_run and not gate.dither_watch and not gate.hold_start
    # Dither: writers stay on, only the Dither button is available.
    gate = control_gate(active_tool=DITHER, spot_lock=False)
    assert gate.ops and gate.dither_watch
    assert not gate.curve_run and not gate.hold_start
    # Hold test.
    gate = control_gate(active_tool=HOLD, spot_lock=False)
    assert gate.ops and gate.hold_start
    assert not gate.curve_run and not gate.dither_watch


def test_led_off_while_crisp_is_locked_whatever_the_tool():
    for tool in (None, DITHER, CURVE, HOLD):
        gate = control_gate(active_tool=tool, spot_lock=False, crisp_locked=True)
        assert not gate.led


def test_led_off_under_the_spot_lock():
    for crisp_locked in (False, True):
        gate = control_gate(active_tool=None, spot_lock=True, crisp_locked=crisp_locked)
        assert not gate.led


def test_led_on_with_neither_lock():
    for tool in (None, DITHER, CURVE, HOLD):
        assert control_gate(active_tool=tool, spot_lock=False).led
        assert control_gate(active_tool=tool, spot_lock=False, crisp_locked=False).led
