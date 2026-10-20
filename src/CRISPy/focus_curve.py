"""Focus curve: the CRISP error signal as a function of focus position.

From the ASI CRISP manual (Using Focus Curve Generation for Optimizing Adjustments): a
plot of the focus error response shows the usable lock range and the capture range.
Near the reflecting surface the curve has a strong slope that provides the feedback;
the useful lock range is the region with the correct slope, and a lock point close to a
peak of the curve "would be hard to hold". On a Tiger controller ASI's own plugin builds
the curve in software, and so does this module: with CRISP in the Ready state (unlocked,
LED on) it steps the paired focus actuator through a range around its current position,
reads the error at each step, and always returns the actuator to where it started. The
scan stays inside the actuator's travel limits when it reports them, and every point is
placed at the position the actuator reads back, so a stage stopped at its limit can't
pass for a flat stretch of the curve. It can also snap an image at each position
(:mod:`.imaging`); the image's sharpness then shows where the image is in focus, to
compare with where CRISP would lock (:func:`sharpest_position`).

The analysis reports, from measured data rather than the calibration:

* the zero crossing (where CRISP would lock with the current offset) and the **slope**
  there, in error counts per µm;
* the peaks either side and how far the lock point is from each;
* the capture range (where the error still has the sign that drives back to focus);
* the error noise: the scatter from one position to the next, and the spread of the
  reads at one position, with the focus noise it implies (noise / slope).

When the error scatters by more than the readout's rounding, the decisions (zero
crossings, which way the curve runs, peaks, capture range) are made on a running mean,
so a few counts of noise at fine steps can't flip a sign near the lock point. The slope
is always fitted to the raw points.

Nothing here imports Qt.
"""

from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import TYPE_CHECKING

from .imaging import focus_score
from .sampling import read_float, read_str

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    import numpy as np
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
# Point-to-point scatter (counts rms) above which decisions use a running mean of
# about this half-width (µm, at least one point each side). Whole-count rounding of a
# clean curve stays well under it.
_SMOOTH_ABOVE_COUNTS = 1.0
_SMOOTH_HALF_WIDTH_UM = 0.4
# ASI stages and piezos report their firmware travel limits (SL / SU) in mm.
LOWER_LIMIT_PROP = "LowerLim(mm)"
UPPER_LIMIT_PROP = "UpperLim(mm)"
# A position counts as reached when the actuator reads back within this distance of it
# (or half a step, if that is larger).
REACH_TOL_UM = 0.2


class FocusCurveError(RuntimeError):
    """The scan could not run, or could not return the actuator to its start."""


@dataclass(frozen=True)
class CurvePoint:
    """The error at one actuator position."""

    z_um: float  # relative to the starting position
    error: float  # mean of the reads
    error_std: float | None  # population std-dev of the reads (None for one read)
    n: int
    # Image sharpness at this position, per camera (when the scan snapped images).
    scores: Mapping[str, float] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class CurveScan:
    """The result of :func:`run_focus_curve`."""

    actuator: str
    z0_um: float  # absolute starting position; the actuator was returned here
    points: list[CurvePoint] = field(default_factory=list)
    completed: bool = True  # False if stopped early
    # The actuator's travel limits (absolute µm), when it reports usable ones.
    limits_um: tuple[float, float] | None = None
    # The actuator's travel cut the scan short on that side: positions past a limit
    # were left out, or the actuator didn't get to them.
    limited_low: bool = False
    limited_high: bool = False
    skipped: int = 0  # positions left out because the actuator didn't reach them


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
    noise_counts: float | None  # typical spread of the reads at one position
    noise_floor_nm: float | None  # noise_counts / |slope|
    # Every other zero crossing in the scan, as (z, slope there). Another reflecting
    # surface coming into focus shows up as one; with the opposite slope sign it is
    # also where the capture range ends on that side.
    other_crossings: tuple[tuple[float, float | None], ...] = ()
    # Scatter of the error from one position to the next near the lock point (robust
    # rms, counts), and the focus distance it amounts to at the slope (nm).
    scatter_counts: float | None = None
    scatter_nm: float | None = None

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


