"""Camera images for the Focus curve: snapping, a sharpness score and thumbnails.

Snapping goes through a **snap provider** when the host has set one with
:func:`set_snap_provider`, otherwise through the core's current camera. A host needs
the hook when the core can't snap by itself: pymmcore-gui on the ASI rig hands its
cameras to worker processes for the whole session and fires the lasers around each
Snap, so it registers a provider that does what its own Snap button does. CRISPy calls
the provider on the GUI thread, like a Snap button would.

:func:`focus_score` rates how sharp an image is; across a focus curve its peak is where
the image is in focus, to compare with where CRISP would lock. :func:`thumbnail` makes a
small 8-bit copy for display.

Nothing here imports Qt.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pymmcore_plus import CMMCorePlus

    SnapProvider = Callable[[], Mapping[str, np.ndarray]]

THUMBNAIL_PX = 256

_provider: SnapProvider | None = None


def set_snap_provider(provider: SnapProvider | None) -> None:
    """Set how CRISPy snaps images, or go back to the core with ``None``.

    Parameters
    ----------
    provider
        Called on the GUI thread with no arguments; returns one image per camera as
        ``{camera label: 2-D (or RGB) array}``. It should do whatever the host's own
        Snap does (illumination, cameras owned elsewhere) and raise if it can't snap.
    """
    global _provider
    _provider = provider


def snap_provider() -> SnapProvider | None:
    """The provider set with :func:`set_snap_provider`, if any."""
    return _provider


def core_snap(mmcore: CMMCorePlus) -> dict[str, np.ndarray]:
    """Snap with the core's current camera: one image per camera channel.

    Parameters
    ----------
    mmcore
        The core to snap with.
    """
    mmcore.snapImage()
    n = int(mmcore.getNumberOfCameraChannels())
    if n > 1:
        return {
            str(mmcore.getCameraChannelName(i)): np.asarray(mmcore.getImage(i))
            for i in range(n)
        }
    return {str(mmcore.getCameraDevice() or "camera"): np.asarray(mmcore.getImage())}


def snap_images(mmcore: CMMCorePlus) -> dict[str, np.ndarray]:
    """Snap through the host's provider if it set one, otherwise through the core.

    Parameters
    ----------
    mmcore
        The core, used when no provider is set.
    """
    provider = _provider
    if provider is not None:
        return {str(k): np.asarray(v) for k, v in provider().items()}
    return core_snap(mmcore)


def _grey(img: np.ndarray) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim == 3:
        a = a[..., :3].mean(axis=-1)  # RGB(A): the mean of the colour channels
    return a.astype(np.float64, copy=False)


def focus_score(img: np.ndarray) -> float:
    """How sharp an image is: higher is sharper.

    The Brenner gradient (mean squared difference between pixels two apart, along both
    axes) divided by the mean intensity squared, so scaling the brightness doesn't
    change it. Defocus spreads the light without changing its total, so across a scan
    the score peaks where the image is in focus.

    Parameters
    ----------
    img
        A 2-D image (an RGB image is averaged over its colours first).
    """
    a = _grey(img)
    if a.ndim != 2 or a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    mean = float(a.mean())
    if mean <= 0:
        return 0.0
    dx = a[:, 2:] - a[:, :-2]
    dy = a[2:, :] - a[:-2, :]
    return float((np.mean(dx * dx) + np.mean(dy * dy)) / (mean * mean))


def thumbnail(img: np.ndarray, max_px: int = THUMBNAIL_PX) -> np.ndarray:
    """A small 8-bit copy of *img* for display.

    Block-averaged to at most *max_px* a side, then stretched between its 0.5 and 99.5
    percentiles (each image on its own, so structure shows at any brightness).

    Parameters
    ----------
    img
        A 2-D image (an RGB image is averaged over its colours first).
    max_px
        The longest side of the result.
    """
    a = _grey(img)
    if a.ndim != 2 or a.size == 0:
        return np.zeros((1, 1), np.uint8)
    h, w = a.shape
    f = max(1, min(math.ceil(max(h, w) / max_px), h, w))
    h2, w2 = (h // f) * f, (w // f) * f
    a = a[:h2, :w2].reshape(h2 // f, f, w2 // f, f).mean(axis=(1, 3))
    lo, hi = np.percentile(a, (0.5, 99.5))
    if hi <= lo:
        return np.zeros(a.shape, np.uint8)
    out = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(out)
