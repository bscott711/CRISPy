# Fast Tune mode for CRISP detector-knob alignment

**Status: implemented; unit tests and a headless smoke test pass. Not yet bench-tested on
the rig (see "Bench checklist").**

## Why

Aligning the CRISP line detector means turning a physical knob on the CRISP unit while
watching the panel. The knob is mechanical (no software readout), and the panel could not
keep up: you could sweep the whole range without seeing the peak.

Causes found in `ui.py`:

1. `_STABILITY_WINDOW = 20` polls at 750 ms is ~15 s of memory, so the fluctuation metric
   is a smear of everything the knob did in the last 15 s.
2. Each slow poll tick did ~11 blocking operations on real hardware (a
   `RefreshPropertyValues` write, the state, 2 sensitivity reads and 7 readout reads).
   Calibration Gain / Range were read twice per tick.
3. Nothing held a peak, so the maximum had to be spotted by eye.

## What was built

- `src/CRISPy/tuning.py` (pure Python, no Qt):
  - `read_tune_sample`: reads only SNR, Sum and Dither Error (3 reads, no writes).
  - `TuneTracker`: time-based rolling window (default 0.5 s), peak-hold per metric, achieved
    sample rate. SNR and Sum are smoothed over the window before being peak-held, so a
    noise spike can't become the "peak". A window only counts once it holds at least 4
    samples spanning 80% of the window.
  - `TuneSampler`: free-running worker thread, capped at 100 Hz. Ends itself if its
    callback raises or the device leaves the core.
- `ui.py`: a **Tune** group box (Start/Stop, Reset peak, Window, Meter metric, meter bar,
  live-vs-peak readouts, Hz).
  - The slow poller is paused while tuning, so only one reader uses the serial port, and
    the Readouts box is labelled "paused while tuning" because its values freeze.
  - Tune stops on hide, close and MDA start.
  - A watchdog shows "no data from the controller" if samples stop for more than 1 s.
- `controller.py`: shared `counts_per_um()` helper. The slow poll now derives sensitivity
  from the readouts it already read (10 -> 8 reads per tick; 11 -> 9 with the refresh write).
- `run_demo.py`: mock completed (`getLoadedDevices`, `hasProperty`, `mda`), simulated knob
  sweep and serial latency, and a stand-in `AsyncPoller` when the real one is missing.
- `tests/test_tuning.py`: 31 Qt-free tests (the 10 mutants tried against them were all caught).

## Decisions and deviations from the first plan

- Tune does **not** use `AsyncPoller`. It lives in `pymmcore_widgets.control._async_poller`,
  which is absent from the latest PyPI release (0.12.1, the version `uv.lock` pins). The slow
  poll still imports it (pre-existing), so `CrispControlPanel` only constructs in an
  environment whose `pymmcore-widgets` has it.
- Default window is 0.5 s, not 1.0 s: a 1 s window lags a hand-turned knob by ~0.5 s.
- Added the "no data" watchdog and the "paused while tuning" label after reviewing the diff.
- `RefreshPropertyValues="Yes"` is written once when Tune starts (assumption: persistent
  mode). If bench values look frozen, move the write inside the sample loop in
  `TuneSampler._run`.
- Number of Averages / Update Skips are not touched; they write controller state.

## Verification done

- `uv run pytest -q`: 40 passed (9 existing lock-offset tests + 31 new).
- `ruff format --check .` clean; `ruff check .` unchanged at its 19 pre-existing findings.
- Headless smoke test of the real panel against the demo mock (PyQt6, offscreen), simulated
  10 ms per serial call: slow poll 1.2-1.4 Hz at the default interval and 10 Hz at the
  fastest spinbox setting; Tune ~33 Hz. Over a full simulated sweep the peak-held SNR
  reached 9.4 of a true ~9.8 and the meter read ~100% back at the optimum. Also checked:
  Tune touches only the 3 properties, the slow poll pauses and resumes (and respects an
  unchecked Poll box), and hide / close / MDA start / link loss are handled.

These rates come from a mock with an *assumed* serial latency, not from the rig.

## Bench checklist

1. Start Tune and read the achieved Hz.
2. Check Dither Error changes between samples; if it looks frozen, see the
   `RefreshPropertyValues` note above.
3. Sweep the knob; confirm the peak-held SNR and the meter let you land on the maximum.
4. If Hz is capped by the controller, try lowering Number of Averages / Update Skips.

## Noticed, not changed

- A failed `CRISP State` read becomes `""`, which counts as unlocked and re-enables the
  joystick wheel while the axis may still be locked.
- The README still says "Zero Serial Polling" and mentions magicgui.
- `CLAUDE.md`'s "update from `propertyChanged`" no longer matches the code.
