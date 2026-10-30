"""Tests for the Dither assistant's Qt-free tracker and reads."""

from __future__ import annotations

import math
import random
from unittest.mock import MagicMock

import pytest

from CRISPy.dither import (
    DITHER_STATES,
    ERROR_PROP,
    STATE_PROP,
    STEADY_TREND,
    SUM_PROP,
    DitherSample,
    DitherTracker,
    read_dither_sample,
    sum_status,
)

LABEL = "CRISPAFocus:P:34"


def _feed(
    tr: DitherTracker,
    values: list[float],
    *,
    rate_hz: float = 20.0,
    t0: float = 0.0,
    signal_sum: float | None = 65.0,
) -> float:
    t = t0
    for v in values:
        tr.add(DitherSample(t, v, signal_sum))
        t += 1.0 / rate_hz
    return t


# ---------------------------------------------------------------------------- reads
def test_read_dither_sample_reads_err_and_sum_only_by_default() -> None:
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: {
        ERROR_PROP: "-145",
        SUM_PROP: "71",
    }[prop]
    s = read_dither_sample(core, LABEL)
    assert s is not None
    assert (s.err, s.signal_sum, s.state) == (-145.0, 71.0, None)
    assert [c.args[1] for c in core.getProperty.call_args_list] == [
        ERROR_PROP,
        SUM_PROP,
    ]
    core.setProperty.assert_not_called()


def test_read_dither_sample_adds_the_state_when_asked() -> None:
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: {
        ERROR_PROP: "10",
        SUM_PROP: "60",
        STATE_PROP: "Calibrating",
    }[prop]
    s = read_dither_sample(core, LABEL, with_state=True)
    assert s is not None
    assert s.state == "Calibrating"


def test_read_dither_sample_is_none_when_nothing_reads() -> None:
    core = MagicMock()
    core.getProperty.side_effect = RuntimeError("link down")
    assert read_dither_sample(core, LABEL, with_state=True) is None


def test_dither_states_include_the_calibrating_substates() -> None:
    # The adapter reports dither sub-states g-j and t as "Calibrating".
    assert {"Dither", "Calibrating"} <= DITHER_STATES
    assert "Ready" not in DITHER_STATES


# ------------------------------------------------------------------------ sum_status
@pytest.mark.parametrize(
    ("value", "status"),
    [
        (None, None),
        (0, "saturated"),
        (100, "saturated"),
        (49, "low"),
        (50, "ok"),
        (80, "ok"),
        (81, "high"),
    ],
)
def test_sum_status(value: float | None, status: str | None) -> None:
    assert sum_status(value) == status


# --------------------------------------------------------------------------- tracker
def test_level_is_the_median_of_abs_err_and_needs_a_full_window() -> None:
    tr = DitherTracker(window_s=1.0)
    _feed(tr, [120.0, 130.0])
    assert tr.stats.level is None  # too few samples / too short a span
    tr2 = DitherTracker(window_s=1.0)
    _feed(tr2, [120.0, 130.0, 125.0] * 10)
    assert tr2.stats.level == pytest.approx(125.0)


def test_a_burst_of_samples_spanning_too_short_a_time_does_not_count() -> None:
    # Enough samples, but they span 40 ms of a 1 s window: no level, no peak yet.
    tr = DitherTracker(window_s=1.0)
    _feed(tr, [500.0] * 5, rate_hz=100.0)
    assert tr.stats.level is None
    assert tr.stats.peak is None


def test_negative_err_counts_as_strongly_as_positive() -> None:
    tr = DitherTracker(window_s=1.0)
    _feed(tr, [-150.0] * 30)
    assert tr.stats.level == pytest.approx(150.0)
    assert tr.stats.sign == -1


def test_median_rejects_brief_jumps_from_moving_the_detector() -> None:
    tr = DitherTracker(window_s=1.0)
    t = _feed(tr, [100.0] * 30)
    t = _feed(tr, [900.0, 950.0], t0=t)  # two-sample transient at 20 Hz
    _feed(tr, [100.0] * 5, t0=t)
    assert tr.stats.level == pytest.approx(100.0)
    assert tr.stats.peak == pytest.approx(100.0)  # the transient never became the peak


def test_a_slowly_decaying_jump_never_becomes_the_peak() -> None:
    # Moving the detector: a jump of +600 that decays over ~2 s, on top of a true 300.
    tr = DitherTracker(window_s=1.0)
    t = _feed(tr, [300.0] * 40)
    decay = [
        300.0 + 600.0 * math.exp(-k / 20 / 0.6) for k in range(120)
    ]  # 6 s at 20 Hz
    _feed(tr, decay, t0=t)
    # Only the tail of the decay can latch: within STEADY_TREND of the settled value.
    assert tr.stats.peak == pytest.approx(300.0, rel=STEADY_TREND)
    assert tr.stats.steady


