from __future__ import annotations

from typing import Protocol

from pymmcore_plus import CMMCorePlus

from .discovery import ActuatorType, classify_focus_actuator, find_tiger_hub

# The ASITiger adapter's settable "CRISP State" values (exact strings matter).
STATE_PROP = "CRISP State"
LED_PROP = "LED Intensity"
# Tiger axis letter for this CRISP axis's paired actuator (e.g. "P"), used to
# address the JOYSTICK (J) command below.
AXIS_LETTER_PROP = "AxisLetter"
# Per-axis calibration read from the controller; these define the mapping between
# the raw error signal (controller counts) and physical focus motion, and differ
# between actuators (a piezo and a Z stage calibrate to very different gains).
CAL_GAIN_PROP = "Calibration Gain"
CAL_RANGE_PROP = "Calibration Range(um)"


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
    def get_axis_letter(self) -> str | None: ...
    def set_joystick_enabled(self, enabled: bool) -> None: ...
    def get_state(self) -> str: ...
    def get_calibration_gain(self) -> float | None: ...
    def get_calibration_range_um(self) -> float | None: ...
    def get_sensitivity_counts_per_um(self) -> float | None: ...


class ASICrispController:
    def __init__(self, label: str, mmcore: CMMCorePlus | None = None):
        self._label = label
        self._mmcore = mmcore or CMMCorePlus.instance()
        # Resolve the paired actuator once; it does not change while loaded.
        self._actuator_type: ActuatorType = classify_focus_actuator(label, self._mmcore)
        # Resolve the Tiger axis letter + comm hub once, for the JOYSTICK (J)
        # command (see set_joystick_enabled); neither changes while loaded.
        self._axis_letter = self._get_str(AXIS_LETTER_PROP)
        self._hub_label = find_tiger_hub(self._mmcore)

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
    def get_axis_letter(self) -> str | None:
        """Tiger axis letter (e.g. ``"P"``) for this axis's paired actuator."""
        return self._axis_letter

    def _send_serial(self, cmd: str) -> str | None:
        """Send a raw command to the Tiger comm hub, if one was found."""
        if self._hub_label is None:
            return None
        try:
            self._mmcore.setProperty(self._hub_label, "SerialCommand", cmd)
            if self._mmcore.hasProperty(self._hub_label, "SerialResponse"):
                return str(self._mmcore.getProperty(self._hub_label, "SerialResponse"))
        except Exception:
            pass
        return None

    def set_joystick_enabled(self, enabled: bool) -> None:
        """Enable/disable manual joystick/wheel input on this axis's actuator.

        Sends ASI's ``JOYSTICK (J)`` command (``J <axis>+``/``J <axis>-``) to
        the Tiger comm hub. No-ops silently if the axis letter or comm hub
        could not be resolved (e.g. no Tiger hub loaded on this rig).
        """
        if self._axis_letter is None:
            return
        sign = "+" if enabled else "-"
        self._send_serial(f"J {self._axis_letter}{sign}")

    # --- calibration / error scaling ---
    def _get_float(self, prop: str) -> float | None:
        try:
            return float(self._mmcore.getProperty(self._label, prop))
        except Exception:
            return None

    def _get_str(self, prop: str) -> str | None:
        try:
            value = str(self._mmcore.getProperty(self._label, prop)).strip()
        except Exception:
            return None
        return value or None

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
