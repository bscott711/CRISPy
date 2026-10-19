"""Focus curve: the CRISP error signal as a function of focus position.

From the ASI CRISP manual (Using Focus Curve Generation for Optimizing Adjustments): a
plot of the focus error response shows the usable lock range and the capture range.
Near the reflecting surface the curve has a strong slope that provides the feedback;
the useful lock range is the region with the correct slope, and a lock point close to a
peak of the curve "would be hard to hold". On a Tiger controller ASI's own plugin builds
the curve in software, and so does this module: with CRISP in the Ready state (unlocked,
LED on) it steps the paired focus actuator through a range around its current position,
reads the error at each step, and always returns the actuator to where it started.

The analysis reports, from measured data rather than the calibration:

* the zero crossing (where CRISP would lock with the current offset) and the **slope**
  there, in error counts per µm;
* the peaks either side and how far the lock point is from each;
* the capture range (where the error still has the sign that drives back to focus);
* the error noise at each step, and the focus noise it implies (noise / slope), which is
  the floor the lock can't do better than without more averaging or more signal.

Nothing here imports Qt.
"""

from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

from .sampling import read_float, read_str

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from pymmcore_plus import CMMCorePlus

ERROR_PROP = "Dither Error"
STATE_PROP = "CRISP State"
LOCKED_STATES = frozenset({"Lock", "In Focus"})

DEFAULT_HALF_RANGE_UM = 10.0
DEFAULT_STEP_UM = 0.25
DEFAULT_SETTLE_S = 0.05
DEFAULT_READS = 3
# A peak must turn over: beyond it (before the scan ends) the error has to fall by at
# least this fraction. A curve that levels off or keeps growing has no peak in the scan.
PEAK_DROP = 0.15
# Distance over which the slope at another zero crossing is fitted, µm (each side).
_OTHER_SLOPE_HALF_WIDTH_UM = 1.0
# Other crossings are reported only if at least this steep relative to the lock point.
_OTHER_MIN_STEEPNESS = 0.25
# A lock point whose two sides restore this many times differently sits at a knee.
LOPSIDED_RATIO = 4.0


class FocusCurveError(RuntimeError):
    """The scan could not run, or could not return the actuator to its start."""


@dataclass(frozen=True)
class CurvePoint:
    """The error at one actuator position."""

    z_um: float  # relative to the starting position
    error: float  # mean of the reads
    error_std: float | None  # population std-dev of the reads (None for one read)
    n: int


@dataclass(frozen=True)
class CurveScan:
    """The result of :func:`run_focus_curve`."""

    actuator: str
    z0_um: float  # absolute starting position; the actuator was returned here
    points: list[CurvePoint] = field(default_factory=list)
    completed: bool = True  # False if stopped early


@dataclass(frozen=True)
class CurveAnalysis:
    """What the curve says about the lock. Fields are ``None`` when not determinable."""

    crossing_um: float | None
    slope: float | None  # error counts per µm at the crossing (signed)
    peak_low: tuple[float, float] | None  # (z, error) of the peak below the crossing
    peak_high: tuple[float, float] | None  # (z, error) of the peak above it
    capture_low_um: float | None  # lowest z that still has the correct sign
    capture_high_um: float | None
    capture_low_open: bool  # the correct sign continued to the end of the scan
    capture_high_open: bool
    noise_counts: float | None  # typical per-step error std-dev near the crossing
    noise_floor_nm: float | None  # noise_counts / |slope|
    # Every other zero crossing in the scan, as (z, slope there). Another reflecting
    # surface coming into focus shows up as one; with the opposite slope sign it is
    # also where the capture range ends on that side.
    other_crossings: tuple[tuple[float, float | None], ...] = ()
    # The largest error with the right (restoring) sign on each side of the lock
    # point, within the capture range. Very unequal sides mean the lock point sits at
    # a knee of the curve rather than in the middle of an S.
    restoring_low: float | None = None
    restoring_high: float | None = None

    @property
    def lopsided(self) -> bool:
        """One side restores ``LOPSIDED_RATIO``× more strongly than the other."""
        lo, hi = self.restoring_low, self.restoring_high
        if not lo or not hi:
            return False
        return max(lo, hi) / min(lo, hi) >= LOPSIDED_RATIO

    @property
    def resolution_nm(self) -> float | None:
        """Focus distance per count of the error readout at the lock point, in nm.

        The ``Dither Error`` readout is a whole number (the LCD's Err), so this is
        the finest focus change it can show; noise near one count is mostly rounding.
        """
        if not self.slope:
            return None
        return 1000.0 / abs(self.slope)

    @property
    def margin_low_um(self) -> float | None:
        """Distance from the lock point down to the lower peak."""
        if self.crossing_um is None or self.peak_low is None:
            return None
        return self.crossing_um - self.peak_low[0]

    @property
    def margin_high_um(self) -> float | None:
        """Distance from the lock point up to the upper peak."""
        if self.crossing_um is None or self.peak_high is None:
            return None
        return self.peak_high[0] - self.crossing_um


