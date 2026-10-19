"""Tests for the hold test's analysis and its worker thread (with a fake core)."""

from __future__ import annotations

import math
import random
import threading
from typing import TYPE_CHECKING

import pytest

from CRISPy.hold import (
    HoldResult,
    HoldSample,
    HoldTest,
    analyze_hold,
    write_hold_csv,
)

if TYPE_CHECKING:
    from pathlib import Path

LABEL = "CRISPAFocus:P:34"
ACT = "PiezoStage:P:34"


def _samples(fn, *, duration: float = 60.0, rate: float = 20.0) -> list[HoldSample]:
    n = int(duration * rate)
    return [HoldSample(k / rate, fn(k / rate), 0.0) for k in range(n)]


# ------------------------------------------------------------------------- analysis
def test_drift_is_the_linear_trend_in_nm_per_min() -> None:
    # 25 um + 12 nm/min
    stats = analyze_hold(_samples(lambda t: 25.0 + 0.012 * t / 60))
    assert stats.drift_nm_per_min == pytest.approx(12.0, rel=1e-6)
    assert stats.rms_nm == pytest.approx(0.0, abs=1e-6)
    assert stats.pkpk_nm == pytest.approx(0.0, abs=1e-6)


def test_slow_motion_is_wander_and_fast_motion_is_jitter() -> None:
    # 0.1 Hz, 50 nm amplitude: almost all of it is slower than 1 s. What lands in
    # jitter is the rise within each 1 s bin: rms slope (2*pi*f*A/sqrt(2)) / sqrt(12).
    slow = analyze_hold(
        _samples(lambda t: 25.0 + 0.05 * math.sin(2 * math.pi * 0.1 * t))
    )
    assert slow.wander_nm == pytest.approx(50 / math.sqrt(2), rel=0.1)
    assert slow.jitter_nm == pytest.approx(
        2 * math.pi * 0.1 * 50 / math.sqrt(24), rel=0.1
    )
    assert slow.jitter_nm < slow.wander_nm / 4

    # White noise sampled at 20 Hz: mostly faster than 1 s.
    rng = random.Random(0)
    fast = analyze_hold(_samples(lambda t: 25.0 + rng.gauss(0, 0.02)))
    assert fast.jitter_nm == pytest.approx(20.0, rel=0.1)
    assert fast.wander_nm < fast.jitter_nm / 3


def test_jitter_and_wander_add_in_quadrature_to_the_total() -> None:
    rng = random.Random(3)
    stats = analyze_hold(
        _samples(
            lambda t: (
                25.0
                + 0.03 * math.sin(2 * math.pi * 0.05 * t)
                + rng.gauss(0, 0.01)
                + 0.001 * t
            )
        )
    )
    assert stats.jitter_nm**2 + stats.wander_nm**2 == pytest.approx(stats.rms_nm**2)


def test_error_statistics_and_rate() -> None:
    samples = [HoldSample(k * 0.1, 25.0, (-1) ** k * 3.0) for k in range(101)]
    stats = analyze_hold(samples)
    assert stats.error_rms == pytest.approx(3.0, rel=0.01)
    assert stats.error_mean == pytest.approx(0.0, abs=0.05)
    assert stats.rate_hz == pytest.approx(10.0)
    assert stats.duration_s == pytest.approx(10.0)


def test_missing_positions_give_no_position_stats() -> None:
    stats = analyze_hold([HoldSample(k * 0.1, None, 1.0) for k in range(20)])
    assert stats.rms_nm is None
    assert stats.drift_nm_per_min is None
    assert stats.error_mean == pytest.approx(1.0)


def test_no_samples() -> None:
    stats = analyze_hold([])
    assert stats.n == 0
    assert stats.rms_nm is None


def test_lock_offset_change() -> None:
    r = HoldResult(LABEL, ACT, None, {}, 474.0, 480.0, analyze_hold([]))  # type: ignore[arg-type]
    assert r.lock_offset_change == 6.0
    r2 = HoldResult(LABEL, ACT, None, {}, None, 480.0, analyze_hold([]))  # type: ignore[arg-type]
    assert r2.lock_offset_change is None