def travel_limits_um(
    mmcore: CMMCorePlus, actuator: str, z0_um: float
) -> tuple[float, float] | None:
    """The actuator's firmware travel limits in µm, if it reports usable ones.

    Reads the ASITiger ``LowerLim(mm)`` / ``UpperLim(mm)`` properties. A pair that
    doesn't bracket the current position *z0_um* (unset, or in another coordinate
    frame) is ignored: the scan then relies on reading the position back.

    Parameters
    ----------
    mmcore
        The shared core.
    actuator
        The stage device.
    z0_um
        Its current position, µm.
    """
    lo = read_float(mmcore, actuator, LOWER_LIMIT_PROP)
    hi = read_float(mmcore, actuator, UPPER_LIMIT_PROP)
    if lo is None or hi is None:
        return None
    lo, hi = lo * 1000.0, hi * 1000.0
    if not lo < hi or not lo <= z0_um <= hi:
        return None
    return lo, hi


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
    snap: Callable[[], Mapping[str, np.ndarray]] | None = None,
    on_images: Callable[[CurvePoint, Mapping[str, np.ndarray]], None] | None = None,
) -> CurveScan:
    """Step *actuator* through ±*half_range_um* around its position, reading the error.

    Blocks until done; run it on a worker thread. The actuator is always moved back to
    its starting position, including when the scan is stopped or fails.

    The scan leaves out positions past the actuator's travel limits
    (:func:`travel_limits_um`) and positions it doesn't reach (it reads back more than
    :data:`REACH_TOL_UM` or half a step away); a position it misses going up ends the
    scan. Each point is placed at the position read back, not the one commanded.

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
    snap
        If given, called at each position after the error is read; returns one image
        per camera (``{camera: image}``). Each image's :func:`.imaging.focus_score`
        goes into the point's ``scores``. A snap that raises ends the scan like a
        failed move.
    on_images
        Called with each point and its full frames (``{camera: image}``), when *snap*
        is given.

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

    limits = travel_limits_um(mmcore, actuator, z0)
    offsets = scan_offsets(half_range_um, step_um)
    limited_low = limited_high = False
    if limits is not None:
        lo, hi = limits[0] - z0 - 1e-6, limits[1] - z0 + 1e-6
        limited_low = offsets[0] < lo
        limited_high = offsets[-1] > hi
        offsets = [dz for dz in offsets if lo <= dz <= hi]
    tol = max(REACH_TOL_UM, step_um / 2)
    points: list[CurvePoint] = []
    failure: Exception | None = None
    completed = True
    skipped = 0
    try:
        for dz in offsets:
            if should_stop is not None and should_stop():
                completed = False
                break
            target = z0 + dz
            mmcore.setPosition(actuator, target)
            mmcore.waitForDevice(actuator)
            if settle_s > 0:
                time.sleep(settle_s)
            z = float(mmcore.getPosition(actuator))
            if abs(z - target) > tol:
                time.sleep(max(settle_s, 0.05))  # still moving, or stopped short?
                z = float(mmcore.getPosition(actuator))
            if abs(z - target) > tol:
                skipped += 1
                if dz > 0:
                    limited_high = True
                    break  # further up is only further past the limit
                limited_low = True
                continue
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
            scores: dict[str, float] = {}
            frames: Mapping[str, np.ndarray] = {}
            if snap is not None:
                frames = snap()
                scores = {camera: focus_score(img) for camera, img in frames.items()}
            point = CurvePoint(
                z - z0, statistics.fmean(values), std, len(values), scores
            )
            points.append(point)
            if on_images is not None and frames:
                on_images(point, frames)
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
    return CurveScan(
        actuator=actuator,
        z0_um=z0,
        points=points,
        completed=completed,
        limits_um=limits,
        limited_low=limited_low,
        limited_high=limited_high,
        skipped=skipped,
    )


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


def _d2_noise(errors: list[float]) -> float | None:
    """A rough, robust rms of the point-to-point noise, from 2nd differences.

    Second differences cancel a straight line and nearly cancel a smooth curve, so what
    is left is the noise: for independent noise of rms σ they have rms σ√6. The median
    ignores the few points where the curve bends sharply, but of whole counts it moves
    in steps: good enough to decide whether the curve is noisy, not to report.
    """
    d2 = [abs(a - 2 * b + c) for a, b, c in zip(errors, errors[1:], errors[2:])]
    if len(d2) < 3:
        return None
    return 1.4826 * statistics.median(d2) / math.sqrt(6)


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve the square system ``a x = b`` (Gaussian elimination); ``None`` if singular."""
    n = len(b)
    m = [[*row, rhs] for row, rhs in zip(a, b)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _cubic_scatter(pts: list[CurvePoint]) -> float | None:
    """Rms scatter of the error about a least-squares cubic in z (8 points or more).

    Near the lock point a focus curve is close to a cubic (an S), so what is left is the
    noise from one position to the next.
    """
    n = len(pts)
    if n < 8:
        return None
    mz = statistics.fmean(p.z_um for p in pts)
    half = max(abs(p.z_um - mz) for p in pts) or 1.0
    us = [(p.z_um - mz) / half for p in pts]
    ys = [p.error for p in pts]
    a = [[sum(u ** (r + c) for u in us) for c in range(4)] for r in range(4)]
    b = [sum(y * u**r for u, y in zip(us, ys)) for r in range(4)]
    coef = _solve(a, b)
    if coef is None:
        return None
    ssr = sum(
        (y - sum(c * u**j for j, c in enumerate(coef))) ** 2 for u, y in zip(us, ys)
    )
    return math.sqrt(ssr / (n - 4))


def _running_mean(pts: list[CurvePoint], k: int) -> list[CurvePoint]:
    """Each error replaced by the mean over up to *k* points each side.

    The window shrinks symmetrically at the ends, so a straight line is unchanged.
    """
    if k <= 0:
        return pts
    n = len(pts)
    out = []
    for i, p in enumerate(pts):
        j = min(k, i, n - 1 - i)
        window = pts[i - j : i + j + 1]
        out.append(replace(p, error=statistics.fmean(w.error for w in window)))
    return out


@dataclass
class _Crossing:
    """Zero crossings close together, seen as one: the z and index range they span."""

    z_first: float
    i_first: int  # index of the lower point of the first crossing
    z_last: float
    i_last: int

    @property
    def z(self) -> float:
        return (self.z_first + self.z_last) / 2


def analyze_focus_curve(points: Iterable[CurvePoint]) -> CurveAnalysis:
    """Find the lock point, slope, peaks, capture range and noise floor of a curve."""
    pts = sorted(points, key=lambda p: p.z_um)
    empty = CurveAnalysis(None, None, None, None, None, None, False, False, None, None)
    if len(pts) < 2:
        return empty

    step = statistics.median(b.z_um - a.z_um for a, b in pairwise(pts))

    # Decide on a running mean when the error scatters by more than rounding and by
    # about as much as the curve changes from one point to the next. (A clean curve
    # sampled coarsely bends a lot per step, but changes by more.)
    errs = [p.error for p in pts]
    d1 = [abs(b - a) for a, b in pairwise(errs)]
    d2 = [abs(a - 2 * b + c) for a, b, c in zip(errs, errs[1:], errs[2:])]
    noisy = (
        (_d2_noise(errs) or 0.0) >= _SMOOTH_ABOVE_COUNTS
        and bool(d2)
        and statistics.median(d2) >= statistics.median(d1)
    )
    k = max(1, round(_SMOOTH_HALF_WIDTH_UM / step)) if noisy and step > 0 else 0
    sm = _running_mean(pts, k)
    merge = max(2 * step, k * step)

    # Every zero crossing. Crossings within `merge` of the previous one are one
    # crossing seen through noise.
    crossings: list[_Crossing] = []
    for i in range(len(sm) - 1):
        a, b = sm[i], sm[i + 1]
        if a.error == 0:
            z = a.z_um
        elif _sign(a.error) * _sign(b.error) < 0:
            z = a.z_um + (b.z_um - a.z_um) * (-a.error) / (b.error - a.error)
        else:
            continue
        if crossings and z - crossings[-1].z_last <= merge:
            crossings[-1].z_last, crossings[-1].i_last = z, i
            continue
        crossings.append(_Crossing(z, i, z, i))
    if not crossings:
        return empty
    # The lock point is the crossing nearest the starting position (z = 0).
    lock = min(crossings, key=lambda c: abs(c.z))
    z_c, i = lock.z, lock.i_first

    # Which way the curve runs through the lock point: a fit over a few points, not
    # one pair, which noise can turn around.
    around = [p for p in pts if abs(p.z_um - z_c) <= max(2 * step, k * step)]
    local = _fit_slope([p.z_um for p in around], [p.error for p in around])
    rising = _sign(local) if local else (_sign(sm[i + 1].error - sm[i].error) or 1)

    # Below the crossing the error has the opposite sign to above it.
    lo_start = i if sm[i].error != 0 else i - 1
    peak_low, cap_low, low_open = _walk_side(sm, lo_start, -1, -rising)
    peak_high, cap_high, high_open = _walk_side(sm, lock.i_last + 1, +1, rising)

    # Fit the slope over the central third of the region between the peaks (at least
    # the two points that bracket the crossing, and the running mean's width). With no
    # peak inside the scan the curve is broad, so fit over a quarter of the way to the
    # nearer scan end.
    margins = [abs(z_c - pk[0]) for pk in (peak_low, peak_high) if pk is not None]
    if margins:
        half_width = max(1.5 * step, k * step, min(margins) / 3)
    else:
        to_end = min(z_c - pts[0].z_um, pts[-1].z_um - z_c)
        half_width = max(1.5 * step, k * step, to_end / 4)
    near = [p for p in pts if abs(p.z_um - z_c) <= half_width]
    if len(near) < 2:
        near = [pts[i], pts[i + 1]]
    slope = _fit_slope([p.z_um for p in near], [p.error for p in near])

    stds = [p.error_std for p in near if p.error_std is not None]
    noise = statistics.median(stds) if stds else None
    floor_nm = None
    if noise is not None and slope:
        floor_nm = noise / abs(slope) * 1000.0
    scatter = _cubic_scatter(near)
    scatter_nm = None
    if scatter is not None and slope:
        scatter_nm = scatter / abs(slope) * 1000.0

    # Other crossings, if they are at least a quarter as steep as the lock point: a
    # shallower one is noise or tails wandering across zero, not another surface.
    others: list[tuple[float, float | None]] = []
    for c in crossings:
        if c is lock:
            continue
        z = c.z
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
        scatter_counts=scatter,
        scatter_nm=scatter_nm,
    )


