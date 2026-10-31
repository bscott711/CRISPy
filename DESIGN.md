# CRISPy UI design system

Scope: presentation of CRISPy's Qt panels only (`ui.py`, `plugin.py`, `plot.py`,
`spot/panel.py`, `spot/live_view.py`). Nothing here changes gating, polling, threads,
signals or widget attribute names. The `pyqt6-ui-designer` skill is not installed; this
document uses general Qt design knowledge. "Audit N" cites section N of the T02
presentation audit (`.claude/orchestration/ui-polish/`, rev e15b4b8); "A7.k" is its
ranked problem k.

## 1. Principles

1. Compact and scientific: numbers first, chrome second. No decoration without a job.
2. Follow the host. Colours, fonts and base sizes come from the host `QPalette` and
   `QFont`; CRISPy never sets an app-global theme, palette, font or stylesheet.
3. One look per role. Same meaning, same colour and shape everywhere (Audit 2 found
   three greens, three oranges, two reds for the same meanings).
4. Colour is never the only signal. Every status pairs a colour with a word or a glyph.
5. Never wider than today. Rule (about the panel's own hint, not the window minimum, which is
   smaller because of the scroll wrapper: 70 px for one axis, 144 for two): the panel's
   `minimumSizeHint()` width must not exceed the recorded baseline at the same font scale (baseline `min_sizes.json`; at 100%: piezo 354
   collapsed / 515 sections open, stage 371). The rig Windows figure of 304 px is the user's
   earlier measurement, not re-verified. No new fixed widths.
6. Measure before claiming. Every layout change is checked against the validation
   checklist (section 11).

## 2. Spacing scale

Pixels, multiples of 2 (4 below 12). Fonts scale with DPI but px margins do not, so
where a gap sits next to text, prefer the font-relative form `em = QFontMetrics(font).height()`
(1 em is about 15 px at 9 pt, 96 dpi): `xs = 0.25 em`, `s = 0.5 em`, `m = 0.75 em`.
Use px for the table below and the em form only if a high-DPI check (125%, 150%) looks
cramped.

| token | px  | use                                          |
| ----- | --- | -------------------------------------------- |
| `xs`  | 2   | glyph-to-text, badge vertical padding        |
| `s`   | 4   | spacing inside a row; between rows in a group |
| `m`   | 6   | between sections; panel outer margin          |
| `l`   | 8   | group content inset (left/right)              |
| `xl`  | 12  | gap between unrelated blocks (rare)           |

Per container level (today everything is Qt default: margins 9, spacing 6; Audit 4):

| container                              | margins (l,t,r,b) | spacing |
| -------------------------------------- | ----------------- | ------- |
| panel root (`CrispControlPanel`)       | 6,6,6,6           | 6       |
| header row, poll row                   | 0,0,0,0           | 6       |
| section body (group or `_Section`)     | 8,4,8,6           | 4       |
| button row inside a body               | 0,0,0,0           | 4       |
| label/value grid (Readouts)            | 8,4,8,6           | 4 vertical, 12 between column pairs |
| form (Parameters)                      | 8,4,8,6           | 4       |
| plot or image (full body width)        | 0                 | n/a     |

Rules: all section bodies use the same 8 px inset (A7.1: today `_Section` bodies are flush
at 0 and group boxes inset 9). Every margin above is at or below the Qt default, so none
raises `minimumSizeHint`; any change that would is rejected. No `setSpacing` literal
outside these tokens.

## 3. Type scale

Sizes are derived from the host font, never from px literals (Audit 3: 14 px title,
22 px readout and 11 px badge ignore pt/DPI scaling). `base = QApplication.font().pointSizeF()`
(Linux Sans Serif 9 pt; rig Segoe UI 9 pt). Apply with `QFont.setPointSizeF(round(base * k, 1))`
on the widget, not with QSS `font-size: Npx`. (QSS `font-size: Npt` is supported but fixes
the size and does not follow a font change; avoid it.)

