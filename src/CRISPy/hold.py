"""Hold test: how still does the locked focus actually stay?

The CRISP error signal is measured *inside* the feedback loop, so it can look quiet
while the actuator moves: on this rig's 2026-08-14 log the locked piezo's error stayed
within ±9 counts while the piezo itself moved ~100 nm between samples. The actuator's
own position readback is what the servo does to the focus, so this test records it
(plus the error and the state) at a steady rate while the axis sits locked.

It can also run **unlocked** (Ready or Idle), with CRISP not driving the actuator. The
position then shows the actuator's own noise (the piezo and its drive), and in Ready the
error shows the sensor's noise. Comparing an unlocked and a locked run separates what
the actuator does by itself from what the lock adds.

Either way the record is split into:

* **drift**: the straight-line trend, in nm/min. Part of it is CRISP doing its job,
  following thermal drift of the sample; a drift together with a changing lock offset
  means the lock reference itself moved (e.g. the focus wheel);
* **wander**: after removing the drift, the spread of the 1 s (``bin_s``) averages,
  i.e. motion over seconds and longer;
* **jitter**: the spread within each 1 s, i.e. faster motion (a slow ramp also adds a
  little: its rise over 1 s divided by √12).

``jitter² + wander²`` is the detrended mean-square, so the two add up to the total.

The error gets the same straight-line fit: its **trend** (counts/min) and its rms about
the trend. In an unlocked run the actuator stands still, so an error trend is the optics
or the sensor drifting; divided by the focus-curve slope it is the drift a lock would
follow. (Without the detrending, a ramp of R counts over the run alone gives R/√12 rms.)
It also records the lock settings and the lock offset at the start and end, so runs can
be compared one change at a time. It never writes a setting.

Nothing here imports Qt.
"""

from __future__ import annotations

import csv
import math
import statistics
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .sampling import device_present, read_float, read_str

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from pymmcore_plus import CMMCorePlus

ERROR_PROP = "Dither Error"
STATE_PROP = "CRISP State"
LOCK_OFFSET_PROP = "Lock Offset"
LOCKED_STATES = frozenset({"Lock", "In Focus"})
# States in which CRISP drives the actuator; an unlocked run ends if one appears.
DRIVEN_STATES = LOCKED_STATES | {
    "Dither",
    "Calibrating",
    "loG_cal",
    "gain_Cal",
    "Curve",
}

# Lock settings recorded with each run: (property, short name).
SETTINGS_PROPS: list[tuple[str, str]] = [
    ("Number of Averages", "Averages"),
    ("GainMultiplier", "Loop Gain"),
    ("Number of Skips", "Update rate (ms)"),
    ("LED Intensity", "LED (%)"),
    ("Objective NA", "Objective NA"),
    ("Sum", "Sum"),
]

DEFAULT_DURATION_S = 60.0
DEFAULT_RATE_HZ = 10.0
DEFAULT_BIN_S = 1.0
# Read the state about this often (not every sample) to see a lost lock.
_STATE_EVERY_S = 1.0
# Stop after this many consecutive state reads in the wrong mode (a single odd read
# is tolerated).
_WRONG_STATE_READS_TO_STOP = 2


@dataclass(frozen=True)
class HoldSample:
    """One reading. ``None`` marks a value not read this time or unreadable."""

    t: float  # seconds since the test started
    position_um: float | None
    error: float | None
    state: str | None = None


@dataclass(frozen=True)
class HoldStats:
    """Summary of a run's position and error. ``None`` when there's too little data."""

    duration_s: float
    n: int
    rate_hz: float | None
    drift_nm_per_min: float | None
    rms_nm: float | None  # detrended
    jitter_nm: float | None  # detrended, faster than bin_s
    wander_nm: float | None  # detrended, bin_s and slower
    pkpk_nm: float | None  # detrended
    error_mean: float | None
    error_rms: float | None  # about the error's straight-line trend
    error_drift_per_min: float | None = None  # the trend, counts/min


@dataclass
class HoldResult:
    """Everything about one run."""

    label: str
    actuator: str
    started: datetime
    settings: dict[str, str]
    lock_offset_start: float | None
    lock_offset_end: float | None
    stats: HoldStats
    samples: list[HoldSample] = field(default_factory=list)
    completed: bool = True  # False if stopped, or the state left the run's mode
    interrupted_at_s: float | None = None  # when the state left the run's mode
    note: str = ""
    locked: bool = True  # a locked run (CRISP driving) or an unlocked one

    @property
    def lock_offset_change(self) -> float | None:
        if self.lock_offset_start is None or self.lock_offset_end is None:
            return None
        return self.lock_offset_end - self.lock_offset_start


