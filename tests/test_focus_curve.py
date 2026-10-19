"""Tests for the focus-curve scan (with a fake core) and its analysis."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

import pytest

from CRISPy.focus_curve import (
    CurvePoint,
    CurveScan,
    FocusCurveError,
    analyze_focus_curve,
    run_focus_curve,
    scan_offsets,
    write_curve_csv,
)

if TYPE_CHECKING:
    from pathlib import Path

LABEL = "CRISPAFocus:P:34"
ACT = "PiezoStage:P:34"


def s_curve(u: float, slope: float = 400.0, width: float = 2.0) -> float:
    """Antisymmetric focus curve with the given slope at u=0 and peaks at ±width."""
    return slope * u * math.exp(-(u * u) / (2 * width * width))


class FakeCore:
    """Just enough of CMMCorePlus: a stage position and an error that follows it."""

    def __init__(
        self,
        *,
        z0: float = 25.0,
        focus: float = 25.0,
        state: str = "Ready",
        noise: float = 0.0,
        fail_at: float | None = None,
        fail_restore: bool = False,
        slope: float = 400.0,
    ) -> None:
        self.z = z0
        self.focus = focus
        self.state = state
        self.noise = noise
        self.fail_at = fail_at
        self.fail_restore = fail_restore
        self.slope = slope
        self.moves: list[float] = []
        self.rng = random.Random(1)

    def getProperty(self, label: str, prop: str) -> str:
        if prop == "CRISP State":
            return self.state
        if prop == "Dither Error":
            e = s_curve(self.z - self.focus, self.slope) + self.rng.gauss(0, self.noise)
            return f"{e:.3f}"
        raise KeyError(prop)

    def getPosition(self, label: str) -> float:
        assert label == ACT
        return self.z

    def setPosition(self, label: str, z: float) -> None:
        assert label == ACT
        if self.fail_at is not None and abs(z - self.fail_at) < 1e-6:
            raise RuntimeError("COD 47")
        if self.fail_restore and self.moves and abs(z - 25.0) < 1e-9:
            raise RuntimeError("serial timeout")
        self.moves.append(z)
        self.z = z

    def waitForDevice(self, label: str) -> None:
        pass


def _run(core: FakeCore, **kw: object) -> CurveScan:
    params: dict = {"half_range_um": 6.0, "step_um": 0.25, "settle_s": 0.0}
    params.update(kw)
    return run_focus_curve(core, LABEL, ACT, **params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- scan_offsets
def test_scan_offsets_are_ascending_symmetric_and_include_zero() -> None:
    offs = scan_offsets(1.0, 0.25)
    assert offs == [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]


def test_scan_offsets_reject_nonpositive_inputs() -> None:
    with pytest.raises(ValueError):
        scan_offsets(0, 0.1)
    with pytest.raises(ValueError):
        scan_offsets(1, 0)


# ------------------------------------------------------------------------------ scan
def test_scan_moves_through_the_range_and_returns_to_start() -> None:
    core = FakeCore()
    scan = _run(core)
    assert scan.completed
    assert scan.z0_um == 25.0
    assert core.z == 25.0  # back where it started
    assert core.moves[0] == pytest.approx(19.0)
    assert core.moves[-2] == pytest.approx(31.0)
    assert core.moves[-1] == 25.0
    assert len(scan.points) == len(scan_offsets(6.0, 0.25))
    assert all(p.n == 3 for p in scan.points)


def test_scan_refuses_while_locked_and_does_not_move() -> None:
    core = FakeCore(state="In Focus")
    with pytest.raises(FocusCurveError, match="locked"):
        _run(core)
    assert core.moves == []


def test_scan_stopped_early_still_returns_to_start() -> None:
    core = FakeCore()
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    scan = _run(core, should_stop=stop)
    assert not scan.completed
    assert len(scan.points) == 5
    assert core.z == 25.0


def test_a_failed_move_is_reported_and_the_actuator_is_returned() -> None:
    core = FakeCore(fail_at=25.0 + 1.0)
    with pytest.raises(FocusCurveError, match="returned to 25.000"):
        _run(core)
    assert core.z == 25.0


def test_a_failed_return_is_reported_loudly() -> None:
    core = FakeCore(fail_restore=True)
    with pytest.raises(FocusCurveError, match="Could not return"):
        _run(core)


def test_on_point_sees_every_point() -> None:
    seen: list[CurvePoint] = []
    scan = _run(FakeCore(), on_point=seen.append)
    assert seen == scan.points


# -------------------------------------------------------------------------- analysis
def test_analysis_finds_the_lock_point_slope_and_peaks() -> None:
    scan = _run(FakeCore(focus=25.3), half_range_um=8.0)
    a = analyze_focus_curve(scan.points)
    assert a.crossing_um == pytest.approx(0.3, abs=0.02)
    assert a.slope == pytest.approx(400.0, rel=0.1)
    assert a.peak_low is not None and a.peak_high is not None
    assert a.peak_low[0] == pytest.approx(0.3 - 2.0, abs=0.26)
    assert a.peak_high[0] == pytest.approx(0.3 + 2.0, abs=0.26)
    assert a.margin_low_um == pytest.approx(2.0, abs=0.26)
    assert a.margin_high_um == pytest.approx(2.0, abs=0.26)
    # This model never changes sign again, so capture runs to both ends of the scan.
    assert a.capture_low_open and a.capture_high_open


def test_a_negative_slope_is_handled_the_same_way() -> None:
    scan = _run(FakeCore(slope=-300.0))
    a = analyze_focus_curve(scan.points)
    assert a.slope == pytest.approx(-300.0, rel=0.1)
    assert a.peak_low is not None and a.peak_low[1] > 0
    assert a.peak_high is not None and a.peak_high[1] < 0


def test_noise_floor_is_noise_over_slope() -> None:
    scan = _run(FakeCore(noise=4.0), reads_per_point=12)
    a = analyze_focus_curve(scan.points)
    assert a.noise_counts == pytest.approx(4.0, rel=0.35)
    assert a.noise_floor_nm == pytest.approx(a.noise_counts / abs(a.slope) * 1000)


def test_one_read_per_point_gives_no_noise_estimate() -> None:
    a = analyze_focus_curve(_run(FakeCore(noise=4.0), reads_per_point=1).points)
    assert a.noise_counts is None
    assert a.noise_floor_nm is None


def test_capture_range_ends_where_the_sign_turns_wrong() -> None:
    # A curve that turns over (like a second reflecting surface) at z = +3.
    pts = [
        CurvePoint(z, s_curve(z) if z < 3.0 else -50.0, None, 1)
        for z in [k * 0.5 for k in range(-8, 9)]
    ]
    a = analyze_focus_curve(pts)
    assert a.capture_high_um == pytest.approx(2.5)
    assert not a.capture_high_open
    assert a.capture_low_open


def test_the_crossing_nearest_the_start_is_used() -> None:
    # Two crossings: one at -3 and one at +0.5; the lock point is the one nearest 0.
    pts = [
        CurvePoint(z, (z + 3) * (z - 0.5), None, 1)
        for z in [k * 0.25 for k in range(-20, 21)]
    ]
    a = analyze_focus_curve(pts)
    assert a.crossing_um == pytest.approx(0.5, abs=0.01)


def test_no_crossing_means_nothing_to_report() -> None:
    pts = [CurvePoint(z, 50.0 + z, None, 1) for z in (-1.0, 0.0, 1.0)]
    a = analyze_focus_curve(pts)
    assert a.crossing_um is None
    assert a.slope is None


def test_too_few_points() -> None:
    assert analyze_focus_curve([]).crossing_um is None
    assert analyze_focus_curve([CurvePoint(0.0, 1.0, None, 1)]).crossing_um is None


# ------------------------------------------------------------------------------- csv
def test_write_curve_csv(tmp_path: Path) -> None:
    scan = _run(FakeCore(), half_range_um=0.5)
    out = tmp_path / "curve.csv"
    write_curve_csv(out, scan, {"crisp": LABEL})
    text = out.read_text(encoding="utf-8").splitlines()
    assert text[0] == f"# actuator: {ACT}"
    assert "# crisp: CRISPAFocus:P:34" in text
    header = text.index("z_rel_um,z_abs_um,error_counts,error_std,n")
    assert len(text) - header - 1 == len(scan.points)
