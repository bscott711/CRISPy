"""Opt-in "Bench" theme (dark primary, light secondary) for CRISPy.

CRISPy never applies this itself (the host owns the look); call
:func:`apply_theme` from a host application or the demo.  Colour carries the status:
Idle = text3, Engaged/Polling = accent, In Focus/Locked = ok, Calibrating = warn,
Fault = bad.  Widgets opt in through dynamic properties (``primary``, ``role``,
``state``, ``badge``, ``tone``); call :func:`refresh` after changing one on a shown
widget.

Type ledger: a plain QLabel is a field label (text2); ``role="value"`` is a mono 600
readout; ``role`` ``header`` / ``micro`` / ``hint`` / ``unit`` as named.  Border ledger:
panels ``line``, interactive controls ``line_strong``, focus ``accent``, disabled none,
pills none (tint fill only).  Mono only for machine numbers.
"""

from __future__ import annotations

import pathlib
import tempfile
from string import Template
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qtpy.QtGui import QIcon
    from qtpy.QtWidgets import QApplication, QLayout, QWidget

DARK = dict(
    bg="#0F1216", surface1="#151A20", surface2="#1C232B", surface3="#242E38",
    line="#28313B", line_strong="#39444F", hairline="#202832",
    text="#E7EDF3", text2="#9DAAB6", text3="#616D79",
    accent="#38BDF8", accent_hover="#7DD3FC", accent_dim="rgba(56,189,248,0.14)",
    on_accent="#04263A", trace2="#A78BFA", trace2_dim="rgba(167,139,250,0.14)",
    ok="#3ECF8E", ok_dim="rgba(62,207,142,0.14)",
    warn="#F5B841", warn_dim="rgba(245,184,65,0.14)",
    bad="#E5484D", bad_dim="rgba(229,72,77,0.14)",
    idle_dim="rgba(97,109,121,0.18)",
)  # fmt: skip

LIGHT = dict(
    bg="#F3F5F7", surface1="#FBFCFD", surface2="#FFFFFF", surface3="#F0F3F5",
    line="#E1E6EA", line_strong="#C9D2DA", hairline="#EDF0F3",
    text="#151A20", text2="#5A6672", text3="#8A95A0",
    accent="#0284C7", accent_hover="#0369A1", accent_dim="rgba(2,132,199,0.10)",
    on_accent="#FFFFFF", trace2="#7C3AED", trace2_dim="rgba(124,58,237,0.10)",
    ok="#16A34A", ok_dim="rgba(22,163,74,0.12)",
    warn="#B45309", warn_dim="rgba(180,83,9,0.12)",
    bad="#DC2626", bad_dim="rgba(220,38,38,0.12)",
    idle_dim="rgba(138,149,160,0.18)",
)  # fmt: skip

THEMES = {"dark": DARK, "light": LIGHT}

SANS = '"Inter", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
MONO = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'

_SVG = dict(
    check='<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><path d="M2.6 6.3 5 8.7 9.4 3.9" fill="none" stroke="{ink}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',  # noqa: E501
    chev_u='<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6"><path d="M1 4.6 5 1.2 9 4.6" fill="none" stroke="{ink}" stroke-width="1.4" stroke-linecap="round"/></svg>',  # noqa: E501
    chev_d='<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6"><path d="M1 1.4 5 4.8 9 1.4" fill="none" stroke="{ink}" stroke-width="1.4" stroke-linecap="round"/></svg>',  # noqa: E501
    dot='<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><circle cx="6" cy="6" r="3" fill="{ink}"/></svg>',  # noqa: E501
    caret_r='<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M3.4 1.6 7 5 3.4 8.4" fill="none" stroke="{ink}" stroke-width="1.4" stroke-linecap="round"/></svg>',  # noqa: E501
    caret_d='<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M1.6 3.4 5 7 8.4 3.4" fill="none" stroke="{ink}" stroke-width="1.4" stroke-linecap="round"/></svg>',  # noqa: E501
)

# Disclosure carets are icons (QSS cannot restyle a tool button's arrowType), so they
# use one mid-grey that reads on both the dark and the light surfaces.
_CARET_INK = "#7B8794"


