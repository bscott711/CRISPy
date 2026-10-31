# Plan: piezo spot-lock widget (PR 1 and PR 2)

Goal: the piezo axis that carries the spot lock gets a dedicated `PiezoSpotWidget`
(`src/CRISPy/piezo_ui.py`); the CRISP detector UI no longer appears on it. The stage
axis (and any non-spot piezo) keeps `CrispControlPanel` unchanged. Function first: no
styling (the user styles later).

## Status
- PR 1 (new widget, relayout, single-edge UI, `piezo_status.py` + tests, demo): in review
- PR 2 (dead-code cleanup of the old spot integration in `ui.py`, `gating.py`, leftovers): NOT started
- Step 2 (live re-zero while locked, loop pause): NOT started (needs its own design note)
- Optional removal of the Qt-free two-edge core (`edge_box_2`, fusion): NOT started

## PR 1 contents
- `piezo_status.py` (Qt-free): status words (Lock, Camera, LED, Edge box, Calibration),
  one worst-first message, tracking-error text, `joystick_for_lock`; `tests/test_piezo_status.py`.
- `spot/panel.py`, `spot/live_view.py`: one vertical layout (controls, offset/tracking error,
  message line, live image, toolbars, four exclusive sections: Camera, Calibration settings,
  Diagnostics, Advanced); single edge only (saved `edge_box_2` is ignored, never deleted);
  `status_inputs()` / `statusChanged`; `_on_started` no longer flickers `lock_active`.
- `piezo_ui.py`: header, status words, panel, Joystick checkbox (off while the lock is active,
  restored on stop and on destroy); `plugin.py` builds it for the `choose_spot_index` piezo
  and gives a two-axis splitter equal halves.
- Deviations from wireframe v3: Joystick checkbox on its own line under the control row;
  "Display settings" at the right end of the "Tracking edge" row (both to fit 460 px at 100 %).

## PR 2 (approved list A)
Delete the `spot_lock=` integration in `ui.py`, `gating.py` spot params and their tests,
edge-2 code and mirror signals in panel/live_view, hidden leftovers; update
`run_demo.py`, `docs/spot_focus_lock.md`, `CLAUDE.md`. The stage panel dump must stay identical.
Keep the Qt-free two-edge core, `edge_box_2` setting, all other tests.
