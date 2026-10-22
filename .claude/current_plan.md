# Compact panel: diagnostics in their own window

**Status: implemented; tests and an offscreen render of the demo panel pass. Not yet
seen on the rig** (CRISPy branch `feat/crisp-unlocked-hold-and-na`).

## Why

The Dither / Focus curve / Hold test tabs, with their plots, intro text and 13-column
hold table, sat inside each axis's panel. A tab widget is as large as its largest
page, so with two axes side by side the CRISPy dock took most of the main window and
left little room to see the cells.

## What changed (`ui.py`)

- The tools' `QTabWidget` moved into `_ToolsWindow`, a top-level window owned by the
  panel (`CRISP diagnostics: <label>`). The panel has a **Diagnostics…** button and an
  orange `<tool> running` label.
- **2. Dither** opens the window on the Dither tab (the assistant is what you watch
  while turning the screw). A Dither state seen by the poll only selects the tab; it
  doesn't pop a window up unasked.
- Closing the window stops a running tool, as hiding the panel already did.
- **Parameters** is a collapsible `_Section`, collapsed by default.
- The Calibration box title is just "Calibration" (the order is in its tooltip and the
  button numbers); the Sensitivity and In-loop labels word-wrap. Both had set the
  panel's minimum width.

## Checked

Measured with real Windows fonts (`WA_DontShowOnScreen`), per panel: minimum size
460×897 → 304×477 px, size hint 517×1033 → 304×493. Two axes side by side now fit in
about 670×500. 141 CRISPy tests and the host's `test_crisp_snap_provider.py` pass;
ruff clean; mypy at the 12 errors `ui.py` already had.
