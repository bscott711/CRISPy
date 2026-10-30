"""Persisted settings of the spot focus lock (plain pydantic models).

Moved unchanged from pymmcore-gui's settings module so the host's saved ``spot_focus``
settings (calibration, edge boxes, detector, ROI, SDK folder) load as before. The
host owns where they are stored: see :mod:`~CRISPy.spot.store`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .spot_focus import FocusCalibration as SpotFocusCalibration
    from .spot_focus import SpotLoopParams


class SpotSourceSettingsV1(BaseModel):
    """Where the spot lock's frames come from (see ``toupcam_source.make_source``)."""

    kind: Literal["toupcam", "fake"] = "toupcam"
    """``"toupcam"`` is the AmScope camera; ``"fake"`` is a simulated spot."""
    serial: str | None = None
    """Camera serial (``None`` takes the first camera found)."""
    sdk_dir: str | None = None
    """Folder holding the vendor's ``toupcam.py``; ``None`` uses the
    ``TOUPCAM_SDK_DIR`` environment variable."""
    fake: dict[str, Any] = Field(default_factory=dict)
    """Keyword arguments of ``FakeSpotSource`` (``kind == "fake"`` only)."""


class SpotLoopParamsSettings(BaseModel):
    """Persisted form of ``CRISPy.spot.spot_focus.SpotLoopParams``."""

    kp: float = 0.6
    ki: float = 0.05
    deadband_um: float = 0.03
    max_step_um: float = 0.5
    max_rate_hz: float = 2.0
    avg_frames: int = 4
    offset_um: float = 0.0
    min_contrast: float = 10.0
    max_saturated_frac: float = 0.002
    min_valid_fraction: float = 0.5
    max_angle_change_deg: float = 5.0
    max_edge_residual_px: float = 3.0
    max_edge_disagreement_um: float = 1.0
    hold_band_um: float = 0.15
    """When the two edges disagree, hold on an edge this close to the target."""
    p_min_um: float | None = None
    p_max_um: float | None = None
    max_excursion_um: float = 5.0
    strikes: int = 5

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_range(cls, data: Any) -> Any:
        """Files written with the old 0..150 um default load as "no range".

        The real piezo P runs negative, so that default faulted a healthy lock.
        """
        if (
            isinstance(data, dict)
            and data.get("p_min_um") == 0.0
            and data.get("p_max_um") == 150.0
        ):
            data = {**data, "p_min_um": None, "p_max_um": None}
        return data

    def to_params(self) -> SpotLoopParams:
        """Return the numpy-only parameters the spot loop works with."""
        from .spot_focus import SpotLoopParams

        return SpotLoopParams.from_dict(self.model_dump())


class SpotFocusSettingsV1(BaseModel):
    """Spot focus lock: the CRISP LED spot on a side camera, held by piezo P.

    The loop runs from Start until Stop, independent of any acquisition (see
    ``docs/spot_focus_lock.md``).
    """

    # Don't fail if an extra key is present; just ignore it (backward compatibility).
    model_config = ConfigDict(extra="ignore")

    source: SpotSourceSettingsV1 = Field(default_factory=SpotSourceSettingsV1)
    exposure_ms: float = 5.0
    gain: int = 100
    """Camera gain in percent (ToupTek convention, 100 = unity)."""
    roi: tuple[int, int, int, int] | None = None
    """Camera ROI ``(x, y, w, h)``; ``None`` uses the full frame."""
    channel: Literal["sum", "red"] = "red"
    """Which Bayer sites the spot is measured on. The LED is 720 nm on a colour
    sensor, so only the red sites carry it."""
    bayer: Literal["RGGB", "BGGR", "GRBG", "GBRG"] = "RGGB"
    led_intensity: float | None = None
    """CRISP LED intensity set on Start; ``None`` leaves it as it is."""
    params: SpotLoopParamsSettings = Field(default_factory=SpotLoopParamsSettings)
    calibration: dict[str, Any] | None = None
    """``FocusCalibration.to_dict()`` of the last calibration (a single-edge
    ``EdgeCalibration`` dict from older versions also loads)."""
    edge_box: tuple[int, int, int, int] | None = None
    """The edge box ``(x, y, w, h)`` in sensor pixels; ``None`` until drawn."""
    edge_box_2: tuple[int, int, int, int] | None = None
    """The optional second edge box, same convention as ``edge_box``."""
    detector: Literal["gaussderiv", "halfmax"] = "gaussderiv"
    """How the edge point is found in each row (``measure_edge``). A calibration
    is only valid for the detector it was made with; the stored one wins for
    measurements it applies to and Lock refuses on a mismatch."""
    detector_sigma_px: float = Field(default=6.0, ge=2.0, le=12.0)
    """Width of the ``gaussderiv`` filter in sensor px (ignored by ``halfmax``)."""
    sweep_span_um: float = 2.0
    """Total piezo span of the calibration sweep, um."""
    sweep_steps: int = 9
    """Number of points of the calibration sweep."""
    sweep_frames: int = 3
    """Frames averaged per sweep point."""

    def calibration_status(self) -> tuple[SpotFocusCalibration | None, str]:
        """Return ``(calibration, message)``; never raises.

        ``message`` is empty for a valid calibration, else says why there is none
        (nothing stored yet, or an old / malformed calibration ignored).
        """
        from .spot_focus import load_calibration

        return load_calibration(self.calibration)

    def to_calibration(self) -> SpotFocusCalibration | None:
        """Return the stored calibration, ``None`` if there is none or it is bad."""
        return self.calibration_status()[0]
