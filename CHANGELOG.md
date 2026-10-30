# Changelog

All notable changes to CRISPy are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