| role          | factor | weight | at 9 pt | where                                              |
| ------------- | ------ | ------ | ------- | -------------------------------------------------- |
| caption       | 0.9    | normal | 8.1 pt  | hints, units, badge text, plot ticks (A3: ticks are `max(7, 0.85 base)`, align to 0.9) |
| body          | 1.0    | normal | 9 pt    | labels, buttons, readout names, readout values     |
| section       | 1.0    | bold   | 9 pt    | section header text                                |
| panel title   | 1.2    | bold   | 10.8 pt | device label (replaces 14 px)                      |
| hero readout  | 1.6    | bold   | 14.4 pt | the dither `\|Err\|` number only (replaces 22 px)  |

Rules: readout values are body weight, not bold (Audit 3: all 8 are bold today, so bold
carries no information); bold is reserved for section headers, the panel title, the state
text and the hero number. Never more than these five sizes. Plot and chart text uses the
caption size, with tick rects sized from `QFontMetrics`, not fixed 60x14 px (Audit 3).

## 4. Semantic colour roles

Roles: `ok`, `warn`, `error`, `active` (a tool or lock is running), `info` (muted),
`accent` (selection, focus, data series 1). Derive from the host `QPalette`, with fixed
fallbacks used only when the derivation fails or a role has no palette analogue.

Theme test: `dark = palette.color(QPalette.ColorRole.Window).lightnessF() < 0.5`
(re-evaluate on `QEvent.PaletteChange`; the hard-coded styles of Audit 5 never do).

Derivations:

- `info` = `windowText` blended 60/40 with `window` (muted text; 4.5:1 or better on the
  window by construction when the host text is high contrast).
- `accent` = `palette.highlight()`; if its contrast against `window` is under 3:1, use the
  fallback instead.
- `ok`, `warn`, `error`, `active` have no palette role; start from the fallback and, if the
  host's `windowText` lightness differs from the fallback's by under 0.3, nudge toward
  `windowText` by 10%. Keep hue: the hue carries the role, lightness carries legibility.
- Backgrounds for badges and banners: the role colour blended 12% into `base`; text on
  it is `windowText`, so contrast does not depend on the role colour.

Fixed fallbacks (contrast ratios against #efefef light / #2b2b2b dark, computed with the WCAG
luminance formula; Audit 2 measured the old values at 1.9-3.2:1 on light):

| role     | light     | on #efefef | dark      | on #2b2b2b | meaning                                              |
| -------- | --------- | ---------- | --------- | ---------- | ---------------------------------------------------- |
| `ok`     | `#1b7a34` | 4.7        | `#66bb6a` | 6.0        | locked, in focus, in band, SNR good                  |
| `warn`   | `#a85a00` | 4.4        | `#ffb74d` | 8.2        | Sum low/high, busy, tool running, spot noise middling |
| `error`  | `#c62828` | 4.9        | `#ef5350` | 4.1        | fault, saturated, SNR bad, travel limit              |
| `active` | `#0b6e99` | 4.9        | `#4fc3f7` | 7.1        | Dither/Focus curve/Hold test/Spot lock running       |
| `info`   | `#5f6b73` | 4.8        | `#9aa5ad` | 5.6        | unknown, `--`, hints, zero/start reference lines     |
| `accent` | `#1565c0` | 5.0        | `#64b5f6` | 6.4        | focus ring, selection, data series 1                 |

Dark `error` is 4.1:1: use it only for bold or large text, or with a glyph.

Replaces: `ui.py:332-339` (`_GREEN`, `_DARK_GREEN`, `_ORANGE`, `_RED`, `_GREY`, `_BLUE`),
`spot/panel.py:97-99`, and the chrome (not the overlays) of `spot/live_view.py:83-90`. The
camera-view overlays stay fixed: they are drawn on a fixed dark canvas (`#1c1c1c`), where
neon colours are legible, and spot setpoint stays magenta.

Channels: where a channel appears (overlays, legends, OME-style labels) channel 2 is magenta
(`#b0179b` light, `#ff6ee6` dark), channel 1 is green, never red/green. No violation exists
today (Audit 2).

## 5. Colour is never the only signal

Each status pairs its colour with text or a glyph, so a colour-blind or greyscale reader gets
the same answer. Never put red against green alone: hue-only red vs green pairs have a 1.32:1
luminance ratio (Audit 2).