# --------------------------------------------------------------------------- worker
class FakeCore:
    def __init__(self, *, states: list[str] | None = None, loaded: bool = True) -> None:
        self.states = states or ["In Focus"]
        self.state_reads = 0
        self.loaded = loaded
        self.props = {
            "Number of Averages": "1",
            "GainMultiplier": "10",
            "Number of Skips": "5",
            "LED Intensity": "50",
            "Objective NA": "1.49",
            "Sum": "77",
            "Lock Offset": "474",
            "Dither Error": "-2",
        }
        self.writes: list[tuple] = []
        self.rng = random.Random(0)

    def getProperty(self, label: str, prop: str) -> str:
        if prop == "CRISP State":
            s = self.states[min(self.state_reads, len(self.states) - 1)]
            self.state_reads += 1
            return s
        return self.props[prop]

    def setProperty(self, *args: object) -> None:
        self.writes.append(args)

    def getPosition(self, label: str) -> float:
        return 25.0 + self.rng.gauss(0, 0.01)

    def getLoadedDevices(self) -> list[str]:
        return [LABEL, ACT] if self.loaded else []


def _run(core: FakeCore, **kw: object) -> HoldResult:
    done = threading.Event()
    box: dict[str, HoldResult] = {}

    def on_done(r: HoldResult) -> None:
        box["r"] = r
        done.set()

    params: dict = {"duration_s": 0.5, "rate_hz": 50.0}
    params.update(kw)
    test = HoldTest(core, LABEL, ACT, on_done=on_done, **params)  # type: ignore[arg-type]
    test.start()
    assert done.wait(5.0)
    return box["r"]


def test_worker_records_position_error_settings_and_never_writes() -> None:
    core = FakeCore()
    seen: list[HoldSample] = []
    r = _run(core, on_sample=seen.append)
    assert r.completed
    assert r.note == ""
    assert len(r.samples) == len(seen) > 10
    assert r.settings["Averages"] == "1"
    assert r.settings["Loop Gain"] == "10"
    assert r.settings["Update rate (ms)"] == "5"
    assert r.lock_offset_start == r.lock_offset_end == 474.0
    assert r.stats.rms_nm is not None
    assert all(s.error == -2.0 for s in r.samples)
    assert core.writes == []


def test_worker_reads_the_state_about_once_a_second_not_every_sample() -> None:
    core = FakeCore()
    r = _run(core, duration_s=1.2)
    with_state = [s for s in r.samples if s.state is not None]
    assert 1 <= len(with_state) <= 3
    assert len(r.samples) > 20


def test_worker_stops_when_the_lock_is_lost() -> None:
    core = FakeCore(states=["In Focus", "Ready", "Ready"])
    r = _run(core, duration_s=10.0)
    assert not r.completed
    assert r.lock_lost_at_s is not None
    assert "lock lost" in r.note


def test_one_unlocked_read_alone_does_not_stop_the_run() -> None:
    core = FakeCore(states=["In Focus", "Ready", "In Focus"])
    r = _run(core, duration_s=2.5)
    assert r.completed


def test_stop_delivers_a_partial_result() -> None:
    core = FakeCore()
    done = threading.Event()
    box: dict[str, HoldResult] = {}

    def on_done(r: HoldResult) -> None:
        box["r"] = r
        done.set()

    test = HoldTest(core, LABEL, ACT, duration_s=60, rate_hz=50, on_done=on_done)  # type: ignore[arg-type]
    test.start()
    threading.Event().wait(0.3)
    test.stop()
    assert done.wait(2.0)
    assert not test.running
    assert not box["r"].completed
    assert box["r"].note == "stopped early"
    assert box["r"].samples


def test_worker_ends_if_the_device_is_unloaded() -> None:
    r = _run(FakeCore(loaded=False), duration_s=5.0)
    assert not r.completed
    assert "unloaded" in r.note
    assert r.samples == []


def test_worker_ends_if_the_display_goes_away() -> None:
    def boom(_: HoldSample) -> None:
        raise RuntimeError("wrapped C/C++ object has been deleted")

    r = _run(FakeCore(), duration_s=5.0, on_sample=boom)
    assert not r.completed
    assert len(r.samples) == 1


# ------------------------------------------------------------------------------ csv
def test_write_hold_csv(tmp_path: Path) -> None:
    r = _run(FakeCore())
    out = tmp_path / "hold.csv"
    write_hold_csv(out, r)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert f"# crisp: {LABEL}" in lines
    assert "# setting Averages: 1" in lines
    header = lines.index("t_s,position_um,error_counts,state")
    assert len(lines) - header - 1 == len(r.samples)
