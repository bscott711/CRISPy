"""A small worker thread for fast, read-only device sampling.

The panel's regular telemetry poll reads ~8 properties every 750 ms. The diagnostic
tools (the Dither assistant and the Hold test) need a few properties much faster than
that, so they run their own reader on a thread while the regular poll is paused, keeping
to one reader on the Tiger serial port at a time.

Nothing here imports Qt, so it is host-agnostic and testable without a display.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from pymmcore_plus import CMMCorePlus

_T = TypeVar("_T")

# Upper bound on the sample rate. The controller updates its values far more slowly
# than this; the cap only stops a very fast (or mocked) link from flooding the GUI.
DEFAULT_MAX_RATE_HZ = 50.0
# Pause after a read that returned nothing, so a dead link doesn't spin.
_FAILED_READ_BACKOFF_S = 0.05
# How often the sampler re-checks that its device is still loaded.
_PRESENCE_CHECK_S = 1.0


def read_float(mmcore: CMMCorePlus, label: str, prop: str) -> float | None:
    """Read a numeric device property, ``None`` if it can't be read or parsed."""
    with suppress(Exception):
        return float(mmcore.getProperty(label, prop))
    return None


def read_str(mmcore: CMMCorePlus, label: str, prop: str) -> str | None:
    """Read a device property as a string, ``None`` if it can't be read."""
    with suppress(Exception):
        return str(mmcore.getProperty(label, prop))
    return None


def device_present(mmcore: CMMCorePlus, label: str) -> bool:
    """Whether *label* is still loaded (``True`` if the core can't tell us).

    The device can vanish under a worker thread (app close, config reload), and a read
    of a missing label only logs core warnings, so samplers check this first.
    """
    with suppress(Exception):
        return label in mmcore.getLoadedDevices()
    return True


class Sampler(Generic[_T]):
    """Free-running worker thread that hands each fresh reading to *on_sample*.

    Parameters
    ----------
    read
        Called repeatedly on the worker thread. Returns a reading, or ``None`` when
        nothing could be read (the sampler then backs off briefly and retries).
    on_sample
        Called on the worker thread with each reading. A GUI must hand the reading over
        to its own thread (e.g. a Qt signal). If it raises, the sampler ends.
    max_rate_hz
        Upper bound on the sample rate.
    present
        Checked about once a second; the sampler ends when it returns ``False``.
    name
        Thread name, for debugging.
    """

    def __init__(
        self,
        read: Callable[[], _T | None],
        on_sample: Callable[[_T], None],
        *,
        max_rate_hz: float = DEFAULT_MAX_RATE_HZ,
        present: Callable[[], bool] | None = None,
        name: str = "CRISPy-sampler",
    ) -> None:
        self._read = read
        self._on_sample = on_sample
        self._min_interval = 1.0 / max_rate_hz
        self._present = present
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start sampling; a no-op if already running. Returns immediately."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Ask the worker to finish and wait up to *timeout* s (one read at most)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the worker thread to end by itself."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        next_presence_check = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            if self._present is not None and started >= next_presence_check:
                next_presence_check = started + _PRESENCE_CHECK_S
                if not self._present():
                    return

            reading = self._read()
            if reading is None:
                wait = _FAILED_READ_BACKOFF_S
            else:
                try:
                    self._on_sample(reading)
                except Exception:  # noqa: BLE001 - a dead consumer ends the sampler
                    return
                wait = max(0.0, self._min_interval - (time.monotonic() - started))
            self._stop.wait(wait)