| status              | colour   | paired cue (mandatory)                      |
| ------------------- | -------- | ------------------------------------------- |
| good / in band      | `ok`     | glyph `✓` plus the word ("good", "in band") |
| warning / out of band | `warn` | glyph `▲` plus the word ("low", "high")     |
| bad / fault         | `error`  | glyph `✕` plus the word ("bad", "saturated", "fault") |
| running tool        | `active` | glyph `●` plus the tool name                |
| unknown / no data   | `info`   | `--`                                        |

Sites that need a cue added first (Audit 2, A7.4): SNR readout (`ui.py:2599`, today green/red
only), Sum readout (`ui.py:2604`, low and high share orange), the `|Err|` level
(`ui.py:1757`, five colours, no word). The state label, dither Sum label, spot noise and fault
text already carry words. When a glyph is risky in a font, fall back to the plain ASCII word.
Where an ok band and an error band must both appear (e.g. spot noise bands), the glyph and word are
mandatory; do not rely on hue. The new `ok` (#1b7a34) and `error` (#c62828) have a luminance ratio
of only 1.04 (recomputed; the 1.32 above is for the old colours), so in greyscale they are
indistinguishable and the glyph plus word is load-bearing: it stays mandatory even where a tooltip
or border also marks the status. Tooltips may repeat but never carry the only cue.

## 6. Widget states

Implement with scoped QSS selectors on `objectName` or a dynamic property, plus
`style().unpolish(w); style().polish(w)` only when the property value changes (Audit 5:
repainting every 0.1 s with unchanged strings re-polishes needlessly; compare with the last
value first).

| state        | what changes                                                         | how                                         |
| ------------ | -------------------------------------------------------------------- | ------------------------------------------- |
| normal       | host look; no QSS on buttons, spin boxes, check boxes                | nothing (keep native rendering)             |
| hover        | text/background lightens one step; header row gets a hover background | `#crispSectionHeader:hover { background: <info 12%> }` |
| focus        | visible 2 px ring in `accent`                                        | `:focus { border: 2px solid <accent>; }` on QToolButton/QLineEdit only; leave native focus rects elsewhere |
| disabled     | host disabled palette (never a custom grey); the cue is not colour alone | native `setEnabled(False)`; do not restyle (A7.8: the disabled LED bar) |
| error        | `error` text with `✕` glyph and a word; the field gets a 1 px `error` border | dynamic property `status="error"`; selector `QLabel[status="error"]` |
| active tool  | `active` text plus `●` and the tool name; section header shows "(running)" | property `status="active"` on the label/header |
| selected / checked | button checked: host look plus bold text; list/table selection from `highlight` | native; add `font-weight: bold` via property |

Note (T03 observation): in the real panel a running MDA disables no widget; only the poller
stops. Disabling comes from a running tool or an active spot lock. Wherever this document or
the checklist mentions "disabled (MDA ...)", read it as the MDA row showing no disabled widgets.

Supported QSS only: `color`, `background`, `background-color`, `border` (+ `-color/-width/-style`),
`border-radius`, `padding`, `margin`, `font-weight`, `font-style`, `min-width`, `min-height`,
selectors `#id`, `Class`, `[prop="v"]`, `:hover`, `:focus`, `:disabled`, `:checked`.

Unsupported or ignored in Qt Style Sheets, avoid: `box-shadow`, `text-shadow`, `transition`,
`animation`, `gap`, `cursor`, `opacity`, flexbox/grid properties, `calc()`, CSS variables
(`var()`), `@media`, `rem` units, `%` font sizes. Colours are written as `#rrggbb` hex from the
role helper (`rgba()` is supported by Qt but not needed). Do not stylesheet native buttons, spin boxes
or sliders: any QSS on a `QToolButton`/`QPushButton` drops native rendering and hover feedback
(Audit 1, row 1). Always scope selectors (`#crispPanel QLabel[status="error"]`); never style
`QWidget` or `*` bare.

## 7. Section hierarchy

Levels, top to bottom:

1. Panel title row: device label (panel title, 1.2x bold), actuator badge ("Piezo"/"Stage",
   caption, pill with 6 px radius, 2/6 padding, fill = role colour blended into `base`, text
   `windowText`, so it stays visible on dark windows; Audit 1 row 9, Audit 5), state text on the
   right (bold, role colour plus the word). Poll row below.
2. Section header: one style for all sections. A 1 px bottom rule in `info` 40%, text bold
   (1.0x), a disclosure arrow, hover background (section 6). Both always-open groups and
   collapsible sections use it.
3. Section body: 8 px inset, spacing 4 (section 2).

Unification (A7.1): `Readouts`, `Operation`, `Calibration` stop being `QGroupBox`es with plain
small titles and become `_Section`s with the same header; `Readouts` and `Operation` default to
open, others as today. `_Section` must gain a `setTitle(text)` that delegates to its toggle button's
`setText`: `ui.py` calls `self.readouts_box.setTitle("Readouts  (paused: ...)")` (ui.py:1525-1531) and the
"(paused: X)" text must keep showing in the header. Keeping the attribute name alone would raise
AttributeError. Order is kept: header, poll, Readouts, Operation, Calibration, Spot lock,
Dither, tools row, Parameters (Audit 4). The tools button row joins the Dither section (it
launches the diagnostic tools) instead of floating between sections (A7.8).

Spot lock flattening (A7.2): the section keeps one header, "Spot lock". Inside: the
`QSplitter` and inner `QScrollArea` and the group box titled "Spot focus lock" are removed,
leaving a plain vertical column: live view (min 240 px as today), state/fault/calibration
labels, controls rows, strip chart, readouts. The sub-groups "Edge box and calibration sweep",
"Camera" and "Parameters" become sub-headers (plain bold label plus a 1 px rule, no frame), at most one
nesting level below the section. The panel's single scroll area is the only scroller (no scrollbar
inside a scrollbar; today the Camera group is clipped, Audit 2/4 row 5a).
Duplicates (LED button, Lock, Offset, second Parameters group; A7.3) are labelled by
role, not removed in this run: their behaviour is out of scope; they get the same style and a
"(spot)" qualifier in the sub-header.