def sharpest_position(
    points: Iterable[CurvePoint], camera: str
) -> tuple[float, bool] | None:
    """Where *camera*'s image was sharpest, as ``(z, inside)``.

    The position is refined with a parabola through the sharpest point and its two
    neighbours. *inside* is ``False`` when the sharpest image is at an end of the scan:
    it may get sharper beyond it. ``None`` if no point has a score for *camera*.

    Parameters
    ----------
    points
        The scan's points.
    camera
        The camera label, as in ``CurvePoint.scores``.
    """
    pts = sorted((p for p in points if camera in p.scores), key=lambda p: p.z_um)
    if not pts:
        return None
    k = max(range(len(pts)), key=lambda i: pts[i].scores[camera])
    if k == 0 or k == len(pts) - 1:
        return pts[k].z_um, False
    (x0, y0), (x1, y1), (x2, y2) = (
        (pts[i].z_um, pts[i].scores[camera]) for i in (k - 1, k, k + 1)
    )
    den = (x1 - x0) * (y1 - y2) - (x1 - x2) * (y1 - y0)
    if den == 0:
        return x1, True
    num = (x1 - x0) ** 2 * (y1 - y2) - (x1 - x2) ** 2 * (y1 - y0)
    return min(max(x1 - 0.5 * num / den, x0), x2), True


