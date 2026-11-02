"""The Statistics window: stats.py's numbers for a deck, a book or everything, drawn by stats.html."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import stats
from .db import Database
from .notes import LocalPage
from .theme import theme

ASSETS = Path(__file__).parent / "assets"


class StatsWindow(QWidget):
    """A top-level window, or (window=False) the main window's Stats page; the combo box picks
    the scope: all decks, one deck (with the decks under it), or one book's cards."""

    def __init__(self, db: Database, parent=None, window: bool = True):
        super().__init__(parent, Qt.WindowType.Window if window else Qt.WindowType.Widget)
        self.db = db
        self.setWindowTitle("Statistics — Study Reader")
        self.setObjectName("library")  # the window background
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        if window:
            self.resize(1040, 820)

        self.scope = QComboBox()
        self.scope.setMinimumWidth(320)
        self.scope.setMaximumWidth(440)
        self.scope.activated.connect(lambda _: self.refresh())
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Recompute the statistics")
        refresh.clicked.connect(self.refresh)
        title = QLabel("Statistics")
        title.setProperty("role", "title")
        top = QHBoxLayout()
        top.addWidget(title)
        top.addSpacing(16)
        top.addStretch()
        top.addWidget(self.scope)
        top.addWidget(refresh)

        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "stats.html")))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 16)
        layout.setSpacing(14)
        head = QWidget()
        head.setMaximumWidth(1100)  # stats.html's width
        head.setLayout(top)
        top.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(head, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.view, 1)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        self._fill_scopes()  # show_scope() computes the statistics

    # -- page ----------------------------------------------------------------------------
    def _js(self, function: str, *args) -> None:
        js = f"{function}({', '.join(json.dumps(a) for a in args)})"
        if self._ready:
            self.view.page().runJavaScript(js)
        else:
            self._pending.append(js)

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        for js in self._pending:
            self.view.page().runJavaScript(js)
        self._pending.clear()

    def _apply_theme(self) -> None:
        self.view.page().setBackgroundColor(QColor(theme().colors["window"]))
        self._js("setTheme", theme().colors, theme().dark)

    # -- scope ---------------------------------------------------------------------------
    def _fill_scopes(self) -> None:
        """All decks, every deck indented under its parent, then the books that have cards.
        Item data: ("deck", id) / ("book", id) / None."""
        current = self.scope.currentData()
        self.scope.clear()
        self.scope.addItem("All decks", None)
        for d in self.db.decks():
            depth = d["name"].count("::")
            self.scope.addItem("    " * depth + d["name"].rsplit("::", 1)[-1], ("deck", d["id"]))
            self.scope.setItemData(self.scope.count() - 1, d["name"], Qt.ItemDataRole.ToolTipRole)
        books = self.db.conn.execute(
            "SELECT b.id, b.title FROM books b WHERE EXISTS (SELECT 1 FROM cards c WHERE c.book_id=b.id) "
            "ORDER BY b.title COLLATE NOCASE"
        ).fetchall()
        for b in books:
            self.scope.addItem(f"Book: {b['title']}", ("book", b["id"]))
        self._select(current)

    def _select(self, data) -> None:
        for i in range(self.scope.count()):
            if self.scope.itemData(i) == data:
                self.scope.setCurrentIndex(i)
                return
        self.scope.setCurrentIndex(0)

    def show_scope(self, deck_id: int | None = None, book_id: int | None = None) -> None:
        """Pick a deck or a book (neither: all decks) and recompute."""
        self._fill_scopes()
        self._select(("deck", deck_id) if deck_id is not None else ("book", book_id) if book_id is not None else None)
        self.refresh()

    def refresh(self) -> None:
        data = self.scope.currentData()
        kind, scope_id = data if data else (None, None)
        self._js("showStats", stats.compute(
            self.db, deck_id=scope_id if kind == "deck" else None, book_id=scope_id if kind == "book" else None,
        ))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fill_scopes()
