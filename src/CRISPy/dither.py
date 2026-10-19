"""Readout and peak-hold for aligning the CRISP detector in the Dither state.

From the ASI CRISP manual (Focus Dither for Optical Adjustments): in the Dither state
the focus moves up and down by ``cal_range`` and the Err number is the *change* in focus
error across that move. Slowly turn the **detector lateral adjustment screw** for the
maximum *absolute* Err ("Large negative numbers are just as good as large positive
numbers"), pausing after each adjustment because "Motion of the detector will give
large temporary values". Keep Sum in 50-80; 0 or 100 means a detector half saturated,
and the log-amp calibration should be redone. Targets: >50 (manual), at least ±100
(ASI's Micro-Manager plugin), >200 is "plenty of signal".

:class:`DitherTracker` turns the raw Err readings into a median-filtered |Err| (which
rejects brief spikes), holds the best value seen while the readings were steady (so a
temporary value that takes a while to decay can't become the peak; see
:data:`STEADY_TREND`), and keeps a short trace for plotting, so a slow sweep of the
screw shows where the maximum is.

Nothing here imports Qt.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .sampling import read_float, read_str

if TYPE_CHECKING:
    from pymmcore_plus import CMMCorePlus

ERROR_PROP = "Dither Error"
SUM_PROP = "Sum"
STATE_PROP = "CRISP State"

# ASI targets for the dither Err (absolute value).
ERR_MANUAL_MIN = (
    50.0  # manual: "For best performance you would like to have a value >50"
)
ERR_TARGET = 100.0  # ASI CRISP plugin: "It must be at least +/- 100"
ERR_PLENTY = 200.0  # manual: "If you have plenty of signal (dither Err number > 200)"
# ASI target band for Sum; 0 and 100 mean a detector half is saturated.
SUM_LOW = 50.0
SUM_HIGH = 80.0

# While dithering, the controller cycles through sub-states. The ASITiger adapter
# reports 'f' (Dither Start) as "Dither" but 'g'-'j' and 't' as "Calibrating", so both
# count as still dithering. (Log Cal and Gain Cal also briefly read "Calibrating".)
DITHER_STATES = frozenset({"Dither", "Calibrating"})

DEFAULT_WINDOW_S = 1.0
DEFAULT_TRACE_S = 30.0
# The peak only latches while the window is steady: the medians of its three thirds
# agree within this fraction of the level. A temporary value from moving the detector
# rises and decays across the window, so it doesn't count until it has settled,
# however long it lasts (the tail of a decay can still latch, up to about this fraction
# above the settled value). On a noisy plateau the check sometimes fails by chance,
# which only delays the latch; a false peak would be worse.
STEADY_TREND = 0.10

SumStatus = Literal["ok", "low", "high", "saturated"]


@dataclass(frozen=True)
class DitherSample:
    """One fast reading. ``None`` marks a value that wasn't read or couldn't be."""

    t: float  # time.monotonic() when the reads finished
    err: float | None
    signal_sum: float | None
    state: str | None = None


@dataclass(frozen=True)
class DitherStats:
    """The tracker's view after the latest sample."""

    rate_hz: float | None
    raw_err: float | None  # latest raw reading (signed)
    level: float | None  # median |Err| over the window; None until the window fills
    sign: int  # sign of the window's median Err (+1 / -1 / 0)
    # (p90 - p10) / 2 of |Err| in the window, as a fraction of level
    spread: float | None
    # Scatter of |Err| in the window in counts: a robust std-dev, (p90 - p10) / 2.56.
    # Unlike spread it doesn't depend on the level, so axes can be compared directly.
    noise: float | None
    steady: bool  # no trend across the window (see STEADY_TREND)
    peak: float | None  # best steady level seen since the last reset
    peak_t: float | None
    signal_sum: float | None  # latest Sum
    sum_status: SumStatus | None


_EMPTY = DitherStats(None, None, None, 0, None, None, False, None, None, None, None)
# p90 - p10 of a normal distribution, in standard deviations.
_P90_P10_SIGMAS = 2.563


def read_dither_sample(
    mmcore: CMMCorePlus, label: str, *, with_state: bool = False
) -> DitherSample | None:
    """Read Dither Error and Sum (and the state if *with_state*); no writes.

    Returns ``None`` if nothing at all could be read.
    """
    err = read_float(mmcore, label, ERROR_PROP)
    signal_sum = read_float(mmcore, label, SUM_PROP)
    state = read_str(mmcore, label, STATE_PROP) if with_state else None
    if err is None and signal_sum is None and state is None:
        return None
    return DitherSample(time.monotonic(), err, signal_sum, state)