def write_curve_csv(path: Path | str, scan: CurveScan, meta: dict[str, str]) -> None:
    """Save a scan as CSV: ``#``-prefixed metadata lines, then one row per point."""
    header = {"actuator": scan.actuator, "z0_um": f"{scan.z0_um:.4f}"}
    if scan.limits_um is not None:
        lo, hi = scan.limits_um
        header["travel_limits_um"] = f"{lo:.3f} .. {hi:.3f}"
    cut = [s for s, c in (("low", scan.limited_low), ("high", scan.limited_high)) if c]
    if cut:
        header["limited_by_travel"] = " and ".join(cut)
    if scan.skipped:
        header["positions_not_reached"] = str(scan.skipped)
    header.update(meta)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.writelines(f"# {key}: {value}\n" for key, value in header.items())
        writer = csv.writer(fh)
        cameras = sorted({c for p in scan.points for c in p.scores})
        writer.writerow(
            ["z_rel_um", "z_abs_um", "error_counts", "error_std", "n"]
            + [f"sharpness {c}" for c in cameras]
        )
        for p in scan.points:
            writer.writerow(
                [
                    f"{p.z_um:.4f}",
                    f"{scan.z0_um + p.z_um:.4f}",
                    f"{p.error:.3f}",
                    "" if p.error_std is None else f"{p.error_std:.3f}",
                    p.n,
                ]
                + [f"{p.scores[c]:.6g}" if c in p.scores else "" for c in cameras]
            )
