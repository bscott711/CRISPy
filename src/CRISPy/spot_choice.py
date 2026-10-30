"""Which CRISPy panel gets the "Spot lock" section (Qt-free, so it is unit-tested).

The spot lock drives one piezo and one camera, so only one panel per process may hold
it. Nothing here imports Qt.
"""

from __future__ import annotations

from collections.abc import Sequence


def choose_spot_index(
    actuators: Sequence[tuple[str, str | None]], piezo_override: str | None
) -> int | None:
    """Index of the panel that gets the Spot lock section, or ``None`` for none.

    Parameters
    ----------
    actuators
        One ``(actuator_type, actuator_label)`` pair per panel, in panel order
        (``actuator_type`` is ``"piezo"``, ``"stage"`` or ``"unknown"``).
    piezo_override
        The host's piezo label (``CRISPy.spot.store`` ``piezo_label``), or ``None``.

    Returns
    -------
    int | None
        The piezo panel whose actuator label equals *piezo_override*; failing that,
        the first piezo panel; ``None`` if no panel is a piezo panel.
    """
    piezos = [i for i, (kind, _) in enumerate(actuators) if kind == "piezo"]
    if not piezos:
        return None
    if piezo_override is not None:
        for i in piezos:
            if actuators[i][1] == piezo_override:
                return i
    return piezos[0]