def _assets(t: dict[str, str]) -> dict[str, str]:
    """Write the SVG glyphs for this palette and return their QSS urls."""
    d = pathlib.Path(tempfile.gettempdir()) / "crispy_theme"
    d.mkdir(exist_ok=True)

    def w(name: str, ink: str) -> str:
        p = d / f"{name}.svg"
        p.write_text(_SVG[name].format(ink=ink))
        return p.as_posix()

    return dict(
        check=w("check", t["on_accent"]),
        chev_u=w("chev_u", t["text2"]),
        chev_d=w("chev_d", t["text2"]),
        dot=w("dot", t["accent"]),
    )


# QSS font sizes are whole pixels (12.5px renders as 13px), so use integers.
# Qt does not clamp an oversized radius (999px draws square), so a pill is half its
# height: 11 px font (about 15 px line) + 4 px padding either side = 23 px.
_PILL = "border-radius: 11px; padding: 4px 12px; font-size: 11px; font-weight: 600;"

QSS = Template(
    """
* { font-family: $sans; font-size: 13px; outline: 0; }
QWidget { background: $bg; color: $text; }
QLabel { background: transparent; border: none; color: $text2; font-size: 13px; }
QLabel[role="header"] { font-size: 15px; font-weight: 600; color: $text; }
QLabel[role="micro"]  { font-size: 11px; font-weight: 600; color: $text3; }
QLabel[role="label"]  { color: $text2; }
QLabel[role="value"]  { font-family: $mono; font-weight: 600; color: $text; }
QLabel[role="unit"]   { font-family: $mono; font-size: 11px; color: $text3; }
QLabel[role="hint"]   { font-size: 12px; color: $text3; }
QLabel[role="read"]   { color: $text; font-weight: 500; }
QLabel[state="idle"] { color: $text3; background: $idle_dim; $pill }
QLabel[state="busy"] { color: $warn; background: $warn_dim; $pill }
QLabel[state="engaged"] { color: $accent; background: $accent_dim; $pill }
QLabel[state="ok"]   { color: $ok;   background: $ok_dim;   $pill }
QLabel[state="warn"] { color: $warn; background: $warn_dim; $pill }
QLabel[state="bad"]  { color: $bad;  background: $bad_dim;  $pill }
QLabel[badge="piezo"]   { color: $trace2; background: $trace2_dim; $pill }
QLabel[badge="stage"]   { color: $accent; background: $accent_dim; $pill }
QLabel[badge="unknown"] { color: $text3; background: $idle_dim; $pill }
QLabel[tone="ok"]   { color: $ok; }
QLabel[tone="warn"] { color: $warn; }
QLabel[tone="bad"]  { color: $bad; }
QLabel[tone="idle"] { color: $text3; }

QGroupBox { background: $surface1; border: 1px solid $line; border-radius: 10px;
    margin-top: 0; padding: 14px; }
QGroupBox::title { color: transparent; background: transparent; border: none;
    padding: 0; }
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton { background: transparent; }
QFrame[role="divider"] { background: $hairline; border: none; max-height: 1px; }

QPushButton { background: $surface2; border: 1px solid $line_strong; border-radius: 6px;
    color: $text; font-size: 13px; font-weight: 500; min-height: 30px; max-height: 30px;
    padding: 0 14px; }
QPushButton:hover   { background: $surface3; }
QPushButton:pressed { background: $surface2; }
QPushButton:focus   { border-color: $accent; }
QPushButton:checked { background: $accent_dim; border-color: $accent; color: $accent; }
QPushButton:disabled { background: $surface1; border-color: transparent; color: $text3; }
QPushButton[hasmenu="true"] { padding: 0 28px 0 14px; }
QPushButton::menu-indicator { image: url($chev_d); width: 10px; height: 6px;
    subcontrol-origin: padding; subcontrol-position: center right; right: 10px; }
QPushButton[role="tab"] { background: transparent; border: none; border-radius: 0;
    border-bottom: 2px solid transparent; color: $text2; padding: 0 14px; }
QPushButton[role="tab"]:hover { background: transparent; color: $text; }
QPushButton[role="tab"]:checked { background: transparent; color: $text;
    border-bottom: 2px solid $accent; }
QPushButton[primary="true"] { background: $accent; border-color: $accent;
    color: $on_accent; font-weight: 600; }
QPushButton[primary="true"]:hover { background: $accent_hover;
    border-color: $accent_hover; }
QPushButton[primary="true"]:disabled { background: $surface1; border-color: transparent;
    color: $text3; }

QToolButton { background: transparent; border: none; border-radius: 6px; color: $text2;
    padding: 4px 8px; font-weight: 500; }
QToolButton:hover { background: $surface3; color: $text; }
QToolButton[role="section"] { color: $text2; font-weight: 600; padding: 6px 4px; }
QToolButton[role="section"]:hover { background: transparent; color: $text; }

QSpinBox, QDoubleSpinBox, QLineEdit { background: $surface2;
    border: 1px solid $line_strong; border-radius: 6px; color: $text; font-family: $mono;
    font-size: 13px; min-height: 30px; max-height: 30px; padding: 0 24px 0 10px;
    selection-background-color: $accent_dim; selection-color: $text; }
QLineEdit { padding: 0 10px; }
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus { border-color: $accent; }
QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled { background: $surface1;
    border-color: transparent; color: $text3; }
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 18px; border: none; background: transparent; subcontrol-origin: border; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url($chev_u); width: 10px;
    height: 6px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url($chev_d); width: 10px;
    height: 6px; }

QPlainTextEdit, QTextEdit { background: $surface2; border: 1px solid $line_strong;
    border-radius: 6px; padding: 6px 10px; color: $text;
    selection-background-color: $accent_dim; selection-color: $text; }
QPlainTextEdit:focus, QTextEdit:focus { border-color: $accent; }

QCheckBox, QRadioButton { spacing: 8px; color: $text; font-size: 13px;
    background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid $line_strong; background: $surface2; }
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent;
    image: url($check); }
QCheckBox::indicator:disabled { background: $surface1; border-color: $line; }
QCheckBox:disabled, QRadioButton:disabled { color: $text3; }
QRadioButton::indicator { width: 16px; height: 16px; border-radius: 8px;
    border: 1px solid $line_strong; background: $surface2; }
QRadioButton::indicator:hover { border-color: $accent; }
QRadioButton::indicator:checked { border-color: $accent; image: url($dot); }

QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: $surface3; }
QSlider::sub-page:horizontal { background: $accent; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; height: 12px; margin: -4px 0;
    border-radius: 6px; background: $accent; border: none; }
QSlider::handle:horizontal:hover { background: $accent_hover; }
QSlider::groove:horizontal:disabled, QSlider::sub-page:horizontal:disabled {
    background: $surface2; }

QComboBox { background: $surface2; border: 1px solid $line_strong; border-radius: 6px;
    color: $text; font-size: 13px; font-weight: 500; min-height: 30px; max-height: 30px;
    padding: 0 12px; }
QComboBox:hover { background: $surface3; }
QComboBox:focus { border-color: $accent; }
QComboBox:disabled { background: $surface1; border-color: transparent; color: $text3; }
QComboBox::drop-down { width: 22px; border: none; subcontrol-origin: border;
    subcontrol-position: top right; }
QComboBox::down-arrow { image: url($chev_d); width: 10px; height: 6px; }
QComboBox QAbstractItemView { background: $surface2; border: 1px solid $line_strong;
    border-radius: 6px; padding: 4px; color: $text;
    selection-background-color: $accent_dim; selection-color: $text; outline: 0; }
QComboBox QAbstractItemView::item { min-height: 26px; border-radius: 4px;
    padding: 0 8px; }
QComboBox QAbstractItemView::item:selected { background: $accent_dim; color: $text; }

QTabWidget::pane { border: 1px solid $line; border-radius: 8px; background: $surface1; }
QTabBar::tab { background: transparent; color: $text2; padding: 6px 14px;
    border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: $text; border-bottom-color: $accent; }
QTabBar::tab:hover { color: $text; }

QTableWidget { background: $surface1; border: 1px solid $line; border-radius: 6px;
    gridline-color: $hairline; selection-background-color: $accent_dim;
    selection-color: $text; }
QHeaderView::section { background: $surface1; color: $text2; border: none;
    border-bottom: 1px solid $line; padding: 4px 8px; font-size: 11px;
    font-weight: 600; }
QProgressBar { background: $surface2; border: 1px solid $line; border-radius: 6px;
    text-align: center; color: $text; }
QProgressBar::chunk { background: $accent; border-radius: 5px; }

QDockWidget { background: $bg; }
QDockWidget::title { background: $surface1; padding: 8px 12px; border: none;
    border-bottom: 1px solid $line; color: $text2; font-size: 12px; font-weight: 600; }
QDockWidget::close-button, QDockWidget::float-button { background: transparent;
    border: none; }
QMainWindow::separator { background: $line; width: 1px; height: 1px; }

QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { width: 10px; background: transparent; }
QScrollBar:horizontal { height: 10px; background: transparent; }
QScrollBar::handle:vertical { background: $line_strong; border-radius: 5px;
    min-height: 24px; }
QScrollBar::handle:horizontal { background: $line_strong; border-radius: 5px;
    min-width: 24px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background: $text3; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: $line; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QToolTip { background: $surface3; color: $text; border: 1px solid $line_strong;
    border-radius: 6px; padding: 6px 8px; }
#cameraView { background: #000000; border: 1px solid $line; border-radius: 8px; }
"""
)


