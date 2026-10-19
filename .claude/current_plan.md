# Piezo CRISP noise: unlocked hold test, NA visibility, wheel guard

**Status: implemented; tests and a headless smoke test (in the pymmcore-gui host env)
pass. Not yet run on the rig** (branch `feat/crisp-unlocked-hold-and-na`).

## Evidence from the rig (2026-10-19 screenshots)

|                          | Z axis (objective NA 1.49)   | Piezo (moves the "snouty", NA 1.0; next to an NA 0.95 air lens) |
| ------------------------ | ---------------------------- | --------------------------------------------------------------- |
| Dither \|Err\|           | ~115, tight (~±4%)           | ~70, spread ±13%, smoothed level wandering ~55-90               |
| Cal Range                | 0.887 µm (= NA 1.30 setting) | 0.714 µm (= NA 1.45 setting)                                    |
| Cal Gain / "Sensitivity" | −1454 / 1639                 | 658 / 922                                                       |
| SNR, Sum                 | 6.0 dB, 64                   | 4.8 dB, 57                                                      |

- The dither move is `cal_range = 1.5 µm / NA²`. The piezo's lens (NA 1.0) wants 1.5 µm;
  it dithers 0.71 µm. The Z lens (1.49) wants 0.68 µm; it dithers 0.89 µm. The 2026-08
  dump had 0.675 / 0.677 (both NA 1.49), so both settings changed since.
- |Cal Gain| ≈ 10-13× the dither |Err| on both axes, so Cal Gain is not the dither swing
  (the earlier "20× weaker piezo" estimate assumed it was, and was wrong). The manual also
  says the dither Err "is actually different from the Err signal computed during
  operation", so only the focus curve measures counts/µm of the operating error.
- The piezo's smoothed |Err| wanders ±20% over seconds, ~5× more than white noise allows
  for a 1 s median, so part of its scatter is slow (piezo/drive, air path, or optics).

## Changes

1. Hold test also runs **unlocked** (Ready / Idle): the actuator is not driven by CRISP,
   so position = the piezo's own noise and error rms = the sensor noise. Table gets a
   Mode column. Unlocked runs end if the state moves into Lock, Dither or calibration.
1. Dither tab: scatter in counts (σ), and samples read after the dither ends are dropped
   (the Z trace fell to 0 and showed "settling…" after the dither stopped).
1. Readouts: Cal Range shows the NA it corresponds to; tooltip says to set Objective NA
   to the objective the axis moves, before calibrating.
1. Sensitivity: after a focus curve, the measured slope is used for the µm conversions
   (labelled), calibration-derived otherwise (labelled unverified). Cleared on Log Cal /
   Dither (the slope changes with AGC and alignment).
1. Wheel guard: parameter widgets ignore the mouse wheel unless focused (clicked), so
   scrolling the panel can't change Objective NA, LED, Loop Gain, Averages, ...
1. Demo, tests, docs.

## Verification done

- `uv run pytest -q`: 82 passed. Planted bugs in the unlocked-mode check and in the
  post-dither discard were both caught.
- `ruff check .` at the 19 findings `main` had; `ruff format`, `mdformat` clean.
- Smoke test of the real panel against the demo in the pymmcore-gui env (PyQt6 6.9, real
  `AsyncPoller`): unlocked hold test in Ready (marked unlocked, the demo piezo's own
  ~20 nm wander measured, writes nothing); post-dither readings dropped (final level 334,
  not 0); scatter in counts shown; Cal Range shows "(NA 1.49)"; the focus-curve slope is
  adopted and cleared by Dither; the wheel over an unfocused control scrolls the panel
  and leaves the value alone, and works after a click.
- Found while testing, not changed: exiting the process while a slow-poll read is in
  flight crashes at shutdown (0xC0000409). It's the existing `AsyncPoller` /
  thread-pool teardown, not this change; the smoke test now waits for the read.

## Addendum: first Z focus curve (2026-10-19)

The rig's Z curve (±10 µm, 0.25 µm steps) was a broad S, +50 to −55 counts, crossing at
−0.50 µm, slope ~8.6 counts/µm, error noise ±0.5 counts, with no peak inside the scan.
The panel's summary was misleading, so:

- The scan ends are no longer reported as peaks (whole-count readings tie at the end):
  "no peak within the scan", and the slope is fitted over a wider window then.
- One count of the error readout = 1000 / slope nm (117 nm here). Noise of about one
  count is reported as rounding (the readout's limit), not as the lock's noise floor.
- Cal Gain / Cal Range (2564) was ~300× the measured slope: an internal scale. It is no
  longer used for any µm conversion; only a measured slope is.
- Plot labels "start" / "lock point" no longer overlap.
- README anchors: headings with variation-selector emoji (🎛️, 🕹️) broke their links on
  GitHub; replaced with 🎯 / 🎮.

Open question for the bench: why the curve is so much broader than ASI's table suggests
for an NA ~1.4-1.49 objective (scan moved less than commanded? broad reflection?).

## Bench (user)

- Set Objective NA: piezo 1.0, Z 1.49 (Parameters box), then Log Cal → Dither → Set Gain.
- Dither |Err| and scatter in counts on both axes; Focus curve on both.
- Hold test unlocked (Ready) and locked on the piezo.
