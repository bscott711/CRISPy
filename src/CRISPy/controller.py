from typing import Protocol
from pymmcore_plus import CMMCorePlus

# The ASITiger adapter's settable "CRISP State" values (exact strings matter).
STATE_PROP = "CRISP State"
LED_PROP = "LED Intensity"


class CrispController(Protocol):
    @property
    def label(self) -> str: ...
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


class ASICrispController:
    def __init__(self, label: str, mmcore: CMMCorePlus | None = None):
        self._label = label
        self._mmcore = mmcore or CMMCorePlus.instance()

    @property
    def label(self) -> str:
        return self._label

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
