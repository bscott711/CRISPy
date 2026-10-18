"""Tests for the Qt-free fast tuning path: reads, rolling window, peak-hold, sampler."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from CRISPy.controller import counts_per_um
from CRISPy.tuning import (
    ERROR_PROP,
    REFRESH_PROP,
    SNR_PROP,
    SUM_PROP,
    TuneSample,
    TuneSampler,
    TuneTracker,
    read_tune_sample,
)

LABEL = "CRISPAFocus:P:34"


def _core(props: dict[str, str] | None = None) -> MagicMock:
    values = {SNR_PROP: "5.0", SUM_PROP: "60", ERROR_PROP: "-4"}
    values.update(props or {})
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: values[prop]
    core.getLoadedDevices.return_value = [LABEL]
    return core


def _feed(
    tracker: TuneTracker,
    n: int,
    *,
    rate_hz: float = 100.0,
    t0: float = 0.0,
    snr: float | None = 5.0,
    signal_sum: float | None = 60.0,
    error: float | None = 0.0,
) -> float:
    """Add *n* samples at *rate_hz* starting at *t0*; return the next free time."""
    dt = 1.0 / rate_hz
    t = t0
    for _ in range(n):
        tracker.add(TuneSample(t, snr, signal_sum, error))
        t += dt
    return t


# ------------------------------------------------------------- read_tune_sample
def test_read_tune_sample_reads_exactly_three_props_and_writes_nothing() -> None:
    core = _core()
    sample = read_tune_sample(core, LABEL)
    props = [c.args[1] for c in core.getProperty.call_args_list]
    assert props == [SNR_PROP, SUM_PROP, ERROR_PROP]
    core.setProperty.assert_not_called()
    assert (sample.snr, sample.signal_sum, sample.error) == (5.0, 60.0, -4.0)


def test_read_tune_sample_marks_unreadable_props_none() -> None:
    core = MagicMock()

    def get(label: str, prop: str) -> str:
        if prop == SUM_PROP:
            raise RuntimeError("serial timeout")
        return "3" if prop == SNR_PROP else "not-a-number"

    core.getProperty.side_effect = get
    sample = read_tune_sample(core, LABEL)
    assert sample.snr == 3.0
    assert sample.signal_sum is None
    assert sample.error is None


# ----------------------------------------------------------------- TuneTracker
def test_stats_are_empty_until_the_window_has_filled() -> None:
    tr = TuneTracker(window_s=0.5)
    _feed(tr, 3)  # fewer than min_samples
    assert tr.stats.snr is None
    assert tr.stats.error_std is None
    assert tr.stats.best_snr is None
    t = _feed(tr, 3, t0=0.03)  # 6 samples now, but spanning only 50 ms of 0.5 s
    assert tr.stats.snr is None
    assert tr.stats.best_error_std is None
    _feed(tr, 100, t0=t)
    assert tr.stats.snr == pytest.approx(5.0)


def test_window_is_time_based_not_count_based() -> None:
    # Same 0.5 s window at 100 Hz and at 20 Hz covers the same time span.
    for rate in (100.0, 20.0):
        tr = TuneTracker(window_s=0.5)
        _feed(tr, int(rate * 3), rate_hz=rate)
        # Old, very different values must have aged out of the window entirely.
        assert tr.stats.snr == pytest.approx(5.0)
    tr = TuneTracker(window_s=0.5)
    t = _feed(tr, 100, snr=1.0)  # 1 s at SNR 1
    _feed(tr, 100, t0=t, snr=9.0)  # 1 s at SNR 9
    assert tr.stats.snr == pytest.approx(9.0)  # no memory of the SNR-1 second


def test_error_std_and_peak_to_peak() -> None:
    tr = TuneTracker(window_s=0.5)
    dt = 0.01
    for i in range(100):
        tr.add(TuneSample(i * dt, 5.0, 60.0, 10.0 if i % 2 else -10.0))
    assert tr.stats.error_std == pytest.approx(10.0, rel=0.05)
    assert tr.stats.error_ptp == pytest.approx(20.0)


def test_peak_hold_tracks_best_while_live_value_falls_away() -> None:
    tr = TuneTracker(window_s=0.2)
    t = 0.0
    # Sweep SNR up to 8 and back down to 2, like turning the knob past the optimum.
    for snr in [2, 4, 6, 8, 6, 4, 2]:
        t = _feed(tr, 40, t0=t, snr=float(snr))
    assert tr.stats.best_snr == pytest.approx(8.0, abs=0.1)
    assert tr.stats.snr == pytest.approx(2.0, abs=0.1)
    assert tr.fraction("snr") == pytest.approx(0.25, abs=0.02)


def test_live_value_reaches_the_peak_when_back_at_the_optimum() -> None:
    tr = TuneTracker(window_s=0.2)
    t = _feed(tr, 40, snr=8.0)
    t = _feed(tr, 40, t0=t, snr=3.0)
    _feed(tr, 40, t0=t, snr=8.0)
    assert tr.fraction("snr") == pytest.approx(1.0)


def test_peak_is_of_the_smoothed_series_so_a_noise_spike_is_not_a_record() -> None:
    tr = TuneTracker(window_s=0.5)
    t = _feed(tr, 100, snr=5.0)
    tr.add(TuneSample(t, 50.0, 60.0, 0.0))  # one wild sample
    assert tr.stats.best_snr is not None
    assert tr.stats.best_snr < 6.0


def test_best_stability_is_the_lowest_std_seen() -> None:
    tr = TuneTracker(window_s=0.2)
    dt = 0.01
    t = 0.0
    for amp in (20.0, 2.0, 20.0):  # steady -> quiet -> noisy again
        for i in range(60):
            tr.add(TuneSample(t, 5.0, 60.0, amp if i % 2 else -amp))
            t += dt
    assert tr.stats.best_error_std == pytest.approx(2.0, rel=0.1)
    assert tr.stats.error_std == pytest.approx(20.0, rel=0.1)
    assert tr.fraction("stability") == pytest.approx(0.1, rel=0.2)


def test_fraction_is_none_when_nothing_is_known_or_best_is_not_positive() -> None:
    tr = TuneTracker()
    assert tr.fraction("snr") is None
    assert tr.fraction("stability") is None
    _feed(tr, 100, snr=-1.0)
    assert tr.fraction("snr") is None  # best <= 0 gives no meaningful ratio


def test_fraction_for_a_perfectly_steady_signal_is_one() -> None:
    tr = TuneTracker(window_s=0.2)
    _feed(tr, 60, error=3.0)  # constant -> std 0
    assert tr.stats.error_std == 0.0
    assert tr.fraction("stability") == 1.0


def test_reset_clears_windows_and_peaks() -> None:
    tr = TuneTracker(window_s=0.2)
    _feed(tr, 60)
    assert tr.stats.best_snr is not None
    tr.reset()
    s = tr.stats
    assert s.best_snr is None
    assert s.snr is None
    assert s.rate_hz is None


def test_clear_error_keeps_snr_and_sum_peaks() -> None:
    tr = TuneTracker(window_s=0.2)
    _feed(tr, 60, error=3.0)
    tr.clear_error()
    s = tr.stats
    assert s.best_error_std is None
    assert s.error_std is None
    assert s.best_snr == pytest.approx(5.0)
    assert s.best_signal_sum == pytest.approx(60.0)


def test_slow_link_still_produces_stats_by_stretching_the_window() -> None:
    # 3 Hz: a 0.5 s window would hold ~1 reading. The window keeps min_samples instead.
    tr = TuneTracker(window_s=0.5)
    _feed(tr, 12, rate_hz=3.0, snr=4.0)
    assert tr.stats.snr == pytest.approx(4.0)
    assert tr.stats.rate_hz == pytest.approx(3.0, rel=0.05)


def test_rate_estimate() -> None:
    tr = TuneTracker()
    _feed(tr, 200, rate_hz=50.0)
    assert tr.stats.rate_hz == pytest.approx(50.0, rel=0.02)


def test_missing_metric_does_not_block_the_others() -> None:
    tr = TuneTracker(window_s=0.2)
    _feed(tr, 60, snr=None)  # SNR unreadable
    s = tr.stats
    assert s.snr is None
    assert s.best_snr is None
    assert s.signal_sum == pytest.approx(60.0)


def test_set_window_changes_the_lag() -> None:
    tr = TuneTracker(window_s=2.0)
    t = _feed(tr, 200, snr=1.0)
    tr.set_window(0.2)
    _feed(tr, 40, t0=t, snr=9.0)
    assert tr.window_s == 0.2
    assert tr.stats.snr == pytest.approx(9.0, abs=0.5)


# ------------------------------------------------------------------ TuneSampler
class _Collector:
    def __init__(self, want: int) -> None:
        self.samples: list[TuneSample] = []
        self.done = threading.Event()
        self._want = want

    def __call__(self, sample: TuneSample) -> None:
        self.samples.append(sample)
        if len(self.samples) >= self._want:
            self.done.set()


def test_sampler_delivers_samples_using_only_the_three_reads() -> None:
    core = _core()
    got = _Collector(5)
    sampler = TuneSampler(core, LABEL, got, max_rate_hz=1000.0)
    sampler.start()
    try:
        assert got.done.wait(2.0)
    finally:
        sampler.stop()
    assert not sampler.running
    assert {c.args[1] for c in core.getProperty.call_args_list} == {
        SNR_PROP,
        SUM_PROP,
        ERROR_PROP,
    }
    core.setProperty.assert_not_called()


def test_sampler_writes_refresh_once_not_per_sample() -> None:
    core = _core()
    got = _Collector(10)
    sampler = TuneSampler(core, LABEL, got, refresh=True, max_rate_hz=1000.0)
    sampler.start()
    try:
        assert got.done.wait(2.0)
    finally:
        sampler.stop()
    core.setProperty.assert_called_once_with(LABEL, REFRESH_PROP, "Yes")


def test_sampler_respects_max_rate() -> None:
    core = _core()
    got = _Collector(10**9)
    sampler = TuneSampler(core, LABEL, got, max_rate_hz=50.0)
    sampler.start()
    got.done.wait(0.5)
    sampler.stop()
    assert 10 <= len(got.samples) <= 35  # ~25 expected at 50 Hz for 0.5 s


def test_sampler_stops_when_the_callback_raises() -> None:
    core = _core()
    calls: list[TuneSample] = []

    def boom(sample: TuneSample) -> None:
        calls.append(sample)
        raise RuntimeError("consumer is gone")

    sampler = TuneSampler(core, LABEL, boom, max_rate_hz=1000.0)
    sampler.start()
    sampler._thread.join(2.0)  # type: ignore[union-attr]
    assert not sampler.running
    assert len(calls) == 1


def test_sampler_stops_when_the_device_disappears() -> None:
    core = _core()
    core.getLoadedDevices.return_value = []
    got = _Collector(1)
    sampler = TuneSampler(core, LABEL, got)
    sampler.start()
    sampler._thread.join(2.0)  # type: ignore[union-attr]
    assert not sampler.running
    core.getProperty.assert_not_called()
    assert got.samples == []


def test_sampler_survives_total_read_failure_without_delivering() -> None:
    core = MagicMock()
    core.getProperty.side_effect = RuntimeError("link down")
    core.getLoadedDevices.return_value = [LABEL]
    got = _Collector(1)
    sampler = TuneSampler(core, LABEL, got)
    sampler.start()
    try:
        assert not got.done.wait(0.3)
        assert sampler.running
    finally:
        sampler.stop()
    assert got.samples == []


def test_sampler_start_and_stop_are_idempotent() -> None:
    core = _core()
    got = _Collector(1)
    sampler = TuneSampler(core, LABEL, got)
    sampler.stop()  # before start: harmless
    sampler.start()
    sampler.start()  # second start must not spawn a second thread
    try:
        assert got.done.wait(2.0)
    finally:
        sampler.stop()
        sampler.stop()
    assert not sampler.running


def test_sampler_can_be_restarted() -> None:
    core = _core()
    first = _Collector(1)
    sampler = TuneSampler(core, LABEL, first, max_rate_hz=1000.0)
    sampler.start()
    assert first.done.wait(2.0)
    sampler.stop()
    seen = len(first.samples)
    sampler.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.2)
    finally:
        sampler.stop()
    assert len(first.samples) > seen


# ------------------------------------------------------------- counts_per_um
@pytest.mark.parametrize(
    ("gain", "rng", "expected"),
    [
        (-377.0, 0.675, 377 / 0.675),
        (2524.0, 0.677, 2524 / 0.677),
        (None, 0.675, None),
        (-377.0, None, None),
        (-377.0, 0.0, None),
        (-377.0, -1.0, None),
    ],
)
def test_counts_per_um(gain: float | None, rng: float | None, expected: float | None):
    result = counts_per_um(gain, rng)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