def build_qss(tokens: dict[str, str]) -> str:
    """Return the stylesheet text for ``tokens`` (writes the SVG glyph files)."""
    return QSS.substitute(
        {**tokens, **_assets(tokens), "pill": _PILL, "sans": SANS, "mono": MONO}
    )


def apply_theme(app: QApplication, tokens: dict[str, str] = DARK) -> None:
    """Install the Bench stylesheet application-wide."""
    app.setStyleSheet(build_qss(tokens))


def caret_icon(expanded: bool) -> QIcon:
    """The disclosure caret (down when ``expanded``, else right) as a QIcon."""
    from qtpy.QtGui import QIcon

    d = pathlib.Path(tempfile.gettempdir()) / "crispy_theme"
    d.mkdir(exist_ok=True)
    name = "caret_d" if expanded else "caret_r"
    p = d / f"{name}.svg"
    p.write_text(_SVG[name].format(ink=_CARET_INK))
    return QIcon(p.as_posix())


def set_role(w: QWidget, role: str) -> None:
    """Set a QLabel's ``role`` property, re-polishing only if it changed."""
    if w.property("role") != role:
        w.setProperty("role", role)
        refresh(w)


def set_readout(val_lb: QWidget, text: str, unit: str = "") -> None:
    """Set a readout's value and its dim unit label (``val_lb.unit_lbl``)."""
    val_lb.setText(text)  # type: ignore[attr-defined]
    unit_lb = getattr(val_lb, "unit_lbl", None)
    if unit_lb is not None:
        unit_lb.setText(unit)
        unit_lb.setVisible(bool(unit))