## 8. Layout rules

- Two-device layout: both axes stay visible side by side (splitter, `plugin.py:56`); the layout
  must not push either off screen at the two-axis sizes in section 11.
- The panel's own `minimumSizeHint()` width (not the window minimum, 70 px one axis / 144 px two axes
  via the scroll wrapper) must not exceed the recorded baseline at the same font scale (baseline `min_sizes.json`; at 100%: piezo 354 collapsed / 515 sections open, stage 371). The rig Windows figure of 304 px is the user's earlier measurement, not re-verified. Record
  `minimumSizeHint()` before and after; a larger value fails.
- No new fixed widths: no `setFixedWidth/Size`, no new `setMaximumWidth`. Existing caps
  (poll box 110, parameter widgets 190) stay but are not extended.
- Readouts grid: columns name | value | name | value with stretch on columns 1 and 3 and
  right-aligned values; the two sentence labels (`sensitivity_lbl`, `stability_lbl`) use caption
  size, `info` colour, word wrap, full-width (A7.7).
- Parameters form: labels left-aligned, one field column, field edge flush (not ragged).
- Primary action emphasis: the Lock/Unlock button is the one default-looking action in
  Operation; persistent writes ("Save to Controller") are visually separated, in their own row
  (A7.8). Do this by layout, not by styling buttons.
- Long text wraps (`setWordWrap(True)`); never elide numbers.

## 9. Plot conventions

`plot.py` `LinePlot` is the model: palette-aware (`Base` background, `WindowText` axes, grid
at `WindowText` alpha 40) and the only one that adapts today (Audit 5). Extend it, do not copy
`StripChart` (black box, fixed greys, fixed pixel offsets).

- Background `Base`, axes and tick text `WindowText` at caption size; grid 1 px at alpha 40
  (alpha 60 on dark `Base`).