def scan_offsets(half_range_um: float, step_um: float) -> list[float]:
    """Ascending offsets from ``-half_range_um`` to ``+half_range_um``, including 0."""
    if half_range_um <= 0 or step_um <= 0:
        raise ValueError("half_range_um and step_um must be positive")
    n = math.floor(half_range_um / step_um + 1e-9)
    return [round(k * step_um, 6) for k in range(-n, n + 1)]


def run_focus_curve(
    mmcore: CMMCorePlus,
    label: str,
    actuator: str,
    *,
    half_range_um: float = DEFAULT_HALF_RANGE_UM,
    step_um: float = DEFAULT_STEP_UM,
    settle_s: float = DEFAULT_SETTLE_S,
    reads_per_point: int = DEFAULT_READS,
    should_stop: Callable[[], bool] | None = None,
    on_point: Callable[[CurvePoint], None] | None = None,
) -> CurveScan:
    """Step *actuator* through ±*half_range_um* around its position, reading the error.

    Blocks until done; run it on a worker thread. The actuator is always moved back to
    its starting position, including when the scan is stopped or fails.

    Parameters
    ----------
    mmcore
        The shared core.
    label
        The CRISP autofocus device.
    actuator
        The stage device CRISP drives (e.g. ``PiezoStage:P:34``).
    half_range_um
        Scan from ``z0 - half_range_um`` to ``z0 + half_range_um``.
    step_um
        Distance between positions.
    settle_s
        Wait after each move (after the device reports it is done) before reading.
    reads_per_point
        Error reads averaged per position; their spread gives the noise estimate.
    should_stop
        Polled before each step; return ``True`` to stop early.
    on_point
        Called with each point as it is measured (on the calling thread).

    Raises
    ------
    FocusCurveError
        If CRISP is locked (moves of a locked axis fail with ASI error ``COD 47``), the
        start position can't be read, a move fails, or the actuator could not be
        returned to its start.
    """
    state = read_str(mmcore, label, STATE_PROP)
    if state in LOCKED_STATES:
        raise FocusCurveError(
            f"{label} is locked ({state}). Unlock to Ready first: a locked axis "
            "rejects moves."
        )
    try:
        z0 = float(mmcore.getPosition(actuator))
    except Exception as e:
        raise FocusCurveError(f"Could not read the position of {actuator}: {e}") from e

    offsets = scan_offsets(half_range_um, step_um)
    points: list[CurvePoint] = []
    failure: Exception | None = None
    completed = True
    try:
        for dz in offsets:
            if should_stop is not None and should_stop():
                completed = False
                break
            mmcore.setPosition(actuator, z0 + dz)
            mmcore.waitForDevice(actuator)
            if settle_s > 0:
                time.sleep(settle_s)
            values = [
                v
                for v in (
                    read_float(mmcore, label, ERROR_PROP)
                    for _ in range(max(1, reads_per_point))
                )
                if v is not None
            ]
            if not values:
                continue
            std = statistics.pstdev(values) if len(values) > 1 else None
            point = CurvePoint(dz, statistics.fmean(values), std, len(values))
            points.append(point)
            if on_point is not None:
                on_point(point)
    except Exception as e:  # noqa: BLE001 - re-raised below after the restore
        failure = e

    try:
        mmcore.setPosition(actuator, z0)
        mmcore.waitForDevice(actuator)
    except Exception as e:
        raise FocusCurveError(
            f"Could not return {actuator} to its start position {z0:.3f} µm: {e}"
        ) from e
    if failure is not None:
        raise FocusCurveError(
            f"Focus curve stopped: {failure} ({actuator} was returned to {z0:.3f} µm)"
        ) from failure
    return CurveScan(actuator=actuator, z0_um=z0, points=points, completed=completed)


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _fit_slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _walk_side(
    pts: list[CurvePoint], start: int, step: int, want_sign: int
) -> tuple[tuple[float, float] | None, float | None, bool]:
    """Walk outward from *start* while the error has *want_sign*.

    Returns the peak on that side, the last z with the correct sign, and whether the
    correct sign continued to the end of the scan. A peak must turn over: if the
    error doesn't fall by :data:`PEAK_DROP` beyond its largest value before the scan
    ends (it levels off or keeps growing), the peak is ``None``. If the sign changes
    first, the error has gone through zero, so the turnover is certain.
    """
    walked: list[CurvePoint] = []
    i = start
    open_end = True
    while 0 <= i < len(pts):
        p = pts[i]
        if _sign(p.error) != want_sign:
            open_end = False
            break
        walked.append(p)
        i += step
    if not walked:
        return None, None, open_end
    k = max(range(len(walked)), key=lambda j: (abs(walked[j].error), -j))
    peak: tuple[float, float] | None = (walked[k].z_um, walked[k].error)
    if open_end:
        beyond = [abs(p.error) for p in walked[k + 1 :]]
        if not beyond or min(beyond) > (1 - PEAK_DROP) * abs(walked[k].error):
            peak = None  # levels off or still growing at the end of the scan
    return peak, walked[-1].z_um, open_end


