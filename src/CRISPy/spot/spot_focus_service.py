"""Main-process side of the spot focus lock (Qt-free, psygnal signals).

Owns the Tiger port: it starts the camera subprocess
(:mod:`~CRISPy.spot.spot_focus_worker`), and when the worker asks
for a move it calls ``mmc.setPosition(piezo_label, target)`` with **no
``waitForDevice`` and no readback**. It never polls the Tiger while the loop
runs, never queues moves (one in flight at most), and sends no further moves
once the worker is dead or the loop has faulted. See
``docs/spot_focus_lock.md``.

**Threading:** every signal is emitted from the service's reader thread, never
from the GUI thread; a Qt slot must hop threads itself (as for
:class:`~CRISPy.spot.panel.SpotFocusPanel`). A slot that raises is
logged and swallowed so it cannot stall the move path.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any

from psygnal import Signal

from .spot_focus import DEFAULT_DETECTOR, DEFAULT_SIGMA_PX, EDGE_DETECTORS
from .spot_focus_worker import launch_worker
from .spot_frame_shm import FrameBuffer, unlink_by_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..controller import CrispController
    from .spot_focus import EdgeCalibration, FocusCalibration, SpotLoopParams
    from .spot_frame_shm import LiveFrame

logger = logging.getLogger(__name__)

PIEZO_P_LABEL = "PiezoStage:P:34"
CRISP_LED_PROP = "LED Intensity"
DEFAULT_SOURCE_SPEC: dict[str, Any] = {"kind": "toupcam"}


def _edge_text(reason: str) -> str:
    """A plain message for a measurement that found no edge."""
    if "left the box" in reason:
        return (
            "edge left the box: widen the box along the edge normal or reduce the span"
        )
    return f"edge not measurable: {reason or 'no usable frames'}"


def _as_box(box: Sequence[Any]) -> tuple[int, int, int, int]:
    """One edge box ``(x, y, w, h)`` as a tuple of ints."""
    if len(box) != 4:
        raise ValueError(f"an edge box is (x, y, w, h), not {box!r}")
    return tuple(int(v) for v in box)  # type: ignore[return-value]


class SpotFocusService:
    """Run the spot focus lock: worker process, move relay, calibration.

    Parameters
    ----------
    mmc : CMMCorePlus-like
        Needs ``setPosition``, ``getPosition``, ``waitForDevice``,
        ``getProperty`` and ``setProperty``.
    piezo_label : str
        The piezo the loop drives (default ``"PiezoStage:P:34"``).
    crisp_label : str or None
        The CRISP device. When set (and no *crisp* is given) an
        :class:`~CRISPy.controller.ASICrispController` is built for it on first
        use. :meth:`led_on` then lights the LED with a raw serial command
        (``set_led``) while some user (preview, lock, manual) holds it, and
        :meth:`led_off` switches it off when the last one lets go. The CRISP
        state is never touched.
    source_spec : dict
        Passed to ``make_source`` in the worker (``{"kind": "toupcam"}`` or
        ``{"kind": "fake", ...}``).
    led_intensity : float or None
        CRISP LED intensity to set on :meth:`start` (``None`` leaves it).
    ready_timeout_s : float
        How long :meth:`wait_ready` waits for the worker by default.
    crisp : CrispController or None
        The CRISP controller for the LED and the piezo joystick. The panel
        passes the host's own; None builds one from *crisp_label*, or means no
        LED / joystick handling.
    """

    telemetry = Signal(dict)
    camera_changed = Signal(dict)
    """The camera's info after a ``set_camera`` (actual exposure, gain, ROI)."""
    fault = Signal(str)
    state = Signal(str)
    moved = Signal(dict)

    def __init__(
        self,
        mmc: Any,
        piezo_label: str = PIEZO_P_LABEL,
        crisp_label: str | None = None,
        source_spec: dict | None = None,
        *,
        led_intensity: float | None = None,
        ready_timeout_s: float = 30.0,
        crisp: CrispController | None = None,
    ) -> None:
        self._mmc = mmc
        self._piezo = piezo_label
        self._crisp_label = crisp_label
        self._crisp_ctl: CrispController | None = crisp
        #: The lazy build of the controller from the label was tried and failed.
        self._crisp_failed = False
        self._crisp_build_lock = threading.Lock()
        self._led = led_intensity
        self._ready_timeout = ready_timeout_s
        self._spec = dict(source_spec or DEFAULT_SOURCE_SPEC)
        analysis = self._spec.get("analysis") or {}
        #: The detector setting (what the worker uses where no calibration
        #: applies), mirrored here for the mismatch check of :meth:`start`.
        self._detector: str = str(analysis.get("detector", DEFAULT_DETECTOR))
        self._detector_sigma_px = float(
            analysis.get("detector_sigma_px", DEFAULT_SIGMA_PX)
        )

        self._lock = threading.Lock()
        #: The GUI thread and the reader thread (``moved`` replies) both send.
        self._send_lock = threading.Lock()
        self._state = "idle"
        self._shutting_down = False
        self._move_busy = False
        self._ready = threading.Event()
        self._info: dict = {}
        self._preview = False
        #: The reader's view of the worker's shared frame buffer.
        self._fb: FrameBuffer | None = None
        self._fb_name: str | None = None
        self._fb_lock = threading.Lock()
        self._meas: Queue[dict] = Queue()
        #: Who needs the LED lit ("preview", "lock", "manual", ...). It goes off
        #: when the last one lets go.
        self._led_owners: set[str] = set()
        self._led_lock = threading.RLock()
        #: The piezo joystick is off (the lock runs); see ``_set_joystick_locked``.
        self._joystick_locked = False
        self._joystick_lock = threading.Lock()
        self._last_fault: str | None = None
        self._moves: list[float] = []  # setPosition durations (ms)
        self._move_times: list[float] = []  # wall times of moves
        self._last_telemetry: dict | None = None

        self._proc, self._conn = launch_worker(self._spec)
        self._reader = threading.Thread(
            target=self._read_loop, name="spot-focus-reader", daemon=True
        )
        self._reader.start()

    # -- context manager --------------------------------------------------

    def __enter__(self) -> SpotFocusService:
        """Return self."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Shut the worker down."""
        self.shutdown()

    # -- properties -------------------------------------------------------

    @property
    def current_state(self) -> str:
        """The service state: idle, preview, running, stopped or fault."""
        return self._state

    @property
    def joystick_locked(self) -> bool:
        """True while the piezo joystick is disabled for the running lock."""
        return self._joystick_locked

    @property
    def preview_active(self) -> bool:
        """True while the live camera view is on (also during a run)."""
        return self._preview

    @property
    def camera_info(self) -> dict:
        """The camera's ``ready`` info, kept current by ``set_camera``.

        Keys: ``shm, sensor, bits, model, serial, exposure_range_ms,
        gain_range, roi, exposure_ms, gain`` (and ``channel``, ``size``).
        """
        return dict(self._info)

    @property
    def detector_setting(self) -> tuple[str, float]:
        """The edge detector setting and its ``gaussderiv`` width (sensor px).

        Set by ``set_camera(detector=...)`` or the spec's ``analysis``. New
        calibrations are measured with it; a lock needs a calibration made with
        the same one (:func:`~.spot_focus.detector_mismatch`).
        """
        return self._detector, self._detector_sigma_px

    @property
    def worker_pid(self) -> int | None:
        """The worker process id."""
        return self._proc.pid

    @property
    def info(self) -> dict:
        """The worker's ``ready`` payload (camera info); see :attr:`camera_info`."""
        return dict(self._info)

    @property
    def last_telemetry(self) -> dict | None:
        """The newest telemetry message, if any."""
        return self._last_telemetry

    def wait_ready(self, timeout_s: float | None = None) -> bool:
        """Block until the worker reports ``ready`` (or *timeout_s*)."""
        return self._ready.wait(self._ready_timeout if timeout_s is None else timeout_s)

    # -- control ----------------------------------------------------------

    def start(
        self,
        params: SpotLoopParams,
        calibration: FocusCalibration | EdgeCalibration,
        led_intensity: float | None = None,
    ) -> None:
        """Light the CRISP LED, read P once, and start the loop.

        *calibration* is a :class:`~.spot_focus.FocusCalibration`; a single
        :class:`~.spot_focus.EdgeCalibration` is wrapped into a one-edge one.

        Raises
        ------
        RuntimeError
            If the calibration was made with another edge detector than the
            setting ("calibrated with halfmax; the detector setting is
            gaussderiv: recalibrate"), or the worker is dead or not ready.
        """
        from .spot_focus import EdgeCalibration, FocusCalibration, detector_mismatch

        if isinstance(calibration, EdgeCalibration):
            calibration = FocusCalibration.from_dict(calibration.to_dict())
        why = detector_mismatch(calibration, *self.detector_setting)
        if why:
            raise RuntimeError(why)
        if self._state == "fault" and not self._proc.is_alive():
            raise RuntimeError("spot focus worker is dead; create a new service")
        if not self.wait_ready():
            raise RuntimeError("spot focus worker did not become ready")
        p_start = float(self._mmc.getPosition(self._piezo))
        params = self._with_travel_limits(params, p_start)
        self.led_on(led_intensity, owner="lock")
        self._last_fault = None
        self._moves.clear()
        self._move_times.clear()
        self._send(
            {
                "type": "start",
                "params": params.to_dict(),
                "calibration": calibration.to_dict(),
                "p_start_um": p_start,
            }
        )

    def _with_travel_limits(
        self, params: SpotLoopParams, p_start: float
    ) -> SpotLoopParams:
        """Fill missing absolute P limits from the device, once, at start.

        Only when *params* has neither bound. Reads the ``Position`` property
        limits if the piezo exposes them (a one-time read, like ``getPosition``;
        nothing is polled while locked). Any failure, a missing property or a
        range that does not contain *p_start* leaves the bounds at None: a
        missing limit source never faults the lock, and the excursion limit
        from the start P remains the guard.
        """
        if params.p_min_um is not None or params.p_max_um is not None:
            return params
        mmc, label = self._mmc, self._piezo
        try:
            for prop in ("Position", "Position(um)"):
                if not (
                    mmc.hasProperty(label, prop) and mmc.hasPropertyLimits(label, prop)
                ):
                    continue
                lo = float(mmc.getPropertyLowerLimit(label, prop))
                hi = float(mmc.getPropertyUpperLimit(label, prop))
                if math.isfinite(lo) and math.isfinite(hi) and lo <= p_start <= hi:
                    logger.info(
                        "Spot focus: using %s.%s limits [%g, %g] um (start P %.2f um).",
                        label,
                        prop,
                        lo,
                        hi,
                        p_start,
                    )
                    return replace(params, p_min_um=lo, p_max_um=hi)
        except Exception:
            logger.debug("Could not read the piezo limits.", exc_info=True)
        logger.info(
            "Spot focus: no piezo travel limits found; guarding by the "
            "excursion limit from the start P %.2f um.",
            p_start,
        )
        return params

    def stop(self) -> None:
        """Stop the loop, release the LED and the joystick. Never moves P.

        Back to ``preview`` if the live view was on, else ``stopped``.
        """
        self._send({"type": "stop"})
        if self._state != "fault":
            self._set_state("preview" if self._preview else "stopped")
        self._set_joystick_locked(False)
        self.led_off("lock")

    def start_preview(self) -> None:
        """Turn the live camera view on (frames into the shared buffer)."""
        self._preview = True
        self._send({"type": "preview_start"})
        if self._state in ("idle", "stopped"):
            self._set_state("preview")

    def stop_preview(self) -> None:
        """Turn the live camera view off. A running lock keeps running."""
        self._preview = False
        self._send({"type": "preview_stop"})
        if self._state == "preview":
            self._set_state("idle")

    def latest_frame(self, last_seq: int | None = None) -> LiveFrame | None:
        """The newest frame as a :class:`LiveFrame` copy, or None.

        None if the buffer is not attached, or nothing is newer than
        *last_seq*. Safe to call from the GUI thread.
        """
        with self._fb_lock:
            fb = self._fb
            return None if fb is None else fb.read(last_seq)

    def update_params(self, params: SpotLoopParams) -> None:
        """Change the loop parameters (including ``offset_um``) while running."""
        self._send({"type": "update_params", "params": params.to_dict()})

    def set_edge_box(self, box: Sequence[float] | None) -> None:
        """Set the edge box ``(x, y, w, h)`` the preview measures; None removes it.

        While the lock runs the calibration's own box is measured instead. When
        it is the held calibration's box (see :meth:`set_calibration`) the
        preview telemetry also carries the focus error ``along_um``.
        """
        self._send(
            {
                "type": "set_edge_box",
                "box": None if box is None else [int(v) for v in box],
            }
        )

    def set_calibration(self, cal: FocusCalibration | None) -> None:
        """Hand the worker a calibration for the preview error (None clears it).

        The worker also keeps the one it was started with. Ignored by the
        worker while the lock runs.
        """
        self._send(
            {
                "type": "set_calibration",
                "calibration": None if cal is None else cal.to_dict(),
            }
        )

    def set_camera(
        self,
        exposure_ms: float | None = None,
        gain: int | None = None,
        roi: tuple[int, int, int, int] | None = None,
        channel: str | None = None,
        detector: str | None = None,
        detector_sigma_px: float | None = None,
    ) -> None:
        """Set exposure, gain, ROI, channel and edge detector on the worker, live.

        *roi* is ``(x, y, w, h)`` in sensor pixels; ``(0, 0, 0, 0)`` is full
        frame. *channel* is ``"sum"`` or ``"red"``. *detector* is
        ``"gaussderiv"`` or ``"halfmax"`` with the ``gaussderiv`` width
        *detector_sigma_px* (sensor px; the present one if None): the setting
        for measurements no calibration applies to and for new calibrations.
        ``None`` leaves a value.

        Raises
        ------
        ValueError
            For an unknown *detector* or a non-positive *detector_sigma_px*.
        """
        if detector is not None:
            if detector not in EDGE_DETECTORS:
                raise ValueError(
                    f"detector must be one of {EDGE_DETECTORS}, not {detector!r}"
                )
            if detector_sigma_px is None:
                detector_sigma_px = self._detector_sigma_px
            if not (math.isfinite(detector_sigma_px) and detector_sigma_px > 0):
                raise ValueError(f"bad detector_sigma_px {detector_sigma_px!r}")
            self._detector, self._detector_sigma_px = detector, float(detector_sigma_px)
        self._send(
            {
                "type": "set_camera",
                "exposure_ms": exposure_ms,
                "gain": gain,
                "roi": None if roi is None else tuple(int(v) for v in roi),
                "channel": channel,
                "detector": detector,
                "detector_sigma_px": detector_sigma_px,
            }
        )

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Stop the worker process and the reader thread. Idempotent."""
        if self._shutting_down:
            return
        self._shutting_down = True
        # Release whenever start() lit the LED, not only once the worker has
        # reported "running": Stop or close right after Start used to leave
        # the LED on (seen on the rig PC, 2026-10-27).
        with self._led_lock:
            had_led = bool(self._led_owners)
            self._led_owners.clear()
            if had_led:
                self._send_led(False)
        self._set_joystick_locked(False)
        self._send({"type": "shutdown"})
        # The reader keeps draining meanwhile: a worker blocked on a full pipe
        # would never read ``shutdown``.
        self._proc.join(timeout_s)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout_s)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join(timeout_s)
        try:
            self._conn.close()
        except OSError:
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout_s)
        self._close_frames()

    # -- CRISP LED --------------------------------------------------------

    @property
    def crisp_label(self) -> str | None:
        """The CRISP device label, ``None`` when there is no CRISP device."""
        if self._crisp_ctl is not None:
            return self._crisp_ctl.label
        return self._crisp_label or None

    @property
    def led_is_on(self) -> bool:
        """True while some user holds the LED lit (always False with no CRISP)."""
        return bool(self._led_owners)

    def _controller(self) -> CrispController | None:
        """The CRISP controller, built from the label on first use; None if none.

        A failing build is logged once and the service then runs without one.
        """
        if self._crisp_ctl is not None or self._crisp_failed:
            return self._crisp_ctl
        if not self._crisp_label:
            return None
        with self._crisp_build_lock:
            if self._crisp_ctl is None and not self._crisp_failed:
                try:
                    from ..controller import ASICrispController

                    self._crisp_ctl = ASICrispController(self._crisp_label, self._mmc)
                except Exception:
                    self._crisp_failed = True
                    logger.warning(
                        "could not build the CRISP controller for %r; the LED and "
                        "the joystick are left alone",
                        self._crisp_label,
                        exc_info=True,
                    )
        return self._crisp_ctl

    def _send_led(self, on: bool) -> None:
        """Switch the LED with a raw command; errors are logged, never raised."""
        ctl = self._controller()
        if ctl is None:
            return
        try:
            ctl.set_led(on)
        except Exception:
            logger.warning(
                "could not switch the CRISP LED %s",
                "on" if on else "off",
                exc_info=True,
            )

    def _write_led_intensity(self) -> None:
        ctl = self._controller()
        if ctl is None or self._led is None:
            return
        try:
            self._mmc.setProperty(ctl.label, CRISP_LED_PROP, self._led)
        except Exception:
            logger.warning("could not set the CRISP LED intensity", exc_info=True)

    def led_on(self, intensity: float | None = None, *, owner: str = "manual") -> None:
        """Light the CRISP LED for *owner*: set the intensity, then switch it on.

        The raw ``set_led(True)`` is sent when the first *owner* arrives; later
        owners only join. *intensity* (else the one given at construction) is
        set every call. Never raises (errors are logged) and does nothing
        without a CRISP controller.
        """
        if self._controller() is None:
            return
        with self._led_lock:
            first = not self._led_owners
            self._led_owners.add(owner)
            if intensity is not None:
                self._led = intensity
            self._write_led_intensity()
            if first:
                self._send_led(True)

    def led_off(self, owner: str = "manual") -> None:
        """Let go of the LED; it goes off once no owner is left.

        Idempotent and never raises. Does nothing for an *owner* that does not
        hold the LED.
        """
        with self._led_lock:
            if owner not in self._led_owners:
                return
            self._led_owners.discard(owner)
            if not self._led_owners:
                self._send_led(False)

    def set_led_intensity(self, value: float) -> None:
        """Change the LED intensity; applied at once if the LED is lit."""
        self._led = value
        if self.led_is_on:
            with self._led_lock:
                self._write_led_intensity()

    # -- piezo joystick ---------------------------------------------------

    def _set_joystick_locked(self, locked: bool) -> None:
        """Disable (*locked*) or restore the piezo joystick; once per transition.

        Called on entering and leaving ``running`` and on every stop / fault /
        shutdown path, so a crash cannot leave the wheel dead. Idempotent, never
        raises (errors are logged), and does nothing without a CRISP controller.
        """
        ctl = self._controller()
        if ctl is None:
            return
        with self._joystick_lock:
            if locked == self._joystick_locked:
                return
            self._joystick_locked = locked
            try:
                ctl.set_joystick_enabled(not locked)
            except Exception:
                logger.warning(
                    "could not %s the piezo joystick",
                    "disable" if locked else "restore",
                    exc_info=True,
                )

    # -- calibration ------------------------------------------------------

    def calibrate(
        self,
        box: Sequence[Any],
        *,
        span_um: float = 2.0,
        steps: int = 9,
        n_frames: int = 3,
        settle_s: float = 0.25,
        record_path: str | Path | None = None,
    ) -> FocusCalibration:
        """Sweep P around its current value, measure the edge and fit it.

        The user has focused on the cells, so the current P is the zero (best
        focus). The sweep goes in increasing P over ``+-span_um / 2``; at each
        step the worker snaps *n_frames* fresh frames (it drops stale ones) and
        measures the edge in the box. P goes back to the start, the edge is
        measured once more (a drift / hysteresis check) and fitted: its slope
        and sign, zero (mean of the start and end measurement), noise, angle
        and contrast.

        Only while the loop is stopped and no acquisition runs. Uses
        ``waitForDevice`` (this polls the Tiger, which is fine for a short,
        user-started procedure with the serial line otherwise idle; the "never
        poll" rule is for the running lock and for volumes). Live streaming is
        paused meanwhile and restored; P, the LED claim and the preview are put
        back even on failure. Run it off the GUI thread.

        Parameters
        ----------
        box : Sequence
            The edge box ``(x, y, w, h)`` in sensor pixels.
        span_um : float
            Total sweep span in um of P.
        steps : int
            Number of sweep points.
        n_frames : int
            Good frames averaged per point (the zero measurements use
            ``max(2 * n_frames, 10)``, which also detects axis and polarity).
        settle_s : float
            Extra pause after each piezo move, before the worker's own settle.
        record_path : str | Path | None
            If given, save the sweep there with ``numpy.savez_compressed``
            (``.npz`` is appended): ``p_um``, ``edge_px`` of shape
            ``(steps, 1)`` (nan where the edge was not measured), ``boxes``
            ``(1, 4)``, ``axes``, ``polarities``, ``frames`` (one raw frame per
            step) and ``p_c``, ``e_start``, ``e_end``, ``noise_px`` (one value).

        Returns
        -------
        FocusCalibration
            The edge with its zero P (the start P). Its ``warnings`` list the
            edge's own warnings.

        Raises
        ------
        RuntimeError
            If the loop or an acquisition is running, or the worker is dead.
        ValueError
            With a plain message if the edge cannot be measured, leaves its box
            or gives no usable line.
        """
        import numpy as np

        from .spot_focus import FocusCalibration, fit_edge_calibration, snap_box

        if self._state == "running":
            raise RuntimeError("stop the loop before calibrating")
        mda = getattr(self._mmc, "mda", None)
        if mda is not None and mda.is_running():
            raise RuntimeError("an acquisition is running; calibrate after it ends")
        if not self.wait_ready():
            raise RuntimeError("spot focus worker did not become ready")
        mmc = self._mmc
        sbox = snap_box(_as_box(box))
        # Every measurement of the sweep (and so the stored calibration) uses the
        # detector setting as it is now.
        det = self.detector_setting
        p_c = float(mmc.getPosition(self._piezo))
        ps = p_c + np.linspace(-span_um / 2.0, span_um / 2.0, steps)
        n_zero = max(2 * n_frames, 10)
        self.led_on(owner="calibrate")
        resume_preview = self._preview
        if resume_preview:  # pause Live without touching the service state
            self._send({"type": "preview_stop"})
        centred = False
        try:
            m0 = self._snap_edge(n_zero, p_c, sbox, None, None, False, det)
            if m0["n"] == 0:
                raise ValueError(_edge_text(m0.get("reason", "")))
            axis, pol = m0.get("axis"), m0.get("polarity")
            edge_px = np.full(len(ps), np.nan)
            frames: list[Any] = []
            for s, p in enumerate(ps):
                mmc.setPosition(self._piezo, float(p))
                mmc.waitForDevice(self._piezo)
                time.sleep(settle_s)
                m = self._snap_edge(n_frames, float(p), sbox, axis, pol, True, det)
                if m["n"]:
                    edge_px[s] = float(m["edge_mean"])
                elif "left the box" in str(m.get("reason", "")):
                    raise ValueError(_edge_text(m["reason"]))
                frames.append(m.get("frame"))
            mmc.setPosition(self._piezo, p_c)
            mmc.waitForDevice(self._piezo)
            time.sleep(settle_s)
            m1 = self._snap_edge(n_zero, p_c, sbox, axis, pol, False, det)
            centred = True
            e_start = float(m0["edge_mean"])
            e_end = float(m1["edge_mean"]) if m1["n"] else math.nan
            noise = float(m0["edge_sd"])
            warns: list[str] = []
            saved = self._save_record(
                record_path,
                p_um=ps,
                edge_px=edge_px[:, None],
                frames=frames,
                boxes=np.asarray([sbox]),
                axes=np.asarray([axis or ""]),
                polarities=np.asarray([int(pol or 0)]),
                p_c=p_c,
                e_start=e_start,
                e_end=e_end,
                noise_px=noise,
            )
            if record_path is not None and saved is None:
                warns.append("could not save the calibration record")
            own: list[str] = []
            if math.isfinite(e_end):
                e0 = 0.5 * (e_start + e_end)
            else:
                e0 = e_start
                own.append("could not re-measure the edge at the start position")
            edge = fit_edge_calibration(
                ps.tolist(),
                edge_px.tolist(),
                box=sbox,
                axis=str(axis),
                polarity=int(pol),
                p0_um=p_c,
                e0_px=e0,
                noise_px=noise,
                angle_deg=float(m0["angle_mean"]),
                contrast=float(m0["contrast_mean"]),
                linear_range_um=span_um / 2.0,
                detector=det[0],
                detector_sigma_px=det[1],
                edge_width_px=float(m0.get("edge_width_mean", math.nan)),
            )
            drift = abs(e_end - e_start)
            if math.isfinite(drift) and drift > max(
                3.0 * noise, 0.05 * abs(edge.slope_px_per_um)
            ):
                own.append(
                    f"the edge moved {drift:.2f} px between the start and the "
                    "end of the sweep (drift or hysteresis)"
                )
            edge.warnings.extend(own)
            warns.extend(edge.warnings)
            return FocusCalibration(
                edge=edge,
                p0_um=p_c,
                created=datetime.now().isoformat(timespec="seconds"),
                record_path="" if saved is None else str(saved),
                warnings=warns,
            )
        finally:
            try:
                mmc.setPosition(self._piezo, p_c)
                mmc.waitForDevice(self._piezo)
                if not centred:  # the fake camera follows P only through a measure
                    with contextlib.suppress(RuntimeError):
                        self._request_measurement(
                            {
                                "type": "measure",
                                "n_frames": 1,
                                "p_um": p_c,
                                "box": list(sbox),
                                "timeout_s": 0.5,
                            },
                            1,
                        )
            finally:
                self.led_off("calibrate")
                if resume_preview:
                    self._send({"type": "preview_start"})

    def zero(self, cal: FocusCalibration, *, n_frames: int = 8) -> FocusCalibration:
        """Make the present edge position the zero (best focus) of *cal*.

        Reads P once, measures the edge in its box with its axis and polarity,
        and returns :meth:`FocusCalibration.rezeroed` of it. The slope and the
        range are kept. Only while the loop is stopped; sends no move.

        Parameters
        ----------
        cal : FocusCalibration
            The calibration to re-zero.
        n_frames : int
            Good frames to average.

        Returns
        -------
        FocusCalibration
            *cal* with ``e0_px`` and ``p0_um`` at the present edge and P.

        Raises
        ------
        RuntimeError
            If the loop is running or the worker is dead.
        ValueError
            If the edge cannot be measured (the reason is in the message).
        """
        if self._state == "running":
            raise RuntimeError("stop the loop before zeroing")
        if not self.wait_ready():
            raise RuntimeError("spot focus worker did not become ready")
        p = float(self._mmc.getPosition(self._piezo))
        self.led_on(owner="zero")
        try:
            e = cal.edge
            m = self._snap_edge(
                n_frames,
                p,
                e.box,
                e.axis,
                e.polarity,
                False,
                (e.detector, e.detector_sigma_px),
            )
        finally:
            self.led_off("zero")
        if m["n"] == 0:
            raise ValueError(_edge_text(m.get("reason", "")))
        return cal.rezeroed(float(m["edge_mean"]), p)

    def measure(
        self,
        n_frames: int = 20,
        p_um: float | None = None,
        *,
        box: Sequence[Any],
        axis: str | None = None,
        polarity: int | None = None,
        return_frame: bool = False,
    ) -> dict:
        """One averaged edge measurement from the worker (loop stopped).

        Parameters
        ----------
        n_frames : int
            Good frames to average; all were exposed after the request.
        p_um : float | None
            P the fake camera should follow (a real camera ignores it).
        box : Sequence
            The edge box ``(x, y, w, h)`` in sensor pixels.
        axis : str | None
            ``"x"`` / ``"y"`` edge normal; detected on the first good frame
            when None.
        polarity : int | None
            +1 / -1 bright side; detected when None.
        return_frame : bool
            Also return one raw full-ROI frame under ``"frame"``.

        Returns
        -------
        dict
            ``p_um, n, edge_mean, edge_sd, x_mean, y_mean, contrast_mean,
            angle_mean, residual_mean, axis, polarity, reason, roi`` (and
            ``frame``). ``reason`` is why the last frame was bad when ``n`` is 0.
        """
        if self._state == "running":
            raise RuntimeError("stop the loop before measuring")
        return self._snap_edge(
            n_frames, p_um, _as_box(box), axis, polarity, return_frame
        )

    def _snap_edge(
        self,
        n_frames: int,
        p_um: float | None,
        box: Sequence[float],
        axis: str | None,
        polarity: int | None,
        return_frame: bool,
        detector: tuple[str, float] | None = None,
    ) -> dict:
        """Ask the worker for *n_frames* fresh fixes of *box*; wait for the reply.

        *axis* and *polarity* None are detected. *detector* is
        ``(detector, sigma_px)`` (a calibration's own); None uses the worker's
        detector setting. The reply names the detector it was measured with.
        """
        msg: dict[str, Any] = {
            "type": "measure",
            "n_frames": int(n_frames),
            "p_um": p_um,
            "box": [int(v) for v in box],
            "axis": axis,
            "polarity": polarity,
            "return_frame": return_frame,
        }
        if detector is not None:
            msg["detector"], msg["detector_sigma_px"] = detector
        return self._request_measurement(msg, int(n_frames))

    def _request_measurement(self, msg: dict, n_frames: int) -> dict:
        self._flush_measurements()
        if not self._send(msg):
            raise RuntimeError("spot focus worker is dead")
        try:
            return self._meas.get(timeout=self._measure_timeout(n_frames))
        except Empty:
            raise RuntimeError("spot focus worker did not answer") from None

    @staticmethod
    def _save_record(path: str | Path | None, **arrays: Any) -> Path | None:
        """Save the sweep as ``.npz``; None if not wanted or not possible."""
        import numpy as np

        if path is None:
            return None
        out = Path(path)
        if out.suffix != ".npz":
            out = out.with_name(out.name + ".npz")
        frames = [f for f in arrays["frames"] if f is not None]
        if frames:
            arrays["frames"] = np.stack(
                [np.zeros_like(frames[0]) if f is None else f for f in arrays["frames"]]
            )
        else:
            arrays["frames"] = np.zeros((0,), dtype=np.uint8)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(out, **arrays)
        except OSError:
            logger.warning(
                "could not save the calibration record %s", out, exc_info=True
            )
            return None
        return out

    # -- stats ------------------------------------------------------------

    def stats(self) -> dict:
        """``n_moves``, ``mean_dt_ms``, ``max_dt_ms`` and ``moves_per_min``."""
        dts = list(self._moves)
        times = list(self._move_times)
        n = len(dts)
        rate = 0.0
        if n >= 2 and times[-1] > times[0]:
            rate = (n - 1) * 60.0 / (times[-1] - times[0])
        elif n == 1 and self._state == "running":
            rate = 0.0
        return {
            "n_moves": n,
            "mean_dt_ms": (sum(dts) / n) if n else 0.0,
            "max_dt_ms": max(dts) if n else 0.0,
            "moves_per_min": rate,
        }

    # -- internals --------------------------------------------------------

    def _measure_timeout(self, n_frames: int) -> float:
        """Generous wait for a ``measurement`` reply (slow at long exposures)."""
        exposure_s = float(self._info.get("exposure_ms") or 0.0) / 1000.0
        return 30.0 + n_frames * max(1.0, 3.0 * exposure_s) + 10.0 * exposure_s

    def _attach_frames(self, name: str | None) -> None:
        """Attach to the worker's shared frame buffer (reader thread)."""
        if not name:
            return
        try:
            fb = FrameBuffer.attach(name)
        except Exception:
            logger.exception("could not attach to the shared frame buffer %r", name)
            self._info["shm_error"] = f"could not attach to {name!r}"
            return
        with self._fb_lock:
            self._fb, self._fb_name = fb, name

    def _close_frames(self) -> None:
        """Close the reader's handle and remove the name if the worker could not."""
        with self._fb_lock:
            fb, self._fb = self._fb, None
        if fb is not None:
            fb.close()
        if self._fb_name:
            # Normally the worker unlinked it at exit; after a kill it did not.
            unlink_by_name(self._fb_name)
            self._fb_name = None

    def _flush_measurements(self) -> None:
        while True:
            try:
                self._meas.get_nowait()
            except Empty:
                return

    def _send(self, msg: dict) -> bool:
        try:
            with self._send_lock:
                self._conn.send(msg)
            return True
        except (OSError, ValueError):
            if not self._shutting_down:
                self._worker_died()
            return False

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        prev, self._state = self._state, state
        # The wheel is off exactly while the lock runs (calibrate / zero are not
        # states, so they never touch it).
        if state == "running":
            self._set_joystick_locked(True)
        elif prev == "running":
            self._set_joystick_locked(False)
        self._emit(self.state, state)

    def _emit(self, signal: Any, *args: Any) -> None:
        try:
            signal.emit(*args)
        except Exception:
            logger.exception("spot focus slot raised")

    def _fault(self, reason: str) -> None:
        self._set_state("fault")
        self._set_joystick_locked(False)
        if reason != self._last_fault:
            self._last_fault = reason
            logger.warning("spot focus fault: %s", reason)
            self._emit(self.fault, reason)

    def _worker_died(self) -> None:
        self._set_joystick_locked(False)
        if self._shutting_down or self._last_fault == "worker died":
            return
        self._fault("worker died")

    def _read_loop(self) -> None:
        """Drain the worker until its pipe closes, also while shutting down."""
        while True:
            try:
                if not self._conn.poll(0.2):
                    if not self._proc.is_alive():
                        self._worker_died()
                        return
                    continue
                msg = self._conn.recv()
            except (EOFError, OSError, ValueError):
                self._worker_died()
                return
            try:
                self._dispatch(msg)
            except Exception:
                logger.exception("spot focus message %r failed", msg.get("type"))

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ready":
            self._info = dict(msg.get("info", {}))
            self._attach_frames(self._info.get("shm"))
            self._ready.set()
        elif kind == "camera":
            self._info.update(msg.get("info", {}))
            self._emit(self.camera_changed, dict(self._info))
        elif kind == "move":
            self._on_move(msg)
        elif kind == "telemetry":
            self._last_telemetry = msg
            self._emit(self.telemetry, msg)
        elif kind == "fault":
            self._fault(str(msg.get("reason", "fault")))
        elif kind == "status":
            st = str(msg.get("state", ""))
            if st == "idle" and self._preview:
                return  # the worker's start-up "idle" must not hide a preview
            if self._state != "fault" or st == "running":
                self._set_state(st)
        elif kind == "measurement":
            self._meas.put(msg)
        elif kind == "error":
            logger.error(
                "spot focus worker: %s\n%s",
                msg.get("message"),
                msg.get("traceback", ""),
            )
            self._fault(f"worker error: {msg.get('message')}")

    def _on_move(self, msg: dict) -> None:
        seq = msg["seq"]
        # Moves only while running: a late request after stop/fault is dropped.
        if self._state != "running" or self._move_busy or not self._proc.is_alive():
            self._send(
                {
                    "type": "moved",
                    "seq": seq,
                    "ok": False,
                    "dt_ms": 0.0,
                    "dropped": True,
                }
            )
            return
        self._move_busy = True
        ok = True
        t0 = time.perf_counter()
        try:
            self._mmc.setPosition(self._piezo, float(msg["target_um"]))  # no wait
        except Exception:
            ok = False
            logger.exception("spot focus setPosition failed")
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self._move_busy = False
        if ok:
            self._moves.append(dt_ms)
            self._move_times.append(time.monotonic())
        info = {
            "seq": seq,
            "ok": ok,
            "dt_ms": dt_ms,
            "target_um": msg["target_um"],
            "along_um": msg.get("along_um"),
        }
        self._send({"type": "moved", "seq": seq, "ok": ok, "dt_ms": dt_ms})
        self._emit(self.moved, info)