def readout_pair(role_name: str = "value") -> tuple[QWidget, QWidget, QLayout]:
    """A right-aligned ``value`` + ``unit`` label pair in an hbox (4 px apart)."""
    from qtpy.QtWidgets import QHBoxLayout, QLabel

    val, unit = QLabel("--"), QLabel()
    val.setProperty("role", role_name)
    unit.setProperty("role", "unit")
    unit.hide()
    val.unit_lbl = unit  # type: ignore[attr-defined]
    hb = QHBoxLayout()
    hb.setContentsMargins(0, 0, 0, 0)
    hb.setSpacing(4)
    hb.addStretch(1)
    hb.addWidget(val)
    hb.addWidget(unit)
    return val, unit, hb


def refresh(w: QWidget) -> None:
    """Re-polish ``w`` after a dynamic property changed."""
    w.style().unpolish(w)
    w.style().polish(w)


def micro(text: str) -> QWidget:
    """In-panel section header: uppercase, letter-spaced, text3 (replaces box titles)."""
    from qtpy.QtGui import QFont
    from qtpy.QtWidgets import QLabel

    lb = QLabel(text.upper())
    f = lb.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
    lb.setFont(f)
    lb.setProperty("role", "micro")
    return lb


def set_state_pill(label: QWidget, kind: str) -> None:
    """Colour a QLabel as a status pill: idle/engaged/ok/busy/warn/bad."""
    label.setProperty("state", kind)
    refresh(label)


def set_tone(label: QWidget, tone: str) -> None:
    """Colour a QLabel's text only: ok/warn/bad/idle (empty string clears it)."""
    label.setProperty("tone", tone)
    refresh(label)
