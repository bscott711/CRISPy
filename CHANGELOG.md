# Changelog

All notable changes to CRISPy are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- The piezo axis panel is now the camera lock only (`CRISPy.piezo_panel.PiezoSpotPanel`):
  the hardware-CRISP Readouts, Operation, Calibration, Dither, Focus curve, Hold test and
  Parameters are gone from it, and the lock has its own Readouts / Operation / Calibration
  groups. The Z-stage axis keeps the unchanged hardware-CRISP panel. The collapsible
  section widget moved to `CRISPy._section`. `CRISPy.gating` lost its spot-lock
  arbitration (`spot_section_enabled`; `poll_may_run`, `tool_may_start` and
  `control_gate` take fewer arguments).
- The camera lock uses a single edge box. `FocusCalibration` holds one `edge` (was a list
  of `edges`), `fuse_edges` and `FusedError` are replaced by `loop_error` and `EdgeError`,
  `SpotFocusService.set_edge_boxes` is replaced by `set_edge_box`, and `calibrate` and
  `measure` take one box. The telemetry and `measure` reply lost their `edges`,
  `disagreement_um` and `held_edge` fields. The calibration `.npz` record keeps its
  array names with single-edge shapes (`e_start`, `e_end` and `noise_px` are now scalars).
- The Camera group's parameters (SDK folder, exposure, gain, channel, detector, ROI) and
  the loop Parameters are collapsible sections. The Camera parameters section remembers
  whether it was open.
- The spot lock lights the CRISP LED with the raw serial commands `LK F=90` (on) and
  `LK F=79` (off) after setting `LED Intensity`, instead of writing `CRISP State = Ready`
  (which from Idle did not light it). Lock, Calibrate, Zero here and Preview light it;
  it goes off when the last of them ends. CRISP stays in Idle. The panel's LED status
  reads "LED on" or "LED off". *Rig-unverified.*
- The spot lock disables the joystick wheel while the lock runs and restores it (from the
  `Z_P` preset) on Stop, on a fault, when the worker dies and at shutdown. Calibrate and
  Zero here do not touch it. *Rig-unverified.*

### Removed

- The second edge box and the edge fusion: the `edge_box_2` setting, the
  `max_edge_disagreement_um` and `hold_band_um` loop parameters, the disagreement and hold
  band logic, the Edge 2 and Disagreement readouts, and the second-box drawing in the live
  view. Old settings that still hold them load (the keys are ignored), and an old
  two-edge calibration loads with its first edge kept and a warning.
- The "Spot lock" section embedded in the hardware-CRISP panel, and the piezo panel's
  Joystick enabled checkbox.

## [0.2.0] - 2026-10-29

### Added

- The spot edge focus lock, moved here from pymmcore-gui (`CRISPy.spot`): a worker
  process that reads the AmScope camera and holds the piezo on the edge of the CRISP
  LED spot. The edge metric defaults to `gaussderiv`. It shows as a "Spot lock" section
  of the piezo panel only (one per process, see `spot_choice.py`); while it is starting,
  running, calibrating or zeroing, the panel's slow poll is paused, its controls that
  write to the Tiger are disabled and the CRISPy tools cannot start (rules in the
  Qt-free `CRISPy.gating`). A config reload rebuilds the panel and stops a running lock.
- `CRISPy.spot.store.set_settings_store()`: the host hook for the lock's settings
  (settings, save, data folder, piezo and CRISP label overrides). pymmcore-gui's
  `pmm_settings.json` key `spot_focus` loads unchanged.
- Unlocked Hold test (the actuator by itself, no CRISP lock).
- Objective NA field visibility in the Parameters box.
- Mouse-wheel guard on spin boxes and sliders (ignore the wheel unless focused).
- Dither scatter plot.
- Focus curve snaps images at every position (`CRISPy.imaging`: the host's
  `set_snap_provider()` / `set_image_view_factory()`), with a sharpness score and an
  image window to step through them.
- Inline Dither section in the panel.
- Per-axis LED control.
- `pydantic` and `psygnal` as declared dependencies.

## [0.1.0]

- First release: the CRISP control, calibration and telemetry panel with the Dither
  assistant, Focus curve and Hold test.
