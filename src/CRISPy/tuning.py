"""Fast sampling and peak-hold for aligning the CRISP line detector.

Aligning the detector means turning a physical knob on the CRISP unit while watching
how good the signal is. The knob has no software readout, so "the best setting" can
only be found by sweeping it and watching a *peak-held* metric: sweep, note the peak,
then back up to where the live value matches it.

The regular telemetry poll can't support that. It reads ~10 properties per tick every
750 ms, and its fluctuation metric averages the last ~15 s, so everything the knob did
in that time is smeared together. This module is the fast path instead:

* :func:`read_tune_sample` reads only the three properties that matter while
  tuning (SNR, Sum, Dither Error).
* :class:`TuneTracker` keeps a short *time-based* window, so the lag is the same
  whatever the sample rate, and holds the best value seen for each metric.
* :class:`TuneSampler` is a free-running worker thread that feeds the tracker.

Nothing here imports Qt, so it is host-agnostic and testable without a display.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from pymmcore_plus import CMMCorePlus

SNR_PROP = "Signal Noise Ratio"
SUM_PROP = "Sum"
ERROR_PROP = "Dither Error"
REFRESH_PROP = "RefreshPropertyValues"

# Which live metric the tuning meter follows.
Metric = Literal["snr", "sum", "stability"]

# Default rolling window. Short enough to follow a hand-turned knob (the smoothing lag
# is about half of it), long enough for a meaningful std-dev at the achievable rate.
DEFAULT_WINDOW_S = 0.5
# Upper bound on the sample rate. The controller can't update this fast anyway; the cap
# stops a very fast (or mocked) device from flooding the GUI with samples.
DEFAULT_MAX_RATE_HZ = 100.0
# Pause after a sample in which every read failed, so a dead link doesn't spin.
_FAILED_READ_BACKOFF_S = 0.05
# How often the sampler re-checks that its device is still loaded.
_PRESENCE_CHECK_S = 1.0


@dataclass(frozen=True)
class TuneSample:
    """One fast reading. ``None`` marks a property that could not be read."""

    t: float  # time.monotonic() when the reads finished
    snr: float | None
    signal_sum: float | None
    error: float | None  # raw Dither Error, controller counts


@dataclass(frozen=True)
class TuneStats:
    """Windowed values and peaks. ``None`` until the window has enough data."""

    rate_hz: float | None
    snr: float | None  # window mean
    best_snr: float | None
    signal_sum: float | None  # window mean
    best_signal_sum: float | None
    error_std: float | None  # window population std-dev, counts
    best_error_std: float | None  # lowest std-dev seen (most stable)
    error_ptp: float | None  # window peak-to-peak, counts


_EMPTY_STATS = TuneStats(None, None, None, None, None, None, None, None)


def _read_float(mmcore: CMMCorePlus, label: str, prop: str) -> float | None:
    with suppress(Exception):
        return float(mmcore.getProperty(label, prop))
    return None


def read_tune_sample(mmcore: CMMCorePlus, label: str) -> TuneSample:
    """Read just SNR, Sum and Dither Error: three property reads, no writes.

    Deliberately skips the state, the calibration values and the
    ``RefreshPropertyValues`` write that the regular telemetry poll does on each tick.
    """
    snr = _read_float(mmcore, label, SNR_PROP)
    signal_sum = _read_float(mmcore, label, SUM_PROP)
    error = _read_float(mmcore, label, ERROR_PROP)
    return TuneSample(time.monotonic(), snr, signal_sum, error)


def _mean_std_ptp(values: list[float]) -> tuple[float, float, float]:
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(var), max(values) - min(values)


class TuneTracker:
    """Rolling-window statistics and peak-hold over a stream of :class:`TuneSample`.

    SNR and Sum are *smoothed* (window mean) before being peak-held. Holding the peak of
    raw samples would record noise spikes, so the live value could never reach the
    "peak" at the true optimum. A window only counts once it holds at least
    ``min_samples`` readings spanning ``min_fill`` of ``window_s``, so one lucky sample
    right after a reset can't set a record either.

    Not thread-safe; feed it from one thread (the GUI thread).
    """

    def __init__(
        self,
        window_s: float = DEFAULT_WINDOW_S,
        *,
        min_samples: int = 4,
        min_fill: float = 0.8,
        rate_window_s: float = 2.0,
    ) -> None:
        self._window_s = window_s
        self._min_samples = min_samples
        self._min_fill = min_fill
        self._rate_window_s = rate_window_s
        self._times: deque[float] = deque()
        self._snr: deque[tuple[float, float]] = deque()
        self._sum: deque[tuple[float, float]] = deque()
        self._err: deque[tuple[float, float]] = deque()
        self._best_snr: float | None = None
        self._best_sum: float | None = None
        self._best_std: float | None = None
        self._stats = _EMPTY_STATS

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_window(self, seconds: float) -> None:
        """Change the window length; takes effect from the next sample."""
        self._window_s = seconds

    @property
    def stats(self) -> TuneStats:
        """The statistics as of the most recent :meth:`add`."""
        return self._stats

    def reset(self) -> None:
        """Forget all samples and peaks (e.g. before a fresh sweep)."""
        self._times.clear()
        for window in (self._snr, self._sum, self._err):
            window.clear()
        self._best_snr = self._best_sum = self._best_std = None
        self._stats = _EMPTY_STATS

    def clear_error(self) -> None:
        """Drop the error window and its best, but keep the SNR / Sum peaks.

        For when the error datum moves (Set Offset): old Dither Error values are no
        longer comparable, whereas SNR and Sum don't depend on the offset.
        """
        self._err.clear()
        self._best_std = None
        self._refresh(self._stats_time())

    def add(self, sample: TuneSample) -> None:
        """Fold *sample* into the windows and update the peaks."""
        t = sample.t
        self._times.append(t)
        while self._times and self._times[0] < t - self._rate_window_s:
            self._times.popleft()
        for window, value in (
            (self._snr, sample.snr),
            (self._sum, sample.signal_sum),
            (self._err, sample.error),
        ):
            if value is not None:
                window.append((t, value))
        self._refresh(t)

    def fraction(self, metric: Metric) -> float | None:
        """Live value as a fraction of the best seen, ``0..1``; ``None`` if unknown.

        For ``"stability"`` lower std-dev is better, so this is ``best_std / std``.
        """
        s = self._stats
        if metric == "snr":
            live, best = s.snr, s.best_snr
        elif metric == "sum":
            live, best = s.signal_sum, s.best_signal_sum
        else:
            std, best_std = s.error_std, s.best_error_std
            if std is None or best_std is None:
                return None
            return 1.0 if std <= best_std else best_std / std
        if live is None or best is None or best <= 0:
            return None
        return max(0.0, min(1.0, live / best))

    # ----------------------------------------------------------------- internals
    def _stats_time(self) -> float:
        return self._times[-1] if self._times else 0.0

    def _trim(self, window: deque[tuple[float, float]], now: float) -> None:
        # Always keep ``min_samples`` readings, so a slow link (few Hz) still yields
        # statistics; the window then stretches instead of emptying.
        cutoff = now - self._window_s
        while len(window) > self._min_samples and window[0][0] < cutoff:
            window.popleft()

    def _ready(self, window: deque[tuple[float, float]]) -> bool:
        if len(window) < self._min_samples:
            return False
        return window[-1][0] - window[0][0] >= self._min_fill * self._window_s

    def _rate_hz(self) -> float | None:
        if len(self._times) < 2:
            return None
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else None

    def _refresh(self, now: float) -> None:
        for window in (self._snr, self._sum, self._err):
            self._trim(window, now)

        snr = signal_sum = std = ptp = None
        if self._ready(self._snr):
            snr = _mean_std_ptp([v for _, v in self._snr])[0]
            self._best_snr = snr if self._best_snr is None else max(self._best_snr, snr)
        if self._ready(self._sum):
            signal_sum = _mean_std_ptp([v for _, v in self._sum])[0]
            self._best_sum = (
                signal_sum
                if self._best_sum is None
                else max(self._best_sum, signal_sum)
            )
        if self._ready(self._err):
            _, std, ptp = _mean_std_ptp([v for _, v in self._err])
            self._best_std = std if self._best_std is None else min(self._best_std, std)

        self._stats = TuneStats(
            rate_hz=self._rate_hz(),
            snr=snr,
            best_snr=self._best_snr,
            signal_sum=signal_sum,
            best_signal_sum=self._best_sum,
            error_std=std,
            best_error_std=self._best_std,
            error_ptp=ptp,
        )


class TuneSampler:
    """Free-running worker thread that calls *on_sample* with each fresh reading.

    *on_sample* runs on the worker thread, so a GUI must hand the sample over to its
    own thread (e.g. a Qt signal). The sampler ends itself if the callback raises or
    the device disappears from the core; it never touches the GUI.

    Parameters
    ----------
    refresh
        Write ``RefreshPropertyValues="Yes"`` once when sampling starts, for ASITiger
        devices that expose it. Assumed to be a persistent mode, so it is *not* repeated
        per sample; if bench readings turn out frozen, that assumption is wrong and the
        write belongs inside the loop instead.
    max_rate_hz
        Upper bound on the sample rate.
    """

    def __init__(
        self,
        mmcore: CMMCorePlus,
        label: str,
        on_sample: Callable[[TuneSample], None],
        *,
        refresh: bool = False,
        max_rate_hz: float = DEFAULT_MAX_RATE_HZ,
    ) -> None:
        self._mmcore = mmcore
        self._label = label
        self._on_sample = on_sample
        self._refresh = refresh
        self._min_interval = 1.0 / max_rate_hz
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
        self._thread = threading.Thread(
            target=self._run, name=f"CRISPy-tune-{self._label}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Ask the worker to finish and wait up to *timeout* s (one read at most)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _device_present(self) -> bool:
        # The device can vanish under us (app close, config reload); a read of a
        # missing label only logs core warnings, so check before touching it.
        with suppress(Exception):
            return self._label in self._mmcore.getLoadedDevices()
        return True  # can't tell: assume still there rather than give up

    def _run(self) -> None:
        if self._refresh:
            with suppress(Exception):
                self._mmcore.setProperty(self._label, REFRESH_PROP, "Yes")
        next_presence_check = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            if started >= next_presence_check:
                next_presence_check = started + _PRESENCE_CHECK_S
                if not self._device_present():
                    return

            sample = read_tune_sample(self._mmcore, self._label)
            if (
                sample.snr is None
                and sample.signal_sum is None
                and sample.error is None
            ):
                wait = _FAILED_READ_BACKOFF_S
            else:
                try:
                    self._on_sample(sample)
                except Exception:  # noqa: BLE001 - a dead consumer ends the sampler
                    return
                wait = max(0.0, self._min_interval - (time.monotonic() - started))
            self._stop.wait(wait)
