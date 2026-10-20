"""Tests for snapping, the sharpness score and thumbnails (no Qt, no camera)."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from CRISPy import imaging
from CRISPy.imaging import (
    core_snap,
    focus_score,
    set_snap_provider,
    snap_images,
    thumbnail,
)


def blurred(sigma: float, size: int = 96, seed: int = 0) -> np.ndarray:
    """A random dot pattern blurred by a Gaussian of *sigma* pixels (via the FFT)."""
    rng = np.random.default_rng(seed)
    img = rng.random((size, size)) ** 8 * 1000 + 100
    if sigma <= 0:
        return img
    f = np.fft.fftfreq(size)
    kernel = np.exp(-2 * (np.pi * sigma) ** 2 * (f[:, None] ** 2 + f[None, :] ** 2))
    return np.real(np.fft.ifft2(np.fft.fft2(img) * kernel))


@pytest.fixture(autouse=True)
def _no_provider():
    set_snap_provider(None)
    yield
    set_snap_provider(None)


def test_a_sharper_image_scores_higher() -> None:
    scores = [focus_score(blurred(s)) for s in (0.0, 1.0, 2.0, 4.0)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 3 * scores[-1]


def test_the_score_ignores_brightness() -> None:
    img = blurred(1.0)
    assert focus_score(img * 7.0) == pytest.approx(focus_score(img))


def test_degenerate_images_score_zero() -> None:
    assert focus_score(np.zeros((10, 10))) == 0.0
    assert focus_score(np.ones((2, 50))) == 0.0


def test_an_rgb_image_is_scored_in_grey() -> None:
    grey = blurred(1.0)
    rgb = np.stack([grey, grey, grey], axis=-1)
    assert focus_score(rgb) == pytest.approx(focus_score(grey))


def test_thumbnail_is_small_and_8_bit() -> None:
    thumb = thumbnail(np.arange(2048 * 1024, dtype=np.uint16).reshape(1024, 2048))
    assert thumb.dtype == np.uint8
    assert max(thumb.shape) <= imaging.THUMBNAIL_PX
    assert thumb.flags["C_CONTIGUOUS"]
    assert thumb.min() == 0 and thumb.max() == 255


def test_thumbnail_of_a_flat_or_tiny_image() -> None:
    assert thumbnail(np.full((50, 50), 7.0)).max() == 0
    assert thumbnail(np.ones((1, 1000))).shape[0] == 1


def test_core_snap_uses_the_current_camera() -> None:
    core = MagicMock()
    core.getNumberOfCameraChannels.return_value = 1
    core.getCameraDevice.return_value = "Camera-1"
    core.getImage.return_value = np.ones((4, 4))
    frames = core_snap(core)
    core.snapImage.assert_called_once()
    assert list(frames) == ["Camera-1"]


def test_core_snap_returns_every_camera_channel() -> None:
    core = MagicMock()
    core.getNumberOfCameraChannels.return_value = 2
    core.getCameraChannelName.side_effect = ["Camera-1", "Camera-2"]
    core.getImage.side_effect = [np.ones((4, 4)), np.zeros((4, 4))]
    assert list(core_snap(core)) == ["Camera-1", "Camera-2"]


def test_a_provider_replaces_the_core() -> None:
    core = MagicMock()
    set_snap_provider(lambda: {"Camera-2": np.ones((3, 3))})
    frames = snap_images(core)
    assert list(frames) == ["Camera-2"]
    core.snapImage.assert_not_called()
    set_snap_provider(None)
    core.getNumberOfCameraChannels.return_value = 1
    core.getCameraDevice.return_value = "Camera-1"
    snap_images(core)
    core.snapImage.assert_called_once()
