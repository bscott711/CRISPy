"""Tests for snapping, the sharpness score and the frame store (no Qt, no camera)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import numpy as np
import pytest

from CRISPy.imaging import (
    FrameStore,
    core_snap,
    focus_score,
    image_view_factory,
    set_image_view_factory,
    set_snap_provider,
    snap_images,
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
def _no_hooks():
    set_snap_provider(None)
    set_image_view_factory(None)
    yield
    set_snap_provider(None)
    set_image_view_factory(None)


def test_a_sharper_image_scores_higher() -> None:
    # Optics blur any real image over more than a pixel; from there, sharper is higher.
    scores = [focus_score(blurred(s)) for s in (1.0, 1.5, 2.5, 4.0)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 3 * scores[-1]


def test_the_score_ignores_brightness_and_the_camera_offset() -> None:
    img = blurred(1.5)
    assert focus_score(img * 7.0) == pytest.approx(focus_score(img))
    assert focus_score(img + 500.0) == pytest.approx(focus_score(img))


def _dim_noisy_scan(seed: int = 0) -> tuple[np.ndarray, list[np.ndarray]]:
    """Dim cells, camera noise as large as the structure, best focus at z = +1.

    Like the rig's piezo scan of 2026-10-20 (2400 px, mean ~20 counts above a ~100
    count offset), where a plain Brenner gradient moved 1% across the scan.
    """
    rng = np.random.default_rng(seed)
    size = 600
    f = np.fft.fftfreq(size)
    f2 = f[:, None] ** 2 + f[None, :] ** 2
    cells = np.real(
        np.fft.ifft2(
            np.fft.fft2(rng.random((size, size))) * np.exp(-2 * (np.pi * 4) ** 2 * f2)
        )
    )
    cells = (cells - cells.min()) / (cells.max() - cells.min()) * 40.0
    spectrum = np.fft.fft2(cells)
    zs = np.arange(-3.0, 3.01, 0.5)
    frames = []
    for z in zs:
        sigma = 1.0 + 3.0 * abs(z - 1.0)
        img = np.real(np.fft.ifft2(spectrum * np.exp(-2 * (np.pi * sigma) ** 2 * f2)))
        frames.append(img + 100.0 + rng.normal(0, 15.0, img.shape))
    return zs, frames


def test_the_score_finds_focus_in_dim_noisy_images() -> None:
    zs, frames = _dim_noisy_scan()
    scores = np.array([focus_score(f) for f in frames])
    assert zs[scores.argmax()] == pytest.approx(1.0, abs=0.5)
    assert scores.min() < 0.5 * scores.max()  # it moves, unlike a plain gradient

    def plain_brenner(a: np.ndarray) -> float:
        d = a[:, 2:] - a[:, :-2]
        return float(np.mean(d * d) / a.mean() ** 2)

    plain = np.array([plain_brenner(f) for f in frames])
    assert plain.min() > 0.9 * plain.max()  # what the panel showed on the rig


def test_degenerate_images_score_zero() -> None:
    assert focus_score(np.zeros((10, 10))) == 0.0
    assert focus_score(np.ones((2, 50))) == 0.0


def test_an_rgb_image_is_scored_in_grey() -> None:
    grey = blurred(1.0)
    rgb = np.stack([grey, grey, grey], axis=-1)
    assert focus_score(rgb) == pytest.approx(focus_score(grey))


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


def test_the_view_factory_hook() -> None:
    assert image_view_factory() is None

    def factory(camera: str) -> object:
        return camera

    set_image_view_factory(factory)
    assert image_view_factory() is factory


def test_frame_store_keeps_full_frames_on_disk() -> None:
    store = FrameStore(3)
    a = np.arange(12, dtype=np.uint16).reshape(3, 4)
    store.put(0, {"Camera-1": a, "Camera-2": a * 2})
    store.put(2, {"Camera-1": a + 1})
    assert store.cameras == ["Camera-1", "Camera-2"]
    directory = store.directory
    assert directory is not None
    frame = store.get(0, "Camera-1")
    np.testing.assert_array_equal(frame, a)
    assert frame.dtype == np.uint16
    np.testing.assert_array_equal(store.get(2, "Camera-1"), a + 1)
    assert store.get(1, "Camera-1") is None  # never put
    assert store.get(2, "Camera-2") is None
    assert store.get(0, "Camera-3") is None
    frame[:] = 0  # a copy: the store is unchanged
    np.testing.assert_array_equal(store.get(0, "Camera-1"), a)
    store.close()
    assert store.cameras == []
    assert not os.path.exists(directory)


def test_frame_store_ignores_odd_frames_and_indices() -> None:
    store = FrameStore(2)
    store.put(0, {"Camera-1": np.zeros((4, 4), np.uint16)})
    store.put(1, {"Camera-1": np.zeros((5, 5), np.uint16)})  # another shape
    store.put(5, {"Camera-1": np.zeros((4, 4), np.uint16)})  # past the capacity
    assert store.get(1, "Camera-1") is None
    assert store.get(0, "Camera-1") is not None
    store.close()
    store.close()  # twice is fine


def test_frame_store_files_go_when_the_store_does() -> None:
    import gc

    store = FrameStore(1)
    store.put(0, {"Camera-1": np.ones((4, 4))})
    directory = store.directory
    assert directory is not None and os.path.exists(directory)
    del store
    gc.collect()
    assert not os.path.exists(directory)