def _restoring(
    pts: list[CurvePoint], z_c: float, edge: float | None, *, below: bool
) -> float | None:
    """Largest |error| between the lock point and the capture edge on one side."""
    if edge is None:
        return None
    lo, hi = (edge, z_c) if below else (z_c, edge)
    side = [abs(p.error) for p in pts if lo <= p.z_um <= hi and p.z_um != z_c]
    return max(side) if side else None


def analyze_focus_curve(points: Iterable[CurvePoint]) -> CurveAnalysis:
    """Find the lock point, slope, peaks, capture range and noise floor of a curve."""
    pts = sorted(points, key=lambda p: p.z_um)
    empty = CurveAnalysis(None, None, None, None, None, None, False, False, None, None)
    if len(pts) < 2:
        return empty

    step = statistics.median(b.z_um - a.z_um for a, b in pairwise(pts))

    # Every zero crossing, as (z, index of the lower point). Crossings within two
    # steps of each other are one crossing seen through noise; keep the first.
    crossings: list[tuple[float, int]] = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a.error == 0:
            z = a.z_um
        elif _sign(a.error) * _sign(b.error) < 0:
            z = a.z_um + (b.z_um - a.z_um) * (-a.error) / (b.error - a.error)
        else:
            continue
        if crossings and z - crossings[-1][0] <= 2 * step:
            continue
        crossings.append((z, i))
    if not crossings:
        return empty
    # The lock point is the crossing nearest the starting position (z = 0).
    z_c, i = min(crossings, key=lambda c: abs(c[0]))
    rising = _sign(pts[i + 1].error - pts[i].error) or 1

    # Below the crossing the error has the opposite sign to above it.
    lo_start = i if pts[i].error != 0 else i - 1
    peak_low, cap_low, low_open = _walk_side(pts, lo_start, -1, -rising)
    peak_high, cap_high, high_open = _walk_side(pts, i + 1, +1, rising)

    # Fit the slope over the central third of the region between the peaks (at least
    # the two points that bracket the crossing). With no peak inside the scan the
    # curve is broad, so fit over a quarter of the way to the nearer scan end.
    margins = [abs(z_c - pk[0]) for pk in (peak_low, peak_high) if pk is not None]
    if margins:
        half_width = max(1.5 * step, min(margins) / 3)
    else:
        to_end = min(z_c - pts[0].z_um, pts[-1].z_um - z_c)
        half_width = max(1.5 * step, to_end / 4)
    near = [p for p in pts if abs(p.z_um - z_c) <= half_width]
    if len(near) < 2:
        near = [pts[i], pts[i + 1]]
    slope = _fit_slope([p.z_um for p in near], [p.error for p in near])

    stds = [p.error_std for p in near if p.error_std is not None]
    noise = statistics.median(stds) if stds else None
    floor_nm = None
    if noise is not None and slope:
        floor_nm = noise / abs(slope) * 1000.0

    # Other crossings, if they are at least a quarter as steep as the lock point: a
    # shallower one is noise or tails wandering across zero, not another surface.
    others: list[tuple[float, float | None]] = []
    for z, _ in crossings:
        if z == z_c:
            continue
        local = [p for p in pts if abs(p.z_um - z) <= _OTHER_SLOPE_HALF_WIDTH_UM]
        s = _fit_slope([p.z_um for p in local], [p.error for p in local])
        if s is not None and slope and abs(s) < _OTHER_MIN_STEEPNESS * abs(slope):
            continue
        others.append((z, s))

    return CurveAnalysis(
        crossing_um=z_c,
        slope=slope,
        peak_low=peak_low,
        peak_high=peak_high,
        capture_low_um=cap_low,
        capture_high_um=cap_high,
        capture_low_open=low_open,
        capture_high_open=high_open,
        noise_counts=noise,
        noise_floor_nm=floor_nm,
        other_crossings=tuple(others),
        restoring_low=_restoring(pts, z_c, cap_low, below=True),
        restoring_high=_restoring(pts, z_c, cap_high, below=False),
    )


def write_curve_csv(path: Path | str, scan: CurveScan, meta: dict[str, str]) -> None:
    """Save a scan as CSV: ``#``-prefixed metadata lines, then one row per point."""
    header = {"actuator": scan.actuator, "z0_um": f"{scan.z0_um:.4f}", **meta}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.writelines(f"# {key}: {value}\n" for key, value in header.items())
        writer = csv.writer(fh)
        writer.writerow(["z_rel_um", "z_abs_um", "error_counts", "error_std", "n"])
        for p in scan.points:
            writer.writerow(
                [
                    f"{p.z_um:.4f}",
                    f"{scan.z0_um + p.z_um:.4f}",
                    f"{p.error:.3f}",
                    "" if p.error_std is None else f"{p.error_std:.3f}",
                    p.n,
                ]
            )
