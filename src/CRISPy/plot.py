"""A minimal QPainter line plot for the diagnostic tools (no plotting dependency)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from qtpy.QtWidgets import QSizePolicy, QWidget

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qtpy.QtGui import QPaintEvent

_MARGIN_L, _MARGIN_R, _MARGIN_T, _MARGIN_B = 46, 10, 8, 30


@dataclass
class Series:
    """One line (or set of dots) to draw."""

    xs: Sequence[float]
    ys: Sequence[float]
    color: str
    width: float = 1.6
    dots: bool = False


@dataclass
class RefLine:
    """A horizontal or vertical reference line."""

    value: float
    color: str
    label: str = ""
    dashed: bool = True
    label_left: bool = False  # put a horizontal line's label at the left end
    label_bottom: bool = False  # put a vertical line's label at the bottom


@dataclass
class _Frame:
    series: list[Series] = field(default_factory=list)
    hlines: list[RefLine] = field(default_factory=list)
    vlines: list[RefLine] = field(default_factory=list)
    markers: list[tuple[float, float, str]] = field(default_factory=list)
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round-numbered ticks (1, 2 or 5 × 10^k apart) covering ``[lo, hi]``."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    ticks = []
    v = first
    while v <= hi + step * 1e-9:
        ticks.append(0.0 if abs(v) < step * 1e-9 else v)
        v += step
    return ticks


def _fmt(v: float, step: float) -> str:
    decimals = max(0, -math.floor(math.log10(step))) if step > 0 else 0
    if abs(v) >= 10000:
        return f"{v:.3g}"
    return f"{v:.{min(decimals, 3)}f}"


class LinePlot(QWidget):
    """Lines, reference lines and point markers on auto-ranged axes.

    Parameters
    ----------
    x_label, y_label
        Axis titles.
    min_height
        Minimum widget height in pixels.
    parent
        Optional parent widget.
    """

    def __init__(
        self,
        *,
        x_label: str = "",
        y_label: str = "",
        min_height: int = 150,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._x_label = x_label
        self._y_label = y_label
        self._frame = _Frame()
        self.setMinimumHeight(min_height)
        # Expanding both ways, so the plot (not the labels around it) takes spare room.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(
        self,
        series: Sequence[Series] = (),
        *,
        hlines: Sequence[RefLine] = (),
        vlines: Sequence[RefLine] = (),
        markers: Sequence[tuple[float, float, str]] = (),
        x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
    ) -> None:
        """Replace what is drawn. Ranges default to the data plus reference lines."""
        self._frame = _Frame(
            list(series), list(hlines), list(vlines), list(markers), x_range, y_range
        )
        self.update()

    def clear(self) -> None:
        self.set_data()

    def set_y_label(self, text: str) -> None:
        """Change the y-axis title."""
        if text != self._y_label:
            self._y_label = text
            self.update()

    # ------------------------------------------------------------------ ranges
    def _ranges(self) -> tuple[float, float, float, float] | None:
        f = self._frame
        xs = [x for s in f.series for x in s.xs] + [m[0] for m in f.markers]
        xs += [v.value for v in f.vlines]
        ys = [y for s in f.series for y in s.ys] + [m[1] for m in f.markers]
        ys += [h.value for h in f.hlines]
        xs = [x for x in xs if math.isfinite(x)]
        ys = [y for y in ys if math.isfinite(y)]
        if f.x_range is not None:
            x0, x1 = f.x_range
        elif xs:
            x0, x1 = min(xs), max(xs)
        else:
            return None
        if f.y_range is not None:
            y0, y1 = f.y_range
        elif ys:
            y0, y1 = min(ys), max(ys)
            pad = (y1 - y0) * 0.06
            y0, y1 = y0 - pad, y1 + pad
        else:
            return None
        if x1 <= x0:
            x0, x1 = x0 - 1, x1 + 1
        if y1 <= y0:
            y0, y1 = y0 - 1, y1 + 1
        return x0, x1, y0, y1

    # ------------------------------------------------------------------- paint
    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        grid = QColor(fg)
        grid.setAlpha(40)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))

        plot = QRectF(
            _MARGIN_L,
            _MARGIN_T,
            max(10, self.width() - _MARGIN_L - _MARGIN_R),
            max(10, self.height() - _MARGIN_T - _MARGIN_B),
        )
        rng = self._ranges()
        p.setPen(QPen(fg, 1))
        if rng is None:
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "no data")
            p.end()
            return
        x0, x1, y0, y1 = rng

        def px(x: float) -> float:
            return plot.left() + (x - x0) / (x1 - x0) * plot.width()

        def py(y: float) -> float:
            return plot.bottom() - (y - y0) / (y1 - y0) * plot.height()

        font = p.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        p.setFont(font)
        xt, yt = nice_ticks(x0, x1, 5), nice_ticks(y0, y1, 4)
        xstep = xt[1] - xt[0] if len(xt) > 1 else 1.0
        ystep = yt[1] - yt[0] if len(yt) > 1 else 1.0
        for x in xt:
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(px(x), plot.top()), QPointF(px(x), plot.bottom()))
            p.setPen(QPen(fg, 1))
            p.drawText(
                QRectF(px(x) - 30, plot.bottom() + 2, 60, 14),
                Qt.AlignmentFlag.AlignHCenter,
                _fmt(x, xstep),
            )
        for y in yt:
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(plot.left(), py(y)), QPointF(plot.right(), py(y)))
            p.setPen(QPen(fg, 1))
            p.drawText(
                QRectF(0, py(y) - 7, _MARGIN_L - 4, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt(y, ystep),
            )
        p.setPen(QPen(fg, 1))
        p.drawRect(plot)
        if self._x_label:
            p.drawText(
                QRectF(plot.left(), self.height() - 14, plot.width(), 14),
                Qt.AlignmentFlag.AlignHCenter,
                self._x_label,
            )
        if self._y_label:
            p.drawText(
                QRectF(plot.left() + 4, plot.top() + 2, plot.width(), 14),
                Qt.AlignmentFlag.AlignLeft,
                self._y_label,
            )

        p.save()
        p.setClipRect(plot)
        for h in self._frame.hlines:
            pen = QPen(QColor(h.color), 1.2)
            if h.dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(
                QPointF(plot.left(), py(h.value)), QPointF(plot.right(), py(h.value))
            )
            if h.label:
                align = (
                    Qt.AlignmentFlag.AlignLeft
                    if h.label_left
                    else Qt.AlignmentFlag.AlignRight
                )
                p.drawText(
                    QRectF(plot.left() + 4, py(h.value) - 15, plot.width() - 8, 14),
                    align,
                    h.label,
                )
        for v in self._frame.vlines:
            pen = QPen(QColor(v.color), 1.2)
            if v.dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(
                QPointF(px(v.value), plot.top()), QPointF(px(v.value), plot.bottom())
            )
            if v.label:
                y = plot.bottom() - 18 if v.label_bottom else plot.top() + 16
                p.drawText(
                    QRectF(px(v.value) + 3, y, 120, 14),
                    Qt.AlignmentFlag.AlignLeft,
                    v.label,
                )
        for s in self._frame.series:
            pts = [
                QPointF(px(x), py(y))
                for x, y in zip(s.xs, s.ys)
                if math.isfinite(x) and math.isfinite(y)
            ]
            if not pts:
                continue
            color = QColor(s.color)
            if s.dots:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                for pt in pts:
                    p.drawEllipse(pt, 1.6, 1.6)
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                p.setPen(QPen(color, s.width))
                p.drawPolyline(QPolygonF(pts))
        for x, y, c in self._frame.markers:
            p.setPen(QPen(QColor(c), 1.5))
            p.setBrush(QColor(c))
            p.drawEllipse(QPointF(px(x), py(y)), 3.5, 3.5)
        p.restore()
        p.end()