def _linear_fit(ts: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares ``(intercept, slope)``; slope 0 if all times are equal."""
    mt, my = statistics.fmean(ts), statistics.fmean(ys)
    stt = sum((t - mt) ** 2 for t in ts)
    if stt == 0:
        return my, 0.0
    slope = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / stt
    return my - slope * mt, slope


def analyze_hold(
    samples: Iterable[HoldSample], *, bin_s: float = DEFAULT_BIN_S
) -> HoldStats:
    """Split the position record into drift, wander and jitter (see the module doc)."""
    samples = list(samples)
    duration = samples[-1].t - samples[0].t if len(samples) > 1 else 0.0
    rate = (len(samples) - 1) / duration if duration > 0 else None

    errs = [(s.t, s.error) for s in samples if s.error is not None]
    err_mean = statistics.fmean(e for _, e in errs) if errs else None
    err_rms = err_drift = None
    if len(errs) > 1:
        ea, eb = _linear_fit([t for t, _ in errs], [e for _, e in errs])
        err_rms = math.sqrt(statistics.fmean((e - (ea + eb * t)) ** 2 for t, e in errs))
        err_drift = eb * 60.0

    pos = [(s.t, s.position_um) for s in samples if s.position_um is not None]
    drift = rms = jitter = wander = pkpk = None
    if len(pos) >= 3:
        ts = [t for t, _ in pos]
        zs = [z for _, z in pos]
        a, b = _linear_fit(ts, zs)
        resid = [(z - (a + b * t)) * 1000.0 for t, z in zip(ts, zs)]  # nm
        drift = b * 1000.0 * 60.0
        rms = math.sqrt(statistics.fmean(r * r for r in resid))
        pkpk = max(resid) - min(resid)
        # Group into bins of bin_s; within-bin spread is jitter, between-bin spread
        # (sample-weighted, so the two add in quadrature) is wander.
        t0 = ts[0]
        bins: dict[int, list[float]] = {}
        for t, r in zip(ts, resid):
            bins.setdefault(int((t - t0) // bin_s), []).append(r)
        overall = statistics.fmean(resid)
        within = between = 0.0
        for vals in bins.values():
            m = statistics.fmean(vals)
            within += sum((v - m) ** 2 for v in vals)
            between += len(vals) * (m - overall) ** 2
        jitter = math.sqrt(within / len(resid))
        wander = math.sqrt(between / len(resid))

    return HoldStats(
        duration_s=duration,
        n=len(samples),
        rate_hz=rate,
        drift_nm_per_min=drift,
        rms_nm=rms,
        jitter_nm=jitter,
        wander_nm=wander,
        pkpk_nm=pkpk,
        error_mean=err_mean,
        error_rms=err_rms,
        error_drift_per_min=err_drift,
    )


class HoldTest:
    """Run a hold test on a worker thread.

    Parameters
    ----------
    mmcore
        The shared core.
    label
        The CRISP autofocus device.
    actuator
        The stage device CRISP drives; its position is recorded.
    duration_s
        How long to record.
    rate_hz
        Target sample rate. Each sample is two reads (position, error).
    locked
        ``True`` (default) for a run with the lock engaged; it ends if the lock is
        lost. ``False`` for a run with CRISP *not* driving the actuator (Ready or
        Idle): the position then shows the actuator's own noise and the error the
        sensor's noise. It ends if the state moves into Lock, Dither or calibration.
    on_sample
        Called on the worker thread with each sample.
    on_done
        Called on the worker thread with the :class:`HoldResult` when the run ends,
        however it ends.
    """

    def __init__(
        self,
        mmcore: CMMCorePlus,
        label: str,
        actuator: str,
        *,
        duration_s: float = DEFAULT_DURATION_S,
        rate_hz: float = DEFAULT_RATE_HZ,
        locked: bool = True,
        on_sample: Callable[[HoldSample], None] | None = None,
        on_done: Callable[[HoldResult], None] | None = None,
    ) -> None:
        self._mmcore = mmcore
        self._label = label
        self._actuator = actuator
        self._locked = locked
        self._duration_s = duration_s
        self._interval = 1.0 / rate_hz
        self._on_sample = on_sample
        self._on_done = on_done
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def locked(self) -> bool:
        """Whether this is a locked run (CRISP driving) or an unlocked one."""
        return self._locked

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"CRISPy-hold-{self._label}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop early; the partial result is still delivered to *on_done*."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _state_ok(self, state: str) -> bool:
        """Whether *state* matches the run's mode (locked, or not driven)."""
        if self._locked:
            return state in LOCKED_STATES
        return state not in DRIVEN_STATES

    def _read_position(self) -> float | None:
        with suppress(Exception):
            return float(self._mmcore.getPosition(self._actuator))
        return None

    def _run(self) -> None:
        core, label = self._mmcore, self._label
        started = datetime.now().astimezone()
        settings = {
            name: value
            for prop, name in SETTINGS_PROPS
            if (value := read_str(core, label, prop)) is not None
        }
        offset_start = read_float(core, label, LOCK_OFFSET_PROP)

        samples: list[HoldSample] = []
        interrupted_at: float | None = None
        wrong_reads = 0
        t0 = time.monotonic()
        next_state = t0
        note = ""
        while not self._stop.is_set():
            now = time.monotonic()
            if now - t0 >= self._duration_s:
                break
            if not device_present(core, label):
                note = "the device was unloaded"
                break
            state = None
            if now >= next_state:
                next_state = now + _STATE_EVERY_S
                state = read_str(core, label, STATE_PROP)
            sample = HoldSample(
                t=time.monotonic() - t0,
                position_um=self._read_position(),
                error=read_float(core, label, ERROR_PROP),
                state=state,
            )
            samples.append(sample)
            if self._on_sample is not None:
                try:
                    self._on_sample(sample)
                except Exception:  # noqa: BLE001 - a dead consumer ends the run
                    note = "stopped: display closed"
                    break
            if state is not None:
                wrong_reads = 0 if self._state_ok(state) else wrong_reads + 1
                if wrong_reads >= _WRONG_STATE_READS_TO_STOP:
                    interrupted_at = sample.t
                    note = (
                        f"lock lost ({state})"
                        if self._locked
                        else f"CRISP started driving the actuator ({state})"
                    )
                    break
            wait = self._interval - (time.monotonic() - now)
            if wait > 0:
                self._stop.wait(wait)

        if self._stop.is_set() and not note:
            note = "stopped early"
        stopped_early = bool(note)  # stopped, lock lost, device gone or display closed
        result = HoldResult(
            label=label,
            actuator=self._actuator,
            started=started,
            settings=settings,
            lock_offset_start=offset_start,
            lock_offset_end=read_float(core, label, LOCK_OFFSET_PROP),
            stats=analyze_hold(samples),
            samples=samples,
            completed=not stopped_early,
            interrupted_at_s=interrupted_at,
            note=note,
            locked=self._locked,
        )
        if self._on_done is not None:
            self._on_done(result)


def write_hold_csv(path: Path | str, result: HoldResult) -> None:
    """Save a run as CSV: ``#``-prefixed metadata lines, then one row per sample."""
    s = result.stats
    meta = {
        "crisp": result.label,
        "actuator": result.actuator,
        "mode": "locked" if result.locked else "unlocked",
        "started": result.started.isoformat(timespec="seconds"),
        **{f"setting {k}": v for k, v in result.settings.items()},
        "lock_offset_start": result.lock_offset_start,
        "lock_offset_end": result.lock_offset_end,
        "note": result.note,
        "drift_nm_per_min": s.drift_nm_per_min,
        "jitter_nm": s.jitter_nm,
        "wander_nm": s.wander_nm,
        "pkpk_nm": s.pkpk_nm,
        "error_rms": s.error_rms,
        "error_drift_per_min": s.error_drift_per_min,
    }
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.writelines(
            f"# {key}: {'' if value is None else value}\n"
            for key, value in meta.items()
        )
        writer = csv.writer(fh)
        writer.writerow(["t_s", "position_um", "error_counts", "state"])
        for smp in result.samples:
            writer.writerow(
                [
                    f"{smp.t:.4f}",
                    "" if smp.position_um is None else f"{smp.position_um:.4f}",
                    "" if smp.error is None else f"{smp.error:g}",
                    smp.state or "",
                ]
            )
