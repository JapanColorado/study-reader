"""The navigation rail down the left of the main window: Home, Library, Review, Cards and
Statistics, the open book, and Settings."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QButtonGroup, QMenu, QToolButton, QVBoxLayout, QWidget

from .theme import icon, theme

RAIL_WIDTH = 76

# (page name, label, icon, tooltip)
PAGES = [
    ("home", "Home", "home", "Home: today's review, what you're reading, your decks (Alt+1)"),
    ("library", "Library", "library", "Every book and web page (Alt+2)"),
    ("review", "Review", "cards", "Review cards from every deck and book (Alt+3)"),
    ("cards", "Cards", "search", "Browse, search and edit cards (Alt+4)"),
    ("stats", "Stats", "chart", "Review statistics (Alt+5)"),
]


class NavButton(QToolButton):
    """A rail entry: icon over label, with an optional count badge in the corner."""

    def __init__(self, label: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("navItem")
        self.setText(label)
        self.setIcon(icon(icon_name, "nav"))
        self.setIconSize(QSize(21, 21))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(RAIL_WIDTH - 12, 58)
        self.badge = ""

    def set_badge(self, text: str) -> None:
        if text != self.badge:
            self.badge = text
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.badge:
            return
        c = theme().colors
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() * 0.78, 7))
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        w = max(QFontMetrics(font).horizontalAdvance(self.badge) + 10, 18)
        r = QRectF(self.width() / 2 + 3, 4, w, 17)
        p.setPen(QColor(c["rail"]))
        p.setBrush(QColor(c["accent"]))
        p.drawRoundedRect(r, 8.5, 8.5)
        p.setPen(QColor(c["accent_fg"]))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.badge)
        p.end()


class NavRail(QWidget):
    """pageRequested(name): one of PAGES, or "book" for the open book."""

    pageRequested = Signal(str)

    def __init__(self, settings_menu: QMenu, parent=None):
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedWidth(RAIL_WIDTH)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, NavButton] = {}
        col = QVBoxLayout(self)
        col.setContentsMargins(6, 10, 6, 10)
        col.setSpacing(4)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        for name, label, icon_name, tip in PAGES:
            self._add(col, name, label, icon_name, tip)
        col.addSpacing(10)
        self.book = self._add(col, "book", "Book", "book-open", "Back to the open book")
        self.book.hide()
        col.addStretch()
        self.settings = QToolButton(objectName="navItem")
        self.settings.setText("Settings")
        self.settings.setIcon(icon("settings", "nav"))
        self.settings.setIconSize(QSize(20, 20))
        self.settings.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.settings.setFixedSize(RAIL_WIDTH - 12, 54)
        self.settings.setToolTip("Settings")
        self.settings.setMenu(settings_menu)
        self.settings.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        col.addWidget(self.settings)

    def _add(self, col: QVBoxLayout, name: str, label: str, icon_name: str, tip: str) -> NavButton:
        b = NavButton(label, icon_name)
        b.setToolTip(tip)
        b.clicked.connect(lambda _=False, n=name: self.pageRequested.emit(n))
        self.group.addButton(b)
        self.buttons[name] = b
        col.addWidget(b)
        return b

    def set_current(self, name: str) -> None:
        if b := self.buttons.get(name):
            b.setChecked(True)

    def set_book(self, title: str | None) -> None:
        """The open book's entry: shown with its title as tooltip, or hidden with no book open."""
        self.book.setVisible(title is not None)
        if title is not None:
            self.book.setToolTip(f"Back to {title} (Alt+6)")

    def set_review_badge(self, waiting: int) -> None:
        self.buttons["review"].set_badge("" if not waiting else "99+" if waiting > 99 else str(waiting))
