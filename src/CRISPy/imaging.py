"""Camera images for the Focus curve: snapping, showing, keeping, and a sharpness score.

Two optional host hooks, for hosts whose core can't snap by itself or that have their
own image viewer (pymmcore-gui on the ASI rig sets both):

* a **snap provider** (:func:`set_snap_provider`): how to snap. pymmcore-gui hands its
  cameras to worker processes for the whole session and fires the lasers around each
  Snap, so it provides what its own Snap button does. Without one CRISPy snaps with the
  core's current camera.
* an **image view factory** (:func:`set_image_view_factory`): the widget that shows a
  camera's images, so they look like the host's preview windows. Without one the panel
  uses its own viewer that behaves the same way.

:class:`FrameStore` keeps a scan's full-resolution frames on disk for stepping through
them afterwards. :func:`focus_score` rates how sharp an image is; across a focus curve
its peak is where the image is in focus, to compare with where CRISP would lock.

Nothing here imports Qt.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import weakref
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pymmcore_plus import CMMCorePlus

    SnapProvider = Callable[[], Mapping[str, np.ndarray]]
    ImageViewFactory = Callable[[str], Any]

# focus_score bins an image to about this many pixels on its short side.
SCORE_SIDE_PX = 300

_provider: SnapProvider | None = None
_view_factory: ImageViewFactory | None = None


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


def set_image_view_factory(factory: ImageViewFactory | None) -> None:
    """Set the widget that shows a camera's images, or use CRISPy's own with ``None``.

    Parameters
    ----------
    factory
        Called on the GUI thread with a camera label; returns a QWidget with an
        ``append(image)`` method that shows one full image, e.g. the widget of the
        host's preview windows, so the Focus curve's images look the same.
    """
    global _view_factory
    _view_factory = factory


def image_view_factory() -> ImageViewFactory | None:
    """The factory set with :func:`set_image_view_factory`, if any."""
    return _view_factory


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


def _binned(a: np.ndarray, b: int) -> np.ndarray:
    if b <= 1:
        return a
    h, w = (a.shape[0] // b) * b, (a.shape[1] // b) * b
    return a[:h, :w].reshape(h // b, b, w // b, b).mean(axis=(1, 3))


def _mean_sq_diff(a: np.ndarray, lag: int) -> float:
    dx = a[:, lag:] - a[:, :-lag]
    dy = a[lag:, :] - a[:-lag, :]
    return float(np.mean(dx * dx) + np.mean(dy * dy))


def focus_score(img: np.ndarray) -> float:
    """How sharp an image is: higher is sharper (0 for no usable structure).

    Built for dim fluorescence images whose camera noise is as large as the structure
    (the rig's 2026-10-20 piezo scan: mean ~20 counts above a ~100 count offset, where a
    plain Brenner gradient moved by 1% across the whole scan):

    1. Bin the image so its short side is about ``SCORE_SIDE_PX`` (8×8 on a 2400 px
       sensor): defocus shows at that scale, and binning averages the noise down.
    2. Gradient energy with the noise cancelled: the mean squared difference between
       pixels two apart minus that between neighbours. Pixel noise is uncorrelated, so
       it adds the same to both and drops out; structure, blurred by the optics over
       more than a pixel, differs more over two pixels than over one.
    3. Divide by the signal squared: the mean above the 1st percentile (the camera's
       dark offset), so brightness, bleaching and the offset don't change the score.

    Defocus spreads the light without changing its total, so across a scan the score
    peaks where the image is in focus.

    Parameters
    ----------
    img
        A 2-D image (an RGB image is averaged over its colours first).
    """
    a = _grey(img)
    if a.ndim != 2 or min(a.shape) < 3:
        return 0.0
    a = _binned(a, max(1, round(min(a.shape) / SCORE_SIDE_PX)))
    if min(a.shape) < 3:
        return 0.0
    signal = float(a.mean() - np.percentile(a, 1))
    if signal <= 0:
        return 0.0
    energy = _mean_sq_diff(a, 2) - _mean_sq_diff(a, 1)
    return max(0.0, energy / (signal * signal))


class FrameStore:
    """A scan's full-resolution frames, kept on disk until :meth:`close`.

    One memory-mapped ``.npy`` file per camera in a temporary directory, so a long scan
    of large frames doesn't fill memory. Each camera's file holds *capacity* frames of
    the shape and dtype of its first frame; a later frame of another shape isn't kept.
    Safe to fill from one thread while another reads.

    Parameters
    ----------
    capacity
        The most frames per camera (the scan's number of positions).
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._s = _StoreState()
        # Delete the files however the store goes: close(), garbage collection (the
        # panel deleted without a close event), or the interpreter exiting.
        self._finalizer = weakref.finalize(self, self._s.clear)

    @property
    def cameras(self) -> list[str]:
        """The cameras with at least one frame, in the order they first appeared."""
        with self._s.lock:
            return list(self._s.arrays)

    @property
    def directory(self) -> str | None:
        """Where the files are, once there is one."""
        return self._s.directory

    def put(self, index: int, frames: Mapping[str, np.ndarray]) -> None:
        """Keep *frames* (``{camera: image}``) as frame *index*."""
        if not 0 <= index < self.capacity:
            return
        s = self._s
        with s.lock:
            for camera, img in frames.items():
                a = np.asarray(img)
                arr = s.arrays.get(camera)
                if arr is None:
                    if s.directory is None:
                        s.directory = tempfile.mkdtemp(prefix="crispy-curve-")
                    path = os.path.join(s.directory, f"camera{len(s.arrays)}.npy")
                    arr = np.lib.format.open_memmap(
                        path, mode="w+", dtype=a.dtype, shape=(self.capacity, *a.shape)
                    )
                    s.arrays[camera] = arr
                    s.filled[camera] = set()
                if arr.shape[1:] != a.shape or arr.dtype != a.dtype:
                    continue
                arr[index] = a
                s.filled[camera].add(index)

    def get(self, index: int, camera: str) -> np.ndarray | None:
        """A copy of frame *index* of *camera* (``None`` if it wasn't kept).

        A copy, not a view of the file, so nothing outside keeps the file open.
        """
        s = self._s
        with s.lock:
            arr = s.arrays.get(camera)
            if arr is None or index not in s.filled[camera]:
                return None
            return np.array(arr[index])

    def close(self) -> None:
        """Forget the frames and delete the files."""
        self._finalizer()


class _StoreState:
    """A :class:`FrameStore`'s data, apart from it so its finalizer can clear it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.directory: str | None = None
        self.arrays: dict[str, np.ndarray] = {}
        self.filled: dict[str, set[int]] = {}

    def clear(self) -> None:
        with self.lock:
            arrays, self.arrays, self.filled = self.arrays, {}, {}
            directory, self.directory = self.directory, None
        for arr in arrays.values():
            mm = getattr(arr, "_mmap", None)  # Windows can't delete an open mapping
            if mm is not None:
                with suppress(Exception):
                    mm.close()
        arrays.clear()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
