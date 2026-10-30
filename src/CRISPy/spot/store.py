"""Where the spot focus lock keeps its settings (the host's seam).

The panel reads and writes one live :class:`SpotFocusSettingsV1` and asks the host to
persist it. A host (pymmcore-gui) sets its own store with :func:`set_settings_store`
before the panel is built, so the settings stay in the host's settings file; without
one, :class:`MemorySpotSettingsStore` keeps them for the session only. The same hook
pattern as :mod:`CRISPy.imaging`.

``piezo_label`` and ``crisp_label`` are read once, when the CRISPy panel builds its
"Spot lock" section; a label change takes effect only after the panel is rebuilt
(for example a config reload).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .settings import SpotFocusSettingsV1


@runtime_checkable
class SpotSettingsStore(Protocol):
    """What the spot lock needs from its host."""

    settings: SpotFocusSettingsV1
    """The live settings. The panel edits them in place (``settings.gain = ...``)."""
    data_dir: Path | None
    """An existing folder for the calibration sweep records (``spot_calibration_*.npz``);
    ``None`` writes none."""
    piezo_label: str | None
    """The piezo to hold, or ``None`` to use the CRISPy panel's own actuator."""
    crisp_label: str | None
    """The CRISP device whose state and LED the lock uses, or ``None`` to use the
    CRISPy panel's own."""

    def save(self) -> None:
        """Persist :attr:`settings` (the panel calls this after edits, debounced)."""
        ...


class MemorySpotSettingsStore:
    """The default store: settings live in memory, nothing is saved or recorded."""

    def __init__(self, settings: SpotFocusSettingsV1 | None = None) -> None:
        self.settings = settings if settings is not None else SpotFocusSettingsV1()
        self.data_dir: Path | None = None
        self.piezo_label: str | None = None
        self.crisp_label: str | None = None

    def save(self) -> None:
        """Nothing to persist."""


_store: SpotSettingsStore | None = None
_default_store: MemorySpotSettingsStore | None = None


def set_settings_store(store: SpotSettingsStore | None) -> None:
    """Set the host's settings store, or go back to the in-memory default with ``None``.

    Parameters
    ----------
    store
        Read by the spot panel whenever it needs a setting, so set it before the panel
        is built (and keep it set while the panel lives).
    """
    global _store
    _store = store


def get_settings_store() -> SpotSettingsStore:
    """The store set with :func:`set_settings_store`, else the in-memory default."""
    global _default_store
    if _store is not None:
        return _store
    if _default_store is None:
        _default_store = MemorySpotSettingsStore()
    return _default_store
