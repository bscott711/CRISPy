from __future__ import annotations

from typing import Protocol

from pymmcore_plus import CMMCorePlus

from .discovery import ActuatorType, classify_focus_actuator, find_paired_actuator_label

# The ASITiger adapter's settable "CRISP State" values (exact strings matter).
STATE_PROP = "CRISP State"
LED_PROP = "LED Intensity"
# The wheel/knob bound to an ASITiger stage's manual input is a regular device
# property on the *stage* (not the CRISP device): e.g. PiezoStage:P:34's
# "JoystickInput" = "22 - right wheel". This project's rig encodes the
# correct per-axis binding in one Micro-Manager config preset (see
# set_joystick_enabled), rather than a raw ASI "J" serial command -- a raw
# "J <axis>=<code>" command changes the hardware but not the ASITiger
# adapter's own cached property value, which is unreliable (confirmed on the
# bench: it silently failed to restore a custom wheel binding on re-enable).
JOYSTICK_PROP = "JoystickInput"
JOYSTICK_OFF_VALUE = "0 - none"
JOYSTICK_GROUP = "Joystick Wheels"
JOYSTICK_PRESET = "Z_P"
# Per-axis calibration read from the controller; these define the mapping between
# the raw error signal (controller counts) and physical focus motion, and differ
# between actuators (a piezo and a Z stage calibrate to very different gains).
CAL_GAIN_PROP = "Calibration Gain"
CAL_RANGE_PROP = "Calibration Range(um)"
# Lock-offset setpoint: shifting it makes the servo hold a deliberately
# defocused plane while staying locked (e.g. a per-wavelength correction for
# axial chromatic aberration). LOCK_OFFSET_PROP is the read-only offset the
# servo is currently tracking; SET_LOCK_OFFSET_PROP is the writable target.
# Both are in the same controller counts as the error signal.
LOCK_OFFSET_PROP = "Lock Offset"
SET_LOCK_OFFSET_PROP = "Set Lock Offset (Advanced Users Only)"


class CrispController(Protocol):
    @property
    def label(self) -> str: ...
    @property
    def actuator_type(self) -> ActuatorType: ...
    def set_state(self, state: str) -> None: ...
    def set_log_cal(self) -> None: ...
    def set_dither(self) -> None: ...
    def set_gain_cal(self) -> None: ...
    def set_idle(self) -> None: ...
    def set_ready(self) -> None: ...
    def set_lock(self) -> None: ...
    def set_unlock(self) -> None: ...
    def set_reset_offset(self) -> None: ...
    def set_save(self) -> None: ...
    def set_led_intensity(self, value: int) -> None: ...
    def set_joystick_enabled(self, enabled: bool) -> None: ...
    def get_state(self) -> str: ...
    def get_calibration_gain(self) -> float | None: ...
    def get_calibration_range_um(self) -> float | None: ...
    def get_sensitivity_counts_per_um(self) -> float | None: ...
    def get_lock_offset_counts(self) -> float | None: ...
    def get_set_lock_offset_counts(self) -> float | None: ...
    def set_lock_offset_counts(self, counts: int) -> None: ...
    def set_lock_offset_um(
        self, um: float, counts_per_um: float | None = None
    ) -> None: ...
    def get_lock_offset_um(
        self, counts_per_um: float | None = None
    ) -> float | None: ...


