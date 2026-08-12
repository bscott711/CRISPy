# Software joystick lockout during CRISP lock

**Status: implemented** (discovery/controller/ui changes done, ruff clean,
verified against a throwaway fake-Core smoke test; not yet bench-verified
against real hardware -- see "ASI command" below).

## Context

Root cause of the "silent drift while CRISP reports healthy" bug (see the
pymmcore-gui-side investigation) turned out to be the piezo's joystick wheel
physically drifting/creeping, which CRISP's servo was continuously fighting
(explains position drifting steadily while `Dither Error` stayed near zero)
until it exceeded `Max Lock Range` and auto-unlocked. A physical switch
already exists to cut the wheel off, but the user wants a software-side
lockout as a redundant safety net, in case the switch is left on or bumped.

Agreed behavior (confirmed with user):
- **Default**: joystick/wheel input for a CRISP axis's actuator is
  automatically disabled the moment that axis locks, and re-enabled the
  moment it unlocks.
- **Manual override**: a checkbox in the panel lets the user re-enable (or
  re-disable) the joystick at any time, including while locked, for
  intentional manual nudging. The auto-behavior only fires again at the next
  lock/unlock *transition* — it must not immediately re-fight a manual
  override while state is unchanged.

## ASI command

`JOYSTICK (J)` command (https://asiimaging.com/docs/commands/joystick):
`J <axis>+` enables, `J <axis>-` disables manual input for that axis letter.
Documented as genuinely axis-letter-generic (`J X+ Y+ Z-` is ASI's own
example) — unlike the `PZ Z-`/`PZ Z+` piezo-drive-card quirk on this same rig
where `Z` turned out to be a fixed literal token, not an axis placeholder.
Still needs one bench confirmation (`J P-` then `J P?` then `J P+`) before
trusting it fully, per this project's standing rule of not assuming ASI
serial syntax works untested.

## Design (respecting CRISPy's architectural boundaries — go through Core,
## stay host-agnostic, don't import pymmcore-gui internals)

- **`discovery.py`**: add `find_tiger_hub(mmcore) -> str | None`, searching
  `getLoadedDevicesOfType(DeviceType.HubDevice)` filtered to the existing
  `_ASI_LIBRARY` ("ASITiger") constant — mirrors the filtering pattern
  already used in `classify_focus_actuator`. This replaces needing
  pymmcore-gui's hardcoded `_HW.tiger_comm_hub_label`.
- **`controller.py`** (`ASICrispController`):
  - Resolve `self._axis_letter` (read the `"AxisLetter"` property, same one
    `classify_focus_actuator` already reads) and `self._hub_label`
    (`find_tiger_hub`) once in `__init__`, alongside the existing
    `self._actuator_type` resolution.
  - Add `_send_serial(cmd) -> str | None`: writes `SerialCommand` /reads
    `SerialResponse` on the hub device — the same mechanism pymmcore-gui's
    `_send_tiger_command` uses, reimplemented locally so CRISPy stays
    dependency-free of pymmcore-gui internals. This still goes through
    `CMMCorePlus`, satisfying "go through the Core."
  - Add `get_axis_letter() -> str | None` and
    `set_joystick_enabled(enabled: bool) -> None` (sends `J <axis>+`/`-`;
    no-ops silently if the axis letter or hub couldn't be resolved, matching
    this class's existing silent-failure style for missing capabilities).
  - Add both new methods to the `CrispController` Protocol.
- **`ui.py`** (`CrispControlPanel`):
  - Add a `"Joystick enabled"` checkbox (Operation box), default checked.
  - Track `self._was_locked: bool` (init `False`). In `_update_state`,
    detect a `locked` edge (`state in _LOCKED_STATES` changing value);
    only on the edge, call `controller.set_joystick_enabled(not locked)`
    and sync the checkbox (`blockSignals` while doing so, to avoid
    re-entrant toggling).
  - Checkbox `toggled` → `controller.set_joystick_enabled(checked)`
    directly, independent of lock state — this is the manual override, and
    since the auto-logic only fires on transition edges it won't stomp on
    it until the next lock/unlock.
  - No hardware query of current joystick binding on load (the `J <axis>?`
    response format — device codes — isn't bench-verified yet); the
    checkbox is the single source of truth for what we last told the
    hardware, seeded to the safe default and corrected at the first
    lock-state edge observed after the panel opens.

## Not doing

- Not adding a general drift watchdog or loosening `Max Lock Range` — out of
  scope; this is specifically the joystick-lockout ask.
- Not adding new tests — CRISPy currently has no `tests/` dir or pytest dev
  dependency; matching existing repo state rather than introducing test
  infra as a side effect of this change.

## Verification

- `uv run ruff check .` / `uv run ruff format --check .` (`just fix` to
  apply) from the CRISPy repo root.
- Manual bench check: lock the piezo axis, confirm the wheel stops moving
  the piezo and the checkbox unchecks itself; toggle it back on while still
  locked and confirm the wheel works again; unlock and confirm it
  re-enables automatically.