def sum_status(value: float | None) -> SumStatus | None:
    """Classify a Sum reading against ASI's 50-80 target band."""
    if value is None:
        return None
    if value <= 0 or value >= 100:
        return "saturated"
    if value < SUM_LOW:
        return "low"
    if value > SUM_HIGH:
        return "high"
    return "ok"


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank quantile of an already sorted, non-empty list."""
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _median(sorted_vals: list[float]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


class DitherTracker:
    """Median-filtered |Err|, peak-hold, Sum check and a plot trace.

    A window only counts once it holds at least *min_samples* readings spanning
    *min_fill* of *window_s*, so a reset can't be followed by a one-sample record.

    Parameters
    ----------
    window_s
        Length of the median window. Longer rejects longer-lived temporary values
        from detector motion but lags the screw more.
    trace_s
        How much history :meth:`trace` keeps for plotting.
    min_samples
        Minimum readings in the window before it counts.
    min_fill
        Minimum fraction of *window_s* the window must span before it counts.

    Not thread-safe; feed it from one thread (the GUI thread).
    """

    def __init__(
        self,
        window_s: float = DEFAULT_WINDOW_S,
        trace_s: float = DEFAULT_TRACE_S,
        *,
        min_samples: int = 3,
        min_fill: float = 0.6,
    ) -> None:
        self._window_s = window_s
        self._trace_s = trace_s
        self._min_samples = min_samples
        self._min_fill = min_fill
        self._win: deque[tuple[float, float]] = deque()  # (t, signed err)
        # Every reading for the last trace_s, so discard_after() can rebuild the window.
        self._raw: deque[tuple[float, float]] = deque()
        self._times: deque[float] = deque()  # for the rate estimate
        self._trace: deque[tuple[float, float, float | None]] = deque()
        self._peak: float | None = None
        self._peak_t: float | None = None
        self._last_sum: float | None = None
        self._last_err: float | None = None
        self._stats = _EMPTY

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_window(self, seconds: float) -> None:
        """Change the median window; takes effect from the next sample."""
        self._window_s = seconds

    @property
    def stats(self) -> DitherStats:
        return self._stats

    def trace(self) -> list[tuple[float, float, float | None]]:
        """``(t, |raw Err|, filtered level or None)`` for the last *trace_s* seconds."""
        return list(self._trace)

    def reset_peak(self) -> None:
        """Forget the best value (e.g. before a fresh sweep), keep the live window."""
        self._peak = self._peak_t = None
        self._stats = self._compute(self._stats_time())

    def discard_after(self, t: float) -> None:
        """Drop readings taken after time *t* (e.g. once the dither has ended).

        The state is only read every so often, so the first readings after the dither
        stops arrive before the tracker knows; this removes them from the window and
        the trace. A peak is never set by them (they fall towards 0).
        """
        while self._raw and self._raw[-1][0] > t:
            self._raw.pop()
        while self._trace and self._trace[-1][0] > t:
            self._trace.pop()
        while self._times and self._times[-1] > t:
            self._times.pop()
        # Rebuild the window from the kept readings: the newer ones it held are gone,
        # and older ones it had already let go of belong in it again.
        end = self._raw[-1][0] if self._raw else t
        self._win = deque(e for e in self._raw if e[0] >= end - self._window_s)
        if self._raw:
            self._last_err = self._raw[-1][1]
        self._stats = self._compute(self._stats_time())

    def reset(self) -> None:
        """Forget everything."""
        self._win.clear()
        self._raw.clear()
        self._times.clear()
        self._trace.clear()
        self._peak = self._peak_t = None
        self._last_sum = self._last_err = None
        self._stats = _EMPTY

    def add(self, sample: DitherSample) -> None:
        """Fold *sample* into the window, the peak and the trace."""
        t = sample.t
        self._times.append(t)
        while self._times and self._times[0] < t - 2.0:
            self._times.popleft()
        if sample.signal_sum is not None:
            self._last_sum = sample.signal_sum
        if sample.err is not None:
            self._last_err = sample.err
            self._win.append((t, sample.err))
            self._raw.append((t, sample.err))
            while self._raw and self._raw[0][0] < t - self._trace_s:
                self._raw.popleft()
        cutoff = t - self._window_s
        while self._win and self._win[0][0] < cutoff:
            self._win.popleft()

        self._stats = self._compute(t)
        level = self._stats.level
        if sample.err is not None:
            self._trace.append((t, abs(sample.err), level))
        while self._trace and self._trace[0][0] < t - self._trace_s:
            self._trace.popleft()

    # ----------------------------------------------------------------- internals
    def _stats_time(self) -> float:
        return self._times[-1] if self._times else 0.0

    def _ready(self) -> bool:
        if len(self._win) < self._min_samples:
            return False
        return self._win[-1][0] - self._win[0][0] >= self._min_fill * self._window_s

    def _rate_hz(self) -> float | None:
        if len(self._times) < 2:
            return None
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else None

    def _steady(self, level: float) -> bool:
        """Whether the window's thirds agree (no trend or hump across it).

        Thirds rather than halves: a jump that rises and decays inside the window can
        leave two halves with similar medians, but not three thirds.
        """
        if level <= 0:
            return False
        t0, t1 = self._win[0][0], self._win[-1][0]
        span = (t1 - t0) / 3 or 1.0
        parts: list[list[float]] = [[], [], []]
        for t, v in self._win:
            parts[min(2, int((t - t0) / span))].append(abs(v))
        if not all(parts):
            return False
        medians = [_median(sorted(p)) for p in parts]
        return max(medians) - min(medians) <= STEADY_TREND * level

    def _compute(self, now: float) -> DitherStats:
        level = spread = noise = None
        sign = 0
        steady = False
        if self._ready():
            signed = sorted(v for _, v in self._win)
            mags = sorted(abs(v) for v in signed)
            level = _median(mags)
            med = _median(signed)
            sign = (med > 0) - (med < 0)
            p10_p90 = _quantile(mags, 0.9) - _quantile(mags, 0.1)
            noise = p10_p90 / _P90_P10_SIGMAS
            if level > 0:
                spread = p10_p90 / 2 / level
            steady = self._steady(level)
            if steady and (self._peak is None or level > self._peak):
                self._peak, self._peak_t = level, now
        return DitherStats(
            rate_hz=self._rate_hz(),
            raw_err=self._last_err,
            level=level,
            sign=sign,
            spread=spread,
            noise=noise,
            steady=steady,
            peak=self._peak,
            peak_t=self._peak_t,
            signal_sum=self._last_sum,
            sum_status=sum_status(self._last_sum),
        )
