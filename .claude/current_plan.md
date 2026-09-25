# CRISP lock-offset setpoint methods

**Status: implemented, tests pass. Consumed by pymmcore-gui's per-channel
focus-offset feature (chromatic focal-shift correction) — that side is still
gated on a bench probe of whether the setpoint actually moves focus.**

## Why

Different excitation wavelengths focus at slightly different axial planes. To
hold a per-wavelength corrected plane while CRISP stays locked, the servo's
lock-offset setpoint is shifted (moving the actuator directly just gets
servoed back). CRISPy owns the CRISP domain, so the low-level lever lives
here; the per-wavelength mapping, settings, and UI live in pymmcore-gui.

## What was added (`src/CRISPy/controller.py`)

Constants: `LOCK_OFFSET_PROP = "Lock Offset"` (read-only, servo's tracked
offset), `SET_LOCK_OFFSET_PROP = "Set Lock Offset (Advanced Users Only)"`
(writable int target). Both in the same controller counts as the error signal.

`ASICrispController` (`# --- lock offset ---` section) + `CrispController`
Protocol:

- `get_lock_offset_counts()` — read-only tracked offset, or `None`.
- `get_set_lock_offset_counts()` — read back the *writable* setpoint (compare
  with the above to catch an adapter cache that diverged from the hardware —
  the failure mode from the raw-serial `J` joystick saga).
- `set_lock_offset_counts(counts)` — **absolute** setpoint write (rounds to
  int). Caller owns settle + verify.
- `set_lock_offset_um(um, counts_per_um=None)` — µm → counts via
  `counts_per_um` or the device sensitivity; `RuntimeError` (writes nothing)
  if neither is available.
- `get_lock_offset_um(counts_per_um=None)`.
- `_resolve_counts_per_um(counts_per_um)` — shared helper; explicit value else
  `get_sensitivity_counts_per_um()` (`|Calibration Gain| / Calibration Range(um)`), raising if unavailable.

Host-agnostic (all I/O through the Core; no pymmcore-gui imports), per CRISPy's
boundary rules.

## Test infra (new)

CRISPy had no test suite. Added `pytest>=8` dev dep + `[tool.pytest.ini_options] testpaths = ["tests"]` to `pyproject.toml`, and
`tests/test_controller_lock_offset.py` — a `MagicMock` core (no Qt, no
hardware): write/round behaviour, read-only vs writable prop keyed correctly,
µm↔counts via sensitivity and via explicit scale, missing-calibration raises
and writes nothing.

## Verification

- `uv run pytest -q` (9 pass).
- `uv run ruff check .` / `uv run ruff format --check .` (`just fix` to apply).
- Bench (on the pymmcore-gui side): `probe_lock_offset_response()` in
  `pymmcore_gui.asi_z_stack.crisp_focus_offset_tuning` establishes whether the
  setpoint moves focus, its real counts/µm and sign, and the settle time.
