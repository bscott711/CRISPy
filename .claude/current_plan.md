# Dither back in the panel; Set Ready and the LED

**Status: Dither layout implemented; 141 tests pass, ruff format clean, no new ruff
errors. Not yet seen on the rig.** LED: researched, no code change (needs a bench probe
and the user's choice). Branch `feat/crisp-unlocked-hold-and-na`.

## 1. Dither inside the panel (implemented)

The user: Dither "pops a giant window up"; they preferred it in the plugin, just not so
wide. Commit `cc12c60` had moved all three tools into `_ToolsWindow` because a tab widget
is as large as its largest page (Focus curve, Hold table). Dither alone is small.

- `ui.py`: Dither is a collapsible `_Section("Dither")` right after the Calibration box,
  collapsed by default. **2. Dither** expands it and starts the assistant; no window pops
  up. Collapsing the section stops a running Dither.
- `_ToolsWindow` holds only Focus curve and Hold test (button "Focus curve / Hold
  test…"); closing it stops only those two. Default size `resize(540, 520)` (was
  640×780).
- The Dither body is narrow: word-wrapped labels, plot `min_height=120`, "Smooth:" label.
  Its minimum width (412 px offscreen) is below the Calibration box's (438), so the
  panel's minimum width is unchanged (460 px offscreen; Windows fonts measured 304 px in
  `cc12c60`).
- Hide, close and MDA start still stop every tool (`_stop_tools` = Dither + window tools).

## 2. Set Ready after Set Idle leaves the LED dark (research; no change)

CRISPy writes exactly what ASI's adapter and plugin expect: "Ready" makes the adapter
send `LK F=85` and "Idle" `LK F=79` (`ASICRISP.cpp:373-378`, unconditional write). The
manual's state table calls code 85 **Unlock** and lists Ready as "LED on - @ button
locks"; the documented way from Idle to Ready is a short press of the `@` button, and it
warns that `LK F=` "out-of-sequence events are not necessarily handled smoothly". What the
firmware does with 85 while Idle is undocumented. ASI's own adapter only sends 85 from
Idle immediately followed by Lock (83), and ASI's plugin has no Ready button.

Next step is a bench probe, then the user picks an option:

1. Idle → Set Ready: does Sum stay near 0 (LED dark)?
2. In that state, rewrite `LED Intensity` with its current value (adapter sends
   `UL X=n`): does Sum come back?
3. Lock → Unlock: Sum back (the user says yes)?

If (2) relights it, Set Ready from Idle can also rewrite `LED Intensity` with the value
it read (property through the Core; a direct effect of the user's button). If not, the
options are a label/tooltip change (the button text assumes any non-Idle state means
LED on, `ui.py` `_update_state`) plus a hint to use Lock/Unlock, or `LK F=90` (LED On)
through the hub's `SerialCommand` (raw serial; failed before on this rig for the
joystick, needs the user's OK).

---

## LED On button and LED Intensity apply (separate feature)

Separate feature, appended (the Dither plan above is uncommitted, so it is kept here
rather than replaced). Rig-unverified.

### Root cause: LED On never relit the LED

- The rig config `20260630-OPM.cfg` has `Parent,` lines for the stages only, none for
  `CRISPAFocus:Z:32` / `CRISPAFocus:P:34`. The adapter still finds its hub through
  MMCore's same-module inference (`DeviceManager.cpp:256-278`), but
  `getParentLabel` returns the stored parent ID, `""` (`MMCore.cpp:1280-1288`).
- `set_led(True)` then read `getProperty("", "OnlySendSerialCommandOnChange")`, which
  raises ("No device with label"), and `ui.py` `_do` swallowed it with
  `suppress(Exception)` while the button still flipped to "on".
- Second bug: the adapter skips a `CRISP State` write equal to its cached state
  (`ASICRISP.cpp:409-413`), and a raw `LK F=90` from Idle still reads back Idle, so
  LED Off through `set_idle()` would have been a no-op. (Section 2 above calls the
  state write "unconditional"; in this adapter version only `ForceSetFocusState` is.)

### Changes

1. `controller.py`: `_hub_label()` uses the parent label, else the single loaded
   `HubDevice` of the same library with a `SerialCommand` property (else a clear
   `RuntimeError`), cached. `_send_addressed()` writes `<card char>LK F=..` with
   `OnlySendSerialCommandOnChange` forced to No and restored. `set_led(True)` sends
   `LK F=90`, `set_led(False)` sends `LK F=79`. `set_idle()` unchanged.
   `should_reapply_led(state, led_on)`: True only for Idle/Ready with the LED on.
2. `ui.py`: `_led_write()` shows failures in a red label under the LED button and in
   the log, and reverts the button. An LED Intensity edit (PropertyWidget
   `valueChanged`) restarts a 200 ms single-shot timer; on timeout the panel relights
   the LED if `should_reapply_led(_last_state, button on)` and no tool is running.
   Never while locked, calibrating or dithering; the tooltips say to unlock and re-lock.
3. Tests: hub fallback (none/one/two hubs), caching, raw Off, guard table (163 pass).

### Bench check

1. P axis: Set Idle, then LED On: lit? LED Off: dark? On again: lit? Repeat on Z, and
   check the other axis does not change.
2. Idle with LED on: drag LED Intensity; the power meter follows about 0.2 s after you
   stop.
3. Ready: change the intensity. Does it apply, and does the state still read Ready
   (F=90 in Ready is undocumented)?
4. Locked: change the intensity. Nothing is sent and the state stays locked; Unlock
   and re-lock apply it.
5. Error path: no red message under the LED button during 1-4.