def test_while_decaying_the_level_is_flagged_not_steady() -> None:
    tr = DitherTracker(window_s=1.0)
    t = _feed(tr, [300.0] * 40)
    _feed(tr, [300.0 + 600.0 * math.exp(-k / 20 / 0.6) for k in range(15)], t0=t)
    assert not tr.stats.steady


def test_a_noisy_plateau_sets_the_peak_during_a_pause() -> None:
    # ±12% fluctuations: the steadiness check may fail on some samples, but within a
    # 3 s pause the plateau must latch, at an unbiased value.
    for seed in range(20):
        rng = random.Random(seed)
        tr = DitherTracker(window_s=1.0)
        _feed(tr, [250.0 + rng.gauss(0, 30) for _ in range(60)])
        assert tr.stats.peak == pytest.approx(250.0, rel=0.1), seed


def test_peak_holds_while_the_level_falls_away() -> None:
    tr = DitherTracker(window_s=0.5)
    t = 0.0
    # Sweep through a maximum of 200 and past it.
    for v in [60, 90, 130, 170, 200, 170, 120, 80]:
        t = _feed(tr, [float(v)] * 20, t0=t)
    assert tr.stats.peak == pytest.approx(200.0)
    assert tr.stats.level == pytest.approx(80.0)
    assert tr.stats.peak_t is not None


def test_reset_peak_starts_from_the_current_level() -> None:
    tr = DitherTracker(window_s=0.5)
    t = _feed(tr, [200.0] * 20)
    _feed(tr, [80.0] * 20, t0=t)
    tr.reset_peak()
    assert tr.stats.peak == pytest.approx(80.0)


def test_reset_forgets_everything() -> None:
    tr = DitherTracker(window_s=0.5)
    _feed(tr, [150.0] * 20)
    tr.reset()
    s = tr.stats
    assert (s.level, s.peak, s.signal_sum, s.rate_hz) == (None, None, None, None)
    assert tr.trace() == []


def test_spread_reports_fluctuation_as_a_fraction_of_the_level() -> None:
    tr = DitherTracker(window_s=1.0)
    _feed(tr, [90.0, 110.0] * 15)
    assert tr.stats.spread == pytest.approx(0.1)


def test_noise_is_the_scatter_in_counts_independent_of_level() -> None:
    rng = random.Random(1)
    small = DitherTracker(window_s=3.0)
    _feed(small, [70.0 + rng.gauss(0, 9) for _ in range(60)])
    big = DitherTracker(window_s=3.0)
    _feed(big, [1400.0 + rng.gauss(0, 9) for _ in range(60)])
    assert small.stats.noise == pytest.approx(9.0, rel=0.35)
    assert big.stats.noise == pytest.approx(9.0, rel=0.35)
    assert small.stats.spread > 10 * big.stats.spread  # same noise, very different %


def test_discard_after_drops_readings_from_after_the_dither() -> None:
    tr = DitherTracker(window_s=1.0)
    t = _feed(tr, [115.0] * 40)  # 2 s of dithering
    t_last_dither = t - 0.05
    _feed(tr, [0.0, 1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], t0=t)  # Ready
    assert tr.stats.level < 50  # the tail dragged it down
    tr.discard_after(t_last_dither)
    assert tr.stats.level == pytest.approx(115.0)
    assert tr.stats.raw_err == 115.0
    assert tr.stats.peak == pytest.approx(115.0)
    assert all(t <= t_last_dither for t, _, _ in tr.trace())


def test_sum_is_tracked_and_classified() -> None:
    tr = DitherTracker()
    _feed(tr, [100.0] * 5, signal_sum=0.0)
    assert tr.stats.signal_sum == 0.0
    assert tr.stats.sum_status == "saturated"


def test_a_missing_err_keeps_the_last_sum() -> None:
    tr = DitherTracker()
    tr.add(DitherSample(0.0, None, 72.0))
    assert tr.stats.signal_sum == 72.0
    assert tr.stats.level is None


def test_trace_keeps_only_the_last_trace_s_seconds() -> None:
    tr = DitherTracker(window_s=0.5, trace_s=5.0)
    _feed(tr, [100.0] * 400, rate_hz=20.0)  # 20 s
    trace = tr.trace()
    assert trace[-1][0] - trace[0][0] <= 5.0
    assert all(raw == 100.0 for _, raw, _ in trace)


def test_rate_estimate() -> None:
    tr = DitherTracker()
    _feed(tr, [100.0] * 100, rate_hz=25.0)
    assert tr.stats.rate_hz == pytest.approx(25.0, rel=0.05)


def test_set_window_changes_the_lag() -> None:
    tr = DitherTracker(window_s=3.0)
    t = _feed(tr, [50.0] * 60)
    tr.set_window(0.3)
    _feed(tr, [200.0] * 10, t0=t)
    assert tr.window_s == 0.3
    assert tr.stats.level == pytest.approx(200.0)
