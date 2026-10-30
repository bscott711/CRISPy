"""Which CRISPy controls may run, given the MDA, a tool and the spot lock (Qt-free).

The Tiger has one serial line, so the panel lets only one reader at a time use it: the
slow poller, one CRISPy tool (Dither assistant, Focus curve, Hold test), or the spot
lock. These pure functions hold those decisions so they are unit-tested; the panel
only applies them to its widgets. Nothing here imports Qt.
"""

from __future__ import annotations

from dataclasses import dataclass

DITHER, CURVE, HOLD = "dither", "curve", "hold"
"""The names of the CRISPy tools (``active_tool`` is one of these, or ``None``)."""


def poll_may_run(
    *, active_tool: str | None, mda_running: bool, spot_lock: bool
) -> bool:
    """The slow poll runs only with no tool, no MDA and no spot lock on the line."""
    return active_tool is None and not mda_running and not spot_lock


def tool_may_start(*, active_tool: str | None, spot_lock: bool) -> bool:
    """A tool starts only if no other tool runs and the spot lock is not active."""
    return active_tool is None and not spot_lock


def spot_section_enabled(*, active_tool: str | None) -> bool:
    """The "Spot lock" section is disabled while a CRISPy tool runs."""
    return active_tool is None


@dataclass(frozen=True)
class ControlGate:
    """Which CRISPy controls are enabled."""

    ops: bool
    """Operation and Calibration buttons (each writes to the Tiger)."""
    led_and_params: bool
    """The joystick box and the parameter widgets (each writes)."""
    led: bool
    """The LED button: off under the spot lock and while CRISP is locked."""
    dither_watch: bool
    curve_run: bool
    hold_start: bool


def control_gate(
    *, active_tool: str | None, spot_lock: bool, crisp_locked: bool = False
) -> ControlGate:
    """The enabled state of the CRISPy controls.

    While the spot lock is active nothing in the panel writes to the Tiger and no tool
    starts. Otherwise the original rules hold: the Focus curve moves the actuator, so
    the Operation and Calibration buttons are off during it, and a tool's own button
    stays on only for that tool. The LED button is also off while CRISP itself is in
    Lock or In Focus (*crisp_locked*), whatever tool runs.
    """
    return ControlGate(
        ops=active_tool != CURVE and not spot_lock,
        led_and_params=not spot_lock,
        led=not spot_lock and not crisp_locked,
        dither_watch=not spot_lock and active_tool in (None, DITHER),
        curve_run=not spot_lock and active_tool in (None, CURVE),
        hold_start=not spot_lock and active_tool in (None, HOLD),
    )
