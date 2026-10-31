# CRISPy Qt UI polish (presentation only)

## Context
CRISPy's Qt panels (`src/CRISPy/ui.py`, `plugin.py`, `plot.py`, `spot/panel.py`, `spot/live_view.py`) have grown organically:
16 ad-hoc `setStyleSheet` calls with hard-coded hex colours, no shared spacing/type rules. The user finds the **piezo axis
panel** (Operation, Readouts, Calibration, Dither, Spot lock) the ugliest. Goal: a compact scientific-desktop design system
(DESIGN.md) applied through a few shared presentation helpers, with zero change to controller, gating, polling, threads or
hardware behaviour.

## Decisions (user, 2026-10-30)
- Review-gate panel: **piezo axis panel** (with Spot lock section).
- `run_demo.py` may be edited: extend the mock core with a property schema so real `PropertyWidget` rows build.
- Replace `.claude/current_plan.md` (finished Dither/LED plan; git keeps it) with this plan, per CRISPy CLAUDE.md.
- `pyqt6-ui-designer` skill is NOT installed; use general Qt design knowledge and say so in DESIGN.md.

## Hard constraints
qtpy only; keep PropertyWidget rows; no app-global theme/palette/font/stylesheet; stylesheets scoped by objectName/class,
supported properties only (capture Qt "Unknown property" warnings = failure); gating/signals/polling/threads/widget attribute
names unchanged; do not raise `minimumSizeHint` (record before/after; panel min width 460 px offscreen, 304 px rig Windows);
both axes visible in two-device layout; no widget library, no `.ui`; Qt-free modules stay Qt-free; colour never the only
signal, never red-vs-green alone; semantic colours derived from host `QPalette` with fixed fallbacks.

## Steps
1. Branch `feat/ui-polish` from clean `main`. Write this plan to `.claude/current_plan.md`.
2. Harness: run demo with `pymmcore-gui/.venv/bin/python` (has PyQt6 + pymmcore_widgets) on private Xvfb `:97`
   (`~/opt/naparym-live/current/sysroot/usr/bin/Xvfb`, cookie first, `LIBGL_ALWAYS_SOFTWARE=1`); scripted Qt events for
   interaction; DCV (`CORE/seglab/dcv_display.sh`) only to show finished results. Fall back to offscreen `grab()` and say so
   if Xvfb fails. Kill only own processes (find PIDs in a separate /proc-scanning call).
3. Extend `run_demo.py` mock: `hasProperty`/`getPropertyType`/limits/allowed values for `_PARAMS` in `ui.py` so
   `PropertyWidget` rows appear. Harness-only. Any parameter still unexercised is listed by name as unvalidated.
4. Baseline: screenshots (piezo+Spot lock, stage, two-axis) at fixed sizes before any edit; record `minimumSizeHint`;
   dump `isEnabled()`/`isVisible()` of all named widgets across gating states (idle, locked, MDA, tool running, spot lock
   active) to JSON.
5. Audit: list every `setStyleSheet`/colour/font/spacing site; note dark-theme and palette-dependence problems.
6. Write `DESIGN.md`: spacing, type scale (host fonts, relative sizes), semantic colours (palette-derived), widget states
   (normal/hover/focus/disabled/error/active-tool), section hierarchy, plot conventions; Linux-vs-rig font caveat.
7. **Gate:** show before/after mock of the piezo panel; wait for user go-ahead before implementing.
8. Implement: small shared helpers (one module, e.g. `src/CRISPy/style.py`) only where reused; apply to ui.py, spot/panel.py,
   live_view.py chrome, plot.py conventions. Replace hard-coded hex with palette/semantic helpers.
9. Validate (separately reported):
   - Tests: `uv run pytest -q` (Qt-free; proves logic modules intact).
   - Behaviour-neutrality: re-run widget state dump, assert identical to baseline; diff shows no `connect`/`Thread`/poller/gating
     lines changed.
   - Visual: piezo+Spot lock, stage, two-axis; sizes panel 460x700 & 600x900, two-axis 1000x700 & 1400x900; fonts 100/125/150%
     (harness-only `QApplication.setFont`); before/after pairs at identical sizes.
   - Interaction: focus, disabled (MDA / tool running / spot lock active), error (LED error label, failed read), each active
     tool (Dither, Focus curve, Hold test, Spot lock).
10. Ship loop: commit, PR into CRISPy's own repo, merge, pull main. No deploy, no rig.
11. Deliver: screenshots via SendUserFile (one-line captions), result shown on DCV, lab-notebook artifact (before/after,
    design choices + evidence); note its URL in a memory. Only DESIGN.md + code committed; screenshots stay out of the repo.

## Critical files
`src/CRISPy/ui.py`, `plugin.py`, `plot.py`, `spot/panel.py`, `spot/live_view.py`, `run_demo.py`, new `DESIGN.md`,
new shared style helper; reuse `_PARAMS`, `_Section`, existing palette-aware code in `plot.py`.

## Verification
Pytest green; widget-state dump identical before/after; zero Qt "Unknown property" warnings; minimumSizeHint not larger;
screenshot matrix complete; unvalidated items listed explicitly.
