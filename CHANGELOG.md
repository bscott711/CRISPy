# Changelog

All notable changes to CRISPy are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- The spot lock keeps living in the piezo axis's `PiezoSpotWidget`, which has no
  hardware-CRISP poller, tools or controls, so `CRISPy.gating` lost its spot-lock
  arbitration (`spot_section_enabled`; `poll_may_run`, `tool_may_start` and
  `control_gate` take fewer arguments). The collapsible section widget `ui.py` uses moved
  to `CRISPy._section`.
- The camera lock uses a single edge box end to end. `FocusCalibration` holds one `edge`
  (was a list of `edges`) and one `box`, `fuse_edges` and `FusedError` are replaced by
  `loop_error` and `EdgeError`, `SpotFocusService.set_edge_boxes` is replaced by
  `set_edge_box`, and `calibrate` and `measure` take one box. The telemetry and `measure`
  reply lost their `edges`, `disagreement_um` and `held_edge` fields. The calibration
  `.npz` record keeps its array names with single-edge shapes (`e_start`, `e_end` and
  `noise_px` are now scalars).
- The spot lock lights the CRISP LED with the raw serial commands `LK F=90` (on) and
  `LK F=79` (off) after setting `LED Intensity`, instead of writing `CRISP State = Ready`
  (which from Idle did not light it). Lock, Calibrate, Zero here, Preview and the LED
  button light it; it goes off when the last of them ends. CRISP stays in Idle. The
  service has new `led_changed(bool)` and `led_failed(str)` signals that feed the
  widget's LED status word, which shows `LED: Error` when a raw command fails.
  *Rig-unverified.*
- The spot service owns the joystick lockout: it disables the wheel while the lock runs
  and restores it (from the `Z_P` preset) on Stop, on a fault, when the worker dies and
  at shutdown. Calibrate, Zero here and Preview do not touch it. The widget's Joystick
  checkbox shows that state and stays a manual override (a direct write); the widget's
  own enable/restore logic and `piezo_status.joystick_for_lock` are gone.
  *Rig-unverified.*

### Removed

- The second edge box and the edge fusion: the `edge_box_2` setting, the
  `max_edge_disagreement_um` and `hold_band_um` loop parameters, the disagreement and hold
  band logic, the Edge 2 and Disagreement readouts, and the second-box drawing in the live
  view. Old settings that still hold them load (the keys are ignored) and an old
  two-edge calibration loads with its first edge kept and an "edge 2 dropped" warning;
  the next save no longer writes `edge_box_2`.

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
