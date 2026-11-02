# Plan: raw LED and joystick lock on the PiezoSpotWidget layout

Goal: keep `main`'s `PiezoSpotWidget` layout (status words, one message line, four
exclusive sections, Joystick checkbox) and bring in the behaviour of
`feat/piezo-camera-only` (761f5be, a5b59a8): raw `LK F=90` / `LK F=79` LED through
`ASICrispController.set_led`, the service-owned joystick lock, and the single edge box
end to end. Branch: `feat/piezo-led-joystick-on-ui-polish`, a merge of
`feat/piezo-camera-only` into `main`.

## Decisions

1. Spot core (`spot_focus.py`, `spot_focus_service.py`, `spot_focus_worker.py`,
   `settings.py`) and its tests come from camera-only. Single edge box:
   `FocusCalibration.edge/.box`, `loop_error` replaces `fuse_edges`.
2. UI files (`plugin.py`, `spot/live_view.py`, `spot/panel.py`, `ui.py`, `gating.py`,
   `spot_choice.py`, `tests/test_gating.py`) come from main; `spot/panel.py` is adapted to
   the single-edge core and gains a `controller` kwarg passed to the service.
3. `SpotFocusService` is the only owner of the joystick lockout: off while the lock
   runs, restored (Z_P preset) on stop, fault, worker death and shutdown. The Joystick
   checkbox shows that state and stays a manual override. `_WheelGuard` in `piezo_ui.py`
   and `piezo_status.joystick_for_lock` are removed.
4. New service signals `led_changed(bool)` / `led_failed(str)` drive the LED status
   word, so it updates during Calibrate/Zero and can show `LED: Error`.
5. `piezo_panel.py` (`PiezoSpotPanel`) is dropped as redundant; `_section.py` is adopted.
6. Old settings and two-edge calibrations load (first edge kept, with a warning); the
   next save no longer writes `edge_box_2`.
7. The reduced golden set from camera-only is kept, not regenerated.

## Status

- Merge resolved, tests pass apart from the timing test
  `test_no_torn_read_under_a_concurrent_writer_thread`, which also fails on `main`.
- LED and joystick behaviour unverified on rig.

## Rig checklist

- LED lights on Lock, Calibrate, Zero, Preview and the LED button; goes dark when the
  last of them ends; the LED word follows.
- Joystick wheel (right = piezo) is dead while the lock runs and works again after Stop,
  after a fault, and after closing the app; Calibrate/Zero/Preview leave it alone.
