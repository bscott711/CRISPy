# CRISP lock diagnostics: Dither assistant, focus curve, hold test

**Status: implemented; unit tests and a headless smoke test pass. Not yet run on the
rig** (branch `feat/crisp-lock-diagnostics`).

## Problem

The focus lock is not held still: values fluctuate while everything sits there, and focus
noise on the second timescale is larger than it should be. Separately, while turning the
detector knob during Dither, the panel was too slow to show the maximum.

## What the ASI manual says (docs.asiimaging.com/crisp_manual)

- The knob is the **detector lateral adjustment screw**, turned in the **Dither** state for
  the maximum **absolute** dither Err ("Large negative numbers are just as good"). Targets:
  > 50 (manual), at least ±100 (ASI plugin), >200 = "plenty of signal". Pause after each
  > adjustment: detector motion gives large temporary values. Keep Sum in 50–80 (0 or 100 =
  > saturated → redo Log Cal).
- **SNR is measured once, during Log Cal**; `EX Y?` returns that stored value. It cannot
  respond to the knob (the removed Tune mode's default meter followed SNR).
- The system "performs best if the photodiode signals are approximately balanced", i.e. the
  lock offset is not too large.
- Stationary-lock levers: Averages `RT F=N` (2^N samples; "N of 5 to 8 with reduced loop
  gain (LR T=1 to 5) … hold the focus very still"), Update rate `UL Y` ("Increase
  Update_Rate to improve stability when using piezo Z-axis drive systems", piezo default
  5 ms; raise Loop Gain with it), Loop Gain `LR T` (reduce if it jitters/oscillates).
- **Focus curve**: plot error vs Z to see the usable lock range, capture range, and whether
  the lock point sits near a peak of the curve (low slope, hard to hold). On Tiger, ASI's
  plugin does this in software (step Z, read the error).
- While dithering, the controller cycles sub-states; the adapter reports 'f' as "Dither"
  but 'g'–'j' and 't' as **"Calibrating"**.

## Evidence from the rig (`pymmcore-gui/CRISPTest/crisp_drift_*.csv`, ~5–6 s sampling)

| Run        | Axis  | SNR | Sum   | Lock Offset | Position step rms | Detrended rms |
| ---------- | ----- | --- | ----- | ----------- | ----------------- | ------------- |
| 2026-08-12 | piezo | 7.1 | 77    | 474         | 36 nm             | 72 nm         |
| 2026-08-14 | piezo | 3.8 | **0** | 6222        | 117 nm            | 112 nm        |
| 2026-08-14 | stage | 4.6 | 53    | 5389        | 4 nm              | 20 nm         |

On 2026-08-14 the piezo's Sum was 0 (saturated low) for 15 minutes while it reported
"In Focus". The locked error was only ±3.5 counts while the piezo moved ~100 nm, so the
in-loop error does not show focus motion; actuator position does. These logs are too
coarse to see second-scale noise.

## Changes

1. **Remove Tune** (`tuning.py`, its tests and UI).
1. **Dither assistant** (`dither.py`, Qt-free + UI tab): runs while the state is Dither or
   Calibrating; reads only Dither Error + Sum (+ state ~2 Hz); median-filtered |Err| with
   peak-hold, 30 s trace with 100/200 marks, Sum band check, achieved rate. Pauses the slow
   poll.
1. **Focus curve** (`focus_curve.py` + UI tab): in Ready, step the paired actuator through
   ±range around the current position, read the error at each step, always return to the
   start. Reports the zero crossing, measured slope (counts/µm) next to the
   calibration-derived value, the peaks and the lock point's margin to each, capture range,
   and the error noise → predicted focus noise floor (nm). Confirmation before moving; CSV
   export.
1. **Hold test** (`hold.py` + UI tab): while locked, record actuator position + error for
   N s at a chosen rate; report drift (nm/min), jitter < 1 s, wander ≥ 1 s, pk-pk, error
   rms, lock-offset change, lock losses, and the lock settings at the time. Keeps the last
   runs side by side for one-change-at-a-time comparison; CSV export. It changes no
   settings.
1. **Lighter slow poll**: every tick reads State, Dither Error, Sum; SNR / AGC / Lock
   Offset / Calibration Gain / Range are read on start, after a button, on a state change
   and every 10 s. `RefreshPropertyValues` (a software-only, persistent adapter flag) is
   written once, not every tick. The in-loop fluctuation readout only accumulates while
   locked and resets on state changes.
1. **Labels and guidance**: "Update Skips" → "Update rate (ms)", "Averages" → "Averages
   (2^N)", manual-based tooltips, Sum colour band, SNR "measured at Log Cal".
1. **Demo**: mock follows the manual's model (static SNR, dither Err vs a demo screw slider,
   S-shaped error curve, lock jitter ∝ noise/slope reduced by averaging); demo controls for
   the screw, Averages and Loop Gain; mock supports stage positions for curve/hold.
1. Docs: README, CLAUDE.md.

## Decisions

- CRISPy still never writes Averages / Update rate / Loop Gain (user kept the rule); the
  user changes one setting at a time and the hold test judges it, per the "verify one step
  at a time" lesson.
- Focus curve is software-based through the Core (move actuator, read `Dither Error`), not
  raw `LK F=97` serial output, which the host's own helper flags as unverified.
- Pure Python statistics (no new dependencies).

## Changes from the plan while building

- The Dither peak first latched a decaying detector-motion jump (498 vs a true 319 in
  the demo): a 1 s median can't reject a jump that lasts ~1 s, and the real duration is
  unknown. The peak now only latches when the window is steady (its thirds agree within
  10%), and the panel shows "settling…" until then.
- Pressing 2. Dither no longer forces one full slow read just before the assistant
  takes the port.
- The Focus curve summary says when the measured slope and the calibration's
  Sensitivity disagree by more than ~1.4×.

## Verification done

- `uv run pytest -q`: 76 passed (9 lock-offset + 67 new). 20 deliberate mutations of the
  new modules were all caught.
- `ruff check .` at the 19 findings `main` already had; `ruff format`, `mdformat` clean.
- Headless smoke test of the real panel against the demo (PyQt6 offscreen, simulated
  10 ms per serial call), all checks passing on repeated runs: light poll (state / Err /
  Sum per tick, the rest every 10 s, RefreshPropertyValues once); Dither assistant
  (~30 reads/s, only Err/Sum/State, peak within 6% of the best true |Err| despite
  300-800 transients, stops when the state leaves Dither); Focus curve (slope within 3%
  of the model, peaks at ±2.0 µm, actuator back at start, refused while locked, buttons
  disabled while scanning); Hold test (no writes, settings recorded, Averages 1 → 6
  lowered the demo's rms 11 → 3 nm); MDA / hide / close stop the tools; no threads left.
- The demo numbers come from a model, not the rig.

## Bench checklist

1. Log Cal → 2. Dither: watch the Dither tab while turning the screw slowly; note the
   peak (≥ 100) and that Sum stays 50-80. Then 3. Set Gain.
1. Ready → Set Offset → Focus curve: slope vs calibration, lock point margins, noise
   floor.
1. Lock → Hold test (60 s) with current settings; then change one Parameter (Averages,
   Update rate, Loop Gain) and repeat. Also try the hold rate at 2 Hz vs 20 Hz.
1. Check the dither readings still change between samples (RefreshPropertyValues is
   once only; the always-read properties shouldn't depend on it).