- Series colours come from section 4, in order: series 1 `accent`, 2 `warn`, 3 `info`, 4
  magenta. Reference lines: `info`, dashed, 1 px; target/limit lines `ok` dashed. No red/green
  pair as the only distinction: vary dash or marker as well.
- Line width 1.5 px for the live series (today 1.2 / 1.5), 1 px for references and grid; markers
  4 px.
- Labels use `WindowText` (not the line colour; Audit 5 found grey/orange labels at 2.3:1/1.9:1),
  with a short line-segment legend swatch for colour.
- Tick label and margin sizes derive from `QFontMetrics` (replace the fixed 60x14 and the
  `46,10,8,30` margins when touched), so 125% and 150% fonts do not overlap.
- Dark mode: the plot follows `Base`; `StripChart` (`spot/panel.py:357-398`) becomes
  palette-aware with the same rules. `_FrameView` and the live camera canvas stay fixed dark:
  they are image viewers.

## 10. Stylesheet scope and implementation

- No app-global theme, palette, font or stylesheet. `QApplication` is the host's; never call
  `QApplication.setStyle/setPalette/setFont/setStyleSheet`.
- Every stylesheet is set on a CRISPy widget (the panel root or a child) and its selectors
  start with that widget's `objectName` (`#crispPanel ...`), so it cannot leak to host widgets.
- Qt Style Sheet "Unknown property" warnings are treated as failure (capture the Qt message
  handler during validation).
- One shared module is planned at `src/CRISPy/style.py` (Qt, via `qtpy`): `is_dark(palette)`,
  `role_color(role, palette) -> QColor`, `scaled_font(base, k, bold)`, spacing tokens, and
  `panel_stylesheet(palette)` that builds one scoped string. It is **not created in this run**;
  this document is the specification for it. Only helpers used in two or more places are added.
- Re-style on `QEvent.PaletteChange` and `FontChange` only; set a stylesheet or property only
  when its value changed.
- Qt-free modules stay Qt-free: `style.py` is imported only by `ui.py`, `plot.py`, `plugin.py`,
  `spot/panel.py`, `spot/live_view.py`.

Platform caveat: the rig is Windows (Segoe UI 9 pt, panel min width 304 px, the user's earlier measurement, not re-verified); the audit and mockups
were rendered on Linux (Sans Serif 9 pt; minimum width 354 / 515 / 371 px, see section 5). Glyph widths, line height and the default style
(Windows vs Fusion) differ, so the type and spacing scale is stated relative to the host font and
must be verified on the rig later (read-only visual check; not part of this run).

## 11. Validation checklist

1. `uv run pytest -q` green (logic modules intact).
2. Widget-state dump (`isEnabled`/`isVisible` of all named widgets across idle, locked, MDA (no widget
   disabled; only the poller stops), tool running, spot lock active, and Readouts paused with its "(paused: ...)" title text) identical to the baseline; no `connect`, `Thread`, poller or gating
   line changed in the diff.
3. No Qt "Unknown property" or stylesheet parse warnings in the captured output.
4. `minimumSizeHint()` width of the panel not larger than the recorded baseline at the same
   font scale (`min_sizes.json`; at 100%: piezo 354 collapsed / 515 sections open, stage 371;
   426/569 and 446 at 125%; 500/631 and 525 at 150%). The 304 px rig figure is the user's
   earlier measurement, not re-verified.
5. Screenshots, before and after at identical sizes: panel 460x700 and 600x900; two-axis
   1000x700 and 1400x900; fonts 100/125/150% (harness `QApplication.setFont` only); piezo with
   Spot lock, stage, two axes; light and dark palettes.
6. Each state of section 6 shown: focus, disabled (tool running, spot lock active; MDA disables no widget), error
   (LED error label, failed read), each active tool (Dither, Focus curve, Hold test, Spot lock).
7. Greyscale pass: convert the screenshots to grey; every status is still readable by its glyph
   or word.
8. Contrast: role text at 4.5:1 or better against the actual window colour (3:1 for the hero
   number and bold dark `error`).
9. Both axes visible in the two-device layout; no new fixed widths (`grep setFixed`).
10. Items that could not be exercised in the harness are listed by name as unvalidated.
