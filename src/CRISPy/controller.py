from typing import Protocol
from pymmcore_plus import CMMCorePlus


class CrispController(Protocol):
    @property
    def label(self) -> str: ...
    def set_log_cal(self) -> None: ...
    def set_dither(self) -> None: ...
    def set_gain_cal(self) -> None: ...
    def set_idle(self) -> None: ...
    def set_led_intensity(self, value: int) -> None: ...


class ASICrispController:
    def __init__(self, label: str, mmcore: CMMCorePlus | None = None):
        self._label = label
        self._mmcore = mmcore or CMMCorePlus.instance()

    @property
    def label(self) -> str:
        return self._label

    def _set_state(self, state: str) -> None:
        self._mmcore.setProperty(self._label, "CRISP State", state)

    def set_log_cal(self) -> None:
        self._set_state("loG_cal")

    def set_dither(self) -> None:
        self._set_state("Dither")

    def set_gain_cal(self) -> None:
        self._set_state("gain_Cal")

    def set_idle(self) -> None:
        self._set_state("Idle")

    def set_led_intensity(self, value: int) -> None:
        self._mmcore.setProperty(self._label, "LED Intensity", str(value))
