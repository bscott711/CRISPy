# Focus curve: snap an image at each position

**Status: implemented; tests and a demo scan through the real panel pass. Not yet run
on the rig** (CRISPy branch `feat/crisp-unlocked-hold-and-na`, PR #3; host change on
pymmcore-gui branch `feat/crispy-snap-provider`).

Done and checked: 137 CRISPy tests (scores per position, sharpest position inside / at
the end, a failed snap returns the actuator, CSV column, the score falls with blur and
ignores brightness, thumbnails, the provider replaces the core); 4 host tests (service
path with lasers on/off, core path stops Live like the toolbar Snap, lasers off when
the snap fails, register/clear). Demo scan ±3 µm at 0.1 µm: 53 snaps, all on the GUI
thread; sharpest image at +0.41 µm (simulated +0.4 from focus). Host `prek`: ruff,
mypy pass; pyright reports 3 errors in `_rebuild` that `main` already has.

## Why

The user wants to see whether the image goes in or out of focus as the focus curve
steps: the error curve says where CRISP would lock, the images say where the image is
actually sharp. Together they give the offset between CRISP's lock point and the
image's best focus.

## Constraint found

On the rig, pymmcore-gui hands Camera-1/Camera-2 to worker processes for the whole
session (`asi_z_stack/camera_handoff.py`): the main process's core has no camera, so
`mmcore.snapImage()` fails. The host's Snap goes through
`CameraWorkerService.get_active().snap()` (returns `{camera: image}`, stops Live
itself, meant for the GUI thread) bracketed by `open_selected_lasers()` /
`close_selected_lasers()`. CRISPy must not import pymmcore-gui ("stay a plugin").

## Design

1. `CRISPy/imaging.py` (Qt-free): `set_snap_provider(fn | None)`, a host hook returning
   `{camera: image}`; `snap_images(mmcore)` uses it, else `core_snap(mmcore)` (current
   camera, one image per camera channel). `focus_score(img)` (Brenner gradient / mean²,
   higher is sharper, scale-invariant), `thumbnail(img)` (≤ 256 px, 8-bit, percentile
   stretch; RGB → grey).
1. `focus_curve.py`: `CurvePoint.scores` (per camera); `run_focus_curve(snap=..., on_images=...)` snaps after reading the error at each position, scores and
   thumbnails on the worker thread; a snap failure ends the scan like a failed move (the
   actuator is returned). `sharpest_position(points, camera)` (parabola through the
   peak; flags a peak at the scan end). CSV gets a sharpness column per camera.
1. `ui.py`: "Snap an image at each position" checkbox; snaps run on the GUI thread
   (`_GuiCall`: the worker asks and waits), the processing on the worker. Image view
   with a position slider and camera selector, a sharpness plot, a "sharpest image"
   line on the curve, and a summary line (sharpest z, distance from the lock point).
   Refuse to start with the core path while Live runs.
1. `run_demo.py`: a simulated camera whose image blurs with the piezo's and Z's defocus
   (best image 0.4 µm from the lock point, to show the offset).
1. Host (pymmcore-gui, its own branch + PR): `CrispWidget` registers a provider: lasers
   on → `svc.snap()` (or CRISPy's `core_snap` without the service) → lasers off.
1. Tests, docs (README, CLAUDE.md: the hook and why).

## Verification

- pytest, ruff (19-finding baseline), mdformat; host tests that touch the CRISP widget.
- Demo scan through the real panel with snapping: images shown, sharpness peak found
  near the simulated best focus, summary and CSV.

## Addendum: show the images like the Preview windows

The user: "The little binned image with whatever autoscaling you cooked up is not it. We
should be using the same style of display that the Preview windows show." On the rig
those are pymmcore-gui's `PygfxPreview` docks: a GPU canvas, full resolution, grey LUT,
contrast auto = the frame's min to max, wheel zoom and drag pan.

- `imaging.set_image_view_factory(fn)`: a second host hook. `fn(camera)` returns a QWidget
  with `append(image)`; pymmcore-gui returns its `PygfxPreview`. Without one, CRISPy's
  `_FrameView` (QGraphicsView) does the same: full resolution, grey, min-max, zoom/pan.
- The images get their own window (one view per camera, position slider, caption): a
  native GPU canvas inside the panel's scroll area wouldn't clip when scrolled. The
  panel keeps the camera selector, the sharpness plot and a "Show images" button.
- `imaging.FrameStore`: the scan's full-resolution frames in one memory-mapped `.npy`
  per camera in a temp dir (81 positions × 2 cameras × 8-20 MB won't fit comfortably in
  memory); copies out, deleted on the next scan and when the panel closes.
- `run_focus_curve(on_images=...)` now passes the full frames; `thumbnail()` goes.

Done and checked: 139 CRISPy tests (FrameStore keeps full frames and copies out, skips
odd shapes and indices, deletes its files on close and when collected; the view hook);
demo scan through the real panel: the window follows the scan and ends on the sharpest
image, frames full size, a host view replaces CRISPy's own, files deleted on the next
scan and on close. pymmcore-gui's real `PygfxPreview` rendered inside the window (grey,
min-max contrast). Host: 6 tests, full suite 288 passed / 2 skipped; ruff, mypy pass,
pyright only the 3 errors `main` has.
