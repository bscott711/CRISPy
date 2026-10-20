# Focus curve: respect the actuator's travel; drop the "lopsided" verdict

**Status: implemented; tests and a demo scan through the real panel pass. Not yet run
on the rig** (branch `feat/crisp-unlocked-hold-and-na`, PR #3).

## Why

- The user: the piezo (150 µm travel) is not centred at focus, so ±30 µm may not fit on
  one side. `run_focus_curve` recorded the *commanded* offset, so a piezo clamped at its
  limit would plot as a flat stretch of error at made-up positions. The rig's piezo
  curve (2026-10-19) is flat at ~+15 from about −2.5 µm down to −30 µm, which is what a
  piezo stuck ~2.5 µm above its lower limit would look like. Unconfirmed: needs the
  piezo position and its `LowerLim(mm)`.
- ASI's docs (MOVE, SETLOW) don't say whether a move past a limit is clamped or
  rejected, so the scan can't rely on an error.
- "Lopsided" (b43d243) called a one-sided curve a defect. It isn't, for holding: what
  holds focus is the slope through zero and the noise there. A weak side only slows the
  recovery from a big excursion that way. And the weak side we saw may be the travel
  limit. Remove it.
- The piezo's reference reflection is a mirror after the air objective (O2), back
  through both objectives: CRISP reads the lateral position of the returned beam
  (manual: split photodiode), so tilt of that mirror or beam wander in that path reads
  as focus error. Docs only (a lead for the bench, not code).

## Changes

1. `focus_curve.py`
   - `travel_limits_um(mmcore, actuator, z0)`: the ASI `LowerLim(mm)` / `UpperLim(mm)`
     properties ×1000; ignored unless they bracket the start position.
   - `run_focus_curve` drops offsets outside the limits; after each move reads the
     position back; a position the actuator didn't reach (> max(0.2 µm, step/2) away,
     re-checked once after a short wait) is skipped (upward: stop, further is worse).
     Points use the measured position.
   - `CurveScan` gains `limits_um`, `limited_low/high`, `skipped`.
   - Remove `lopsided`, `restoring_low/high`, `_restoring`, `LOPSIDED_RATIO`.
   - CSV metadata: limits, skipped.
1. `ui.py`: summary line when the scan was cut short by travel (how far it got, the
   limit, where the start sits); no "Scan wider" on a side that hit the limit; confirm
   dialog says it stops at the travel limit. Remove the Lopsided line.
1. `run_demo.py`: stage travel limits (piezo focus 2.2 µm above its lower limit, as
   suspected on the rig), clamped moves, `LowerLim(mm)` / `UpperLim(mm)` properties.
1. Tests: clipping by limits; insane limits ignored; clamped (unknown-limit) positions
   skipped and measured z recorded; upward stop; lopsided tests removed.
1. Docs: CLAUDE.md (focus curve travel, piezo curve finding rewritten, mirror path),
   README focus-curve section.

## Verification

- pytest 92 passed; ruff at the 19-finding baseline; ruff format and mdformat clean.
- A ±10 µm scan through the real panel on the demo piezo (start −19.8 µm, limits −22 …
  128 µm): commanded nothing past −22 µm, covered −2.0 … +10 µm, "Travel limit" line
  and red plot marker shown, no "Scan wider"; CSV records the limits.
- Plot marks the cut with a red "travel limit" line.

## Addendum: noisy curves and the hold-test error trend (2026-10-20)

Rig: the piezo curve (±10 µm, 0.1 µm steps) is a clean straight line, but the summary
said slope 0, a peak 0.1 µm away and capture "--": the line moves 1.6 counts per step,
the noise is ±3. The Z summary said "can't show focus noise below ≈ 0 nm" (reads at a
position all equal). The locked piezo hold test ramped +1 µm/min; the error rms wasn't
detrended, and there was no error trend to compare an unlocked run with.

- `analyze_focus_curve`: running mean (±0.4 µm) for decisions when the noise (median
  2nd difference) is above rounding and ≥ the per-step change; chain-merged crossings
  (lock point = middle); direction from a local fit; slope window ≥ the smoothing. New
  `scatter_counts` / `scatter_nm` (rms about a local cubic).
- Summary noise line: scatter between positions in counts and nm; ≤ 1 count says one
  count = N nm (no more "≈ 0 nm").
- `analyze_hold`: error rms about its trend, `error_drift_per_min`; the unlocked summary
  says what a lock would follow (nm/min); a locked drift ≥ 200 nm/min gets the "sharp
  image = real drift, blurring = reference drift" note. Table: measurements before
  settings, so they show without scrolling; new Err /min column.

Verification: 124 tests pass (20 seeds of a rig-like noisy line, 10 of a noisy S, the
error-ramp hold case; all earlier tests unchanged); ruff at the baseline. Rendered
through the real panel with curves and hold runs traced from the screenshots: piezo
slope −16.1, no peaks, capture ≥ ±10 µm, scatter ±2.9 counts ≈ 179 nm; Z noise line
"one count = 80 nm"; unlocked ramp → "a lock would move the actuator ≈ +981 nm/min".