class ASICrispController:
    def __init__(self, label: str, mmcore: CMMCorePlus | None = None):
        self._label = label
        self._mmcore = mmcore or CMMCorePlus.instance()
        # Resolve the paired actuator once; it does not change while loaded.
        self._actuator_type: ActuatorType = classify_focus_actuator(label, self._mmcore)
        # The stage device whose "JoystickInput" property this axis's wheel
        # lives on (e.g. PiezoStage:P:34), and the correct "on" value for it
        # per the user's own JOYSTICK_PRESET -- see set_joystick_enabled.
        self._joystick_stage_label = find_paired_actuator_label(label, self._mmcore)
        self._joystick_on_value = self._read_joystick_preset_value()

    @property
    def label(self) -> str:
        return self._label

    @property
    def actuator_type(self) -> ActuatorType:
        """``"piezo"``, ``"stage"``, or ``"unknown"`` for the paired focus device."""
        return self._actuator_type

    def set_state(self, state: str) -> None:
        """Write an exact ASITiger ``CRISP State`` value (e.g. ``"Lock"``)."""
        self._mmcore.setProperty(self._label, STATE_PROP, state)

    # --- calibration workflow ---
    def set_log_cal(self) -> None:
        self.set_state("loG_cal")

    def set_dither(self) -> None:
        self.set_state("Dither")

    def set_gain_cal(self) -> None:
        self.set_state("gain_Cal")

    # --- operation ---
    def set_idle(self) -> None:
        """Idle: servo off, LED off."""
        self.set_state("Idle")

    def set_ready(self) -> None:
        """Ready: LED on, awaiting a lock command."""
        self.set_state("Ready")

    def set_lock(self) -> None:
        """Engage focus lock on this axis (the Tiger card servos it autonomously)."""
        self.set_state("Lock")

    def set_unlock(self) -> None:
        """Release focus lock (returns to Ready, LED stays on)."""
        self.set_state("Ready")

    def set_reset_offset(self) -> None:
        """Re-zero the focus error / offset at the current Z position."""
        self.set_state("Reset Focus Offset")

    def set_save(self) -> None:
        """Persist the current CRISP settings to the controller's NVRAM."""
        self.set_state("Save to Controller")

    def set_led_intensity(self, value: int) -> None:
        self._mmcore.setProperty(self._label, LED_PROP, str(value))

    def get_state(self) -> str:
        try:
            return str(self._mmcore.getProperty(self._label, STATE_PROP))
        except Exception:
            return ""

    # --- joystick lockout ---
    def _read_joystick_preset_value(self) -> str | None:
        """Look up this axis's correct ``JoystickInput`` value from JOYSTICK_PRESET.

        Reading the "on" target from the user's own saved preset (rather than
        snapshotting whatever the property currently reads) means a wheel that
        is already mis-bound -- e.g. from a prior bug -- self-corrects the
        next time this axis unlocks, instead of that wrong state being
        preserved indefinitely.
        """
        if self._joystick_stage_label is None:
            return None
        try:
            settings = self._mmcore.getConfigData(JOYSTICK_GROUP, JOYSTICK_PRESET)
        except Exception:
            return None
        for dev, prop, value in settings:
            if dev == self._joystick_stage_label and prop == JOYSTICK_PROP:
                return str(value)
        return None

    def set_joystick_enabled(self, enabled: bool) -> None:
        """Enable/disable manual joystick/wheel input on this axis's actuator.

        Writes the ASITiger ``JoystickInput`` device property directly on the
        paired stage (through the Core, like any other property), restoring
        the exact value encoded in the user's ``JOYSTICK_PRESET`` config
        preset -- which already correctly encodes their wheel layout (e.g.
        left wheel -> Z stage, right wheel -> piezo) -- rather than a raw ASI
        serial command. No-ops silently if the paired stage device or a
        matching preset entry could not be resolved.
        """
        if self._joystick_stage_label is None:
            return
        if not enabled:
            self._mmcore.setProperty(
                self._joystick_stage_label, JOYSTICK_PROP, JOYSTICK_OFF_VALUE
            )
            return
        if self._joystick_on_value is not None:
            self._mmcore.setProperty(
                self._joystick_stage_label, JOYSTICK_PROP, self._joystick_on_value
            )

    # --- calibration / error scaling ---
    def _get_float(self, prop: str) -> float | None:
        try:
            return float(self._mmcore.getProperty(self._label, prop))
        except Exception:
            return None

    def get_calibration_gain(self) -> float | None:
        """Error-signal calibration gain (controller counts) for this axis."""
        return self._get_float(CAL_GAIN_PROP)

    def get_calibration_range_um(self) -> float | None:
        """Dither calibration travel, in microns, used to derive the gain."""
        return self._get_float(CAL_RANGE_PROP)

    def get_sensitivity_counts_per_um(self) -> float | None:
        """Error counts per micron of defocus: ``|Calibration Gain| / range``.

        This is the actuator-specific scaling that maps the raw error signal onto
        physical focus displacement; it lets a raw error/offset reading be
        expressed in microns and compared across axes with different actuators.
        Returns ``None`` if either calibration value is unavailable or the range
        is non-positive.
        """
        gain = self.get_calibration_gain()
        rng = self.get_calibration_range_um()
        if gain is None or rng is None or rng <= 0:
            return None
        return abs(gain) / rng

    # --- lock offset ---
    def _resolve_counts_per_um(self, counts_per_um: float | None) -> float:
        """Return an explicit *counts_per_um* or fall back to the device value.

        Raises
        ------
        RuntimeError
            If *counts_per_um* is ``None`` and the device's own
            ``|Calibration Gain| / Calibration Range(um)`` is unavailable, so
            no micron<->counts conversion is possible.
        """
        scale = counts_per_um or self.get_sensitivity_counts_per_um()
        if not scale:
            raise RuntimeError(
                f"No counts-per-micron scale available for {self._label} "
                "(Calibration Gain / Range could not be read) and none was "
                "passed explicitly."
            )
        return scale

    def get_lock_offset_counts(self) -> float | None:
        """Read-only offset the servo is currently holding, in controller counts.

        Near zero when locked and in focus; a non-zero steady value means the
        servo is deliberately holding a shifted plane (see
        :meth:`set_lock_offset_counts`). ``None`` if the property can't be read.
        """
        return self._get_float(LOCK_OFFSET_PROP)

    def get_set_lock_offset_counts(self) -> float | None:
        """Read back the *writable* lock-offset setpoint, in controller counts.

        Distinct from :meth:`get_lock_offset_counts` (the read-only tracked
        value): comparing the two detects an adapter that accepted a write into
        its cache without the controller acting on it, or vice versa.
        """
        return self._get_float(SET_LOCK_OFFSET_PROP)

    def set_lock_offset_counts(self, counts: int) -> None:
        """Command an **absolute** lock-offset setpoint, in controller counts.

        The Tiger card servos the focus actuator to hold this offset from the
        calibrated in-focus error value while staying locked. Not a delta --
        each call replaces the previous setpoint. The caller owns waiting for
        the actuator to settle and verifying the result.

        Parameters
        ----------
        counts : int
            Target offset in controller counts (rounded to an int on write).
        """
        self._mmcore.setProperty(
            self._label, SET_LOCK_OFFSET_PROP, str(int(round(counts)))
        )

    def set_lock_offset_um(self, um: float, counts_per_um: float | None = None) -> None:
        """Command an absolute lock-offset setpoint expressed in microns.

        Converts *um* to counts via *counts_per_um* (or the device's own
        sensitivity) and calls :meth:`set_lock_offset_counts`.

        Parameters
        ----------
        um : float
            Target focus offset from the calibrated plane, in microns.
        counts_per_um : float | None
            Conversion scale to use; ``None`` falls back to
            :meth:`get_sensitivity_counts_per_um`. Raises ``RuntimeError`` if
            neither is available.
        """
        scale = self._resolve_counts_per_um(counts_per_um)
        self.set_lock_offset_counts(round(um * scale))

    def get_lock_offset_um(self, counts_per_um: float | None = None) -> float | None:
        """The servo's currently-held lock offset in microns, or ``None``.

        ``None`` if the offset can't be read; raises ``RuntimeError`` if no
        micron<->counts scale is available (pass *counts_per_um* to avoid that).
        """
        counts = self.get_lock_offset_counts()
        if counts is None:
            return None
        return counts / self._resolve_counts_per_um(counts_per_um)
