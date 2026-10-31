"""Which CRISPy controls may run, given the MDA and a tool (Qt-free).

The Tiger has one serial line, so the panel lets only one reader at a time use it: the
slow poller or one CRISPy tool (Dither assistant, Focus curve, Hold test). These pure
functions hold those decisions so they are unit-tested; the panel only applies them to
its widgets. Nothing here imports Qt.
"""

from __future__ import annotations

from dataclasses import dataclass

DITHER, CURVE, HOLD = "dither", "curve", "hold"
"""The names of the CRISPy tools (``active_tool`` is one of these, or ``None``)."""


def poll_may_run(*, active_tool: str | None, mda_running: bool) -> bool:
    """The slow poll runs only with no tool and no MDA on the line."""
    return active_tool is None and not mda_running


def tool_may_start(*, active_tool: str | None) -> bool:
    """A tool starts only if no other tool runs."""
    return active_tool is None


@dataclass(frozen=True)
class ControlGate:
    """Which CRISPy controls are enabled."""

    ops: bool
    """Operation and Calibration buttons (each writes to the Tiger)."""
    led_and_params: bool
    """The joystick box and the parameter widgets (each writes)."""
    led: bool
    """The LED button: off while CRISP is locked."""
    dither_watch: bool
    curve_run: bool
    hold_start: bool


def control_gate(*, active_tool: str | None, crisp_locked: bool = False) -> ControlGate:
    """The enabled state of the CRISPy controls.

    The Focus curve moves the actuator, so the Operation and Calibration buttons are
    off during it, and a tool's own button stays on only for that tool. The LED button
    is also off while CRISP itself is in Lock or In Focus (*crisp_locked*), whatever
    tool runs.
    """
    return ControlGate(
        ops=active_tool != CURVE,
        led_and_params=True,
        led=not crisp_locked,
        dither_watch=active_tool in (None, DITHER),
        curve_run=active_tool in (None, CURVE),
        hold_start=active_tool in (None, HOLD),
    )
