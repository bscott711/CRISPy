"""A collapsible titled section, shared by the CRISPy panels."""

from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QToolButton, QVBoxLayout, QWidget


class _Section(QWidget):
    """A titled section whose body shows or hides when the title is clicked.

    Parameters
    ----------
    title
        The clickable title.
    body
        The widget shown when the section is expanded.
    expanded
        Whether the section starts expanded.
    parent
        Optional parent widget.
    """

    def __init__(
        self,
        title: str,
        body: QWidget,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._body = body
        self.toggle_btn = QToolButton()
        self.toggle_btn.setText(title)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_btn.setStyleSheet(
            "QToolButton { border: none; font-weight: bold; }"
        )
        self.toggle_btn.toggled.connect(self._set_expanded)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.toggle_btn)
        v.addWidget(body)
        self.toggle_btn.setChecked(expanded)
        self._set_expanded(expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle_btn.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._body.setVisible(expanded)


Section = _Section
