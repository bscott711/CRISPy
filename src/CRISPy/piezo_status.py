"""Status words, the one contextual message and the joystick policy (Qt-free).

The piezo panel's spot-lock section shows five status words (Lock, Camera, LED,
Edge box, Calibration), a tracking-error label and ONE contextual message line. This
module maps the spot panel's existing state to them. It is pure: no Qt, no I/O, no
hardware. The caller (the panel) fills a :class:`SpotStatusInputs` from its own
attributes and asks for the words and the message.

Only ONE edge box exists in this UI. A saved ``edge_box_2`` is ignored by the caller,
and a calibration made with two edges is reported as stale: the caller sets
``box_changed`` when the calibration's edge count differs from 1.

Message priority (the first match wins; the wording is copied from
``spot/panel.py`` and ``spot/spot_focus*.py``, the source line is named at each
constant):

1. fault ("Fault: {reason}")
2. a failed start, then calibrate, then zero, then stop
3. camera error, then LED error
4. no edge box
5. calibration needs a recalibration: detector mismatch, then box changed
6. no CRISP device
7. an acquisition is running (idle / stopped / not busy only): Calibrate is refused
8. calibration required with the box set ("Calibrate first"; idle / stopped only)
9. no new frames
10. busy ("Starting...", "Calibrating...", "Zeroing...") or "Locked" while running
11. "Ready to lock"

The MDA note (7) sits before "Calibrate first" (8) because calibration is refused
during an acquisition, so the advice to calibrate would be wrong then.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# -- the status words (exact strings) ------------------------------------------

LOCK_IDLE = "Idle"
LOCK_STARTING = "Starting"
LOCK_CALIBRATING = "Calibrating"
LOCK_ZEROING = "Zeroing"
LOCK_RUNNING = "Running"
LOCK_STOPPED = "Stopped"
LOCK_FAULT = "Fault"
LOCK_WORDS = (
    LOCK_IDLE,
    LOCK_STARTING,
    LOCK_CALIBRATING,
    LOCK_ZEROING,
    LOCK_RUNNING,
    LOCK_STOPPED,
    LOCK_FAULT,
)

CAMERA_NOT_OPEN = "Not open"
CAMERA_OPENING = "Opening"
CAMERA_RUNNING = "Running"
CAMERA_NONE = "No camera"
CAMERA_ERROR = "Error"
CAMERA_WORDS = (
    CAMERA_NOT_OPEN,
    CAMERA_OPENING,
    CAMERA_RUNNING,
    CAMERA_NONE,
    CAMERA_ERROR,
)

LED_ON = "On"
LED_OFF = "Off"
LED_NO_CRISP = "No CRISP device"
LED_ERROR = "Error"
LED_WORDS = (LED_ON, LED_OFF, LED_NO_CRISP, LED_ERROR)

BOX_NOT_SET = "Not set"
BOX_SET = "Set"
BOX_WORDS = (BOX_NOT_SET, BOX_SET)

CAL_REQUIRED = "Required"
CAL_VALID = "Valid"
CAL_STALE = "Stale (edge box changed)"
CAL_MISMATCH = "Detector mismatch"
CAL_WORDS = (CAL_REQUIRED, CAL_VALID, CAL_STALE, CAL_MISMATCH)

NOT_AVAILABLE = "Not available"

# -- message texts (copied; the panel will point here later) --------------------

#: panel.py:107 ``NEED_BOX_TEXT``.
NEED_BOX_TEXT = "Draw an edge box across the edge that moves with focus"
#: panel.py:1119-1121 (the single-box wording; two boxes are not reported).
BOX_CHANGED_TEXT = "edge box changed since the calibration; recalibrate"
#: Wording copied from the old panel text.
NO_CRISP_TEXT = "no CRISP device (no CRISP label)"
#: spot_focus_service.py:614 (the refusal of Calibrate during an acquisition).
MDA_RUNNING_TEXT = "an acquisition is running; calibrate after it ends"
#: No panel wording exists (panel.py:1738 is a tooltip); the brief's exact text.
CALIBRATE_FIRST_TEXT = "Calibrate first"
#: live_view.py:888 ("No new frames.  " prefix of the status line, without the dot).
NO_NEW_FRAMES_TEXT = "No new frames"
#: Busy texts; the panel shows the bare ``_busy`` word (panel.py:1703).
STARTING_TEXT = "Starting..."
CALIBRATING_TEXT = "Calibrating..."
ZEROING_TEXT = "Zeroing..."
LOCKED_TEXT = "Locked"
READY_TEXT = "Ready to lock"


@dataclass(frozen=True)
class SpotStatusInputs:
    """The spot panel's state as plain values (every field has a safe default).

    Attributes
    ----------
    lock_state : str
        ``"idle"``, ``"running"``, ``"stopped"`` or ``"fault"`` (panel.py ``state``).
        Anything else counts as idle.
    busy : str
        ``""``, ``"starting"``, ``"calibrating"`` or ``"zeroing"`` (panel.py
        ``_busy``, a worker thread is running).
    camera_open : bool
        The camera streams (the preview or the lock runs).
    camera_opening : bool
        A thread is opening the camera (panel.py ``_preview_busy``).
    camera_missing : bool
        No camera was found (the "no camera" label of ``_on_preview_failed``).
    camera_error : str
        The camera error text ("" when none), e.g. the message of the
        ``Camera: {message}`` fault line (panel.py ``_on_preview_failed``,
        ``_apply_camera``).
    crisp_present : bool | None
        A CRISP device is known (``svc.crisp_label`` is not None). ``None`` while
        unknown (no service yet), which counts as present.
    led_on : bool
        The CRISP LED is lit (``svc.led_is_on``, the LED button state).
    led_error : str
        The LED failure text ("" when none; panel.py ``_on_led_failed``).
    edge_box_set : bool
        The single edge box is drawn (``_edge_box`` is not None). A saved second
        box is ignored by the caller.
    calibration_present : bool
        A usable calibration is loaded (``_cal`` is not None).
    detector_mismatch : str
        Why the calibration does not fit the detector setting ("" if it does;
        panel.py ``_mismatch_text()``).
    box_changed : bool
        The drawn box differs from the calibration's box, or the calibration was
        made with other than exactly one edge (panel.py ``_refresh_calibration``
        compares the snapped boxes). A two-edge calibration is reported here so it
        comes out as "Stale (edge box changed)".
    fault_reason : str
        The lock's fault reason ("" when none; panel.py ``_on_fault``).
    start_error, calibrate_error, zero_error, stop_error : str
        The last failure texts ("" when none; panel.py ``_on_start_failed``,
        ``_on_calibrate_failed``, ``_on_zero_failed``, ``stop``).
    no_new_frames : bool
        The live view got no new frame lately (live_view.py ``_set_status(stale)``).
    mda_running : bool
        An acquisition (MDA) is running: Calibrate is refused.
    tracking_error_um : float | None
        The latest edge error from the lock, um; ``None`` when unknown (panel.py
        ``_on_telemetry``: ``along_um``).
    """

    lock_state: str = "idle"
    busy: str = ""
    camera_open: bool = False
    camera_opening: bool = False
    camera_missing: bool = False
    camera_error: str = ""
    crisp_present: bool | None = None
    led_on: bool = False
    led_error: str = ""
    edge_box_set: bool = False
    calibration_present: bool = False
    detector_mismatch: str = ""
    box_changed: bool = False
    fault_reason: str = ""
    start_error: str = ""
    calibrate_error: str = ""
    zero_error: str = ""
    stop_error: str = ""
    no_new_frames: bool = False
    mda_running: bool = False
    tracking_error_um: float | None = None


@dataclass(frozen=True)
class StatusWords:
    """The five status words of the spot-lock section."""

    lock: str
    camera: str
    led: str
    edge_box: str
    calibration: str


_BUSY_WORDS = {
    "starting": LOCK_STARTING,
    "calibrating": LOCK_CALIBRATING,
    "zeroing": LOCK_ZEROING,
}
_BUSY_TEXTS = {
    "starting": STARTING_TEXT,
    "calibrating": CALIBRATING_TEXT,
    "zeroing": ZEROING_TEXT,
}


def lock_word(inp: SpotStatusInputs) -> str:
    """Return the lock word: a fault wins, then a busy thread, then the state."""
    if inp.lock_state == "fault" or inp.fault_reason:
        return LOCK_FAULT
    if inp.busy in _BUSY_WORDS:
        return _BUSY_WORDS[inp.busy]
    if inp.lock_state == "running":
        return LOCK_RUNNING
    if inp.lock_state == "stopped":
        return LOCK_STOPPED
    return LOCK_IDLE


def camera_word(inp: SpotStatusInputs) -> str:
    """Return the camera word: error, no camera, opening, running, else not open."""
    if inp.camera_error:
        return CAMERA_ERROR
    if inp.camera_missing:
        return CAMERA_NONE
    if inp.camera_opening:
        return CAMERA_OPENING
    if inp.camera_open:
        return CAMERA_RUNNING
    return CAMERA_NOT_OPEN


def led_word(inp: SpotStatusInputs) -> str:
    """Return the LED word: error, no CRISP device, on, else off."""
    if inp.led_error:
        return LED_ERROR
    if inp.crisp_present is False:
        return LED_NO_CRISP
    return LED_ON if inp.led_on else LED_OFF


def edge_box_word(inp: SpotStatusInputs) -> str:
    """Return "Set" with the single edge box drawn, else "Not set"."""
    return BOX_SET if inp.edge_box_set else BOX_NOT_SET


def calibration_word(inp: SpotStatusInputs) -> str:
    """Return the calibration word.

    No calibration is "Required"; a detector mismatch beats a changed box.
    """
    if not inp.calibration_present:
        return CAL_REQUIRED
    if inp.detector_mismatch:
        return CAL_MISMATCH
    if inp.box_changed:
        return CAL_STALE
    return CAL_VALID


def status_words(inp: SpotStatusInputs) -> StatusWords:
    """Return all five status words."""
    return StatusWords(
        lock=lock_word(inp),
        camera=camera_word(inp),
        led=led_word(inp),
        edge_box=edge_box_word(inp),
        calibration=calibration_word(inp),
    )


def tracking_error_text(inp: SpotStatusInputs) -> str:
    """Return the tracking error as "+0.012 µm", or "Not available".

    Shown only while the lock word is "Running" and the error is a finite number.
    """
    err = inp.tracking_error_um
    if lock_word(inp) != LOCK_RUNNING or err is None or not math.isfinite(err):
        return NOT_AVAILABLE
    return f"{err:+.3f} µm"


def message(inp: SpotStatusInputs) -> str:
    """Return the ONE contextual line, the worst problem first (see the module doc)."""
    lock = lock_word(inp)
    # 1. fault
    if lock == LOCK_FAULT:
        return f"Fault: {inp.fault_reason}" if inp.fault_reason else "Fault"
    # 2. failed start / calibrate / zero / stop
    if inp.start_error:
        return f"Could not start: {inp.start_error}"
    if inp.calibrate_error:
        return f"Calibration failed: {inp.calibrate_error}"
    if inp.zero_error:
        return f"Zero failed: {inp.zero_error}"
    if inp.stop_error:
        return f"Stop failed: {inp.stop_error}"
    # 3. camera error, then LED error
    if inp.camera_error:
        return f"Camera: {inp.camera_error}"
    if inp.led_error:
        return f"LED: {inp.led_error}"
    # 4. no edge box
    if not inp.edge_box_set:
        return NEED_BOX_TEXT
    # 5. recalibrate: detector mismatch, then a changed box
    if inp.calibration_present:
        if inp.detector_mismatch:
            return inp.detector_mismatch
        if inp.box_changed:
            return BOX_CHANGED_TEXT
    # 6. no CRISP device
    if inp.crisp_present is False:
        return NO_CRISP_TEXT
    at_rest = lock in (LOCK_IDLE, LOCK_STOPPED)
    # 7. an acquisition is running: Calibrate is refused
    if inp.mda_running and at_rest:
        return MDA_RUNNING_TEXT
    # 8. calibration required, box set
    if at_rest and not inp.calibration_present:
        return CALIBRATE_FIRST_TEXT
    # 9. no new frames
    if inp.no_new_frames:
        return NO_NEW_FRAMES_TEXT
    # 10. busy / locked
    if inp.busy in _BUSY_TEXTS:
        return _BUSY_TEXTS[inp.busy]
    if lock == LOCK_RUNNING:
        return LOCKED_TEXT
    # 11. ready
    return READY_TEXT


def joystick_for_lock(lock_active: bool) -> bool:
    """Return whether the joystick should be enabled for the lock's activity.

    The joystick is enabled iff the lock is not active. The checkbox starts checked;
    ``lockActiveChanged(True)`` unchecks it (returns False) and
    ``lockActiveChanged(False)`` checks it again (returns True). The user may still
    toggle it manually at any time.
    """
    return not lock_active
