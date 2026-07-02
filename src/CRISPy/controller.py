from __future__ import annotations

from typing import Protocol

from pymmcore_plus import CMMCorePlus

from .discovery import ActuatorType, classify_focus_actuator

# The ASITiger adapter's settable "CRISP State" values (exact strings matter).
STATE_PROP = "CRISP State"
LED_PROP = "LED Intensity"
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
