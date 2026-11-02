"""The Home page: today's review, the books being read, decks, recent activity and what's coming
up (dashboard.home), rendered in home.html."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import covers, dashboard
from .db import Database
from .notes import LocalPage
from .theme import theme

ASSETS = Path(__file__).parent / "assets"


class _Bridge(QObject):
    """What home.html calls through the web channel."""

    def __init__(self, view: HomeView):
        super().__init__(view)
        self.view = view

    @Slot(str)
    def start(self, limit: str) -> None:
        self.view.startRequested.emit(limit)

    @Slot(int)
    def open(self, book_id: int) -> None:
        self.view.openRequested.emit(book_id)

    @Slot(int)
    def reviewBook(self, book_id: int) -> None:
        self.view.reviewBookRequested.emit(book_id)

    @Slot(int, bool)
    def reviewDeck(self, deck_id: int, start: bool) -> None:
        self.view.reviewDeckRequested.emit(deck_id, start)

    @Slot(str)
    def go(self, page: str) -> None:
        self.view.pageRequested.emit(page)

    @Slot(str)
    def add(self, what: str) -> None:
        (self.view.webRequested if what == "web" else self.view.browseRequested).emit()


class HomeView(QWidget):
    startRequested = Signal(str)  # review everything: "" or a quick session's limit ("cards" / "time")
    openRequested = Signal(int)  # book id
    reviewBookRequested = Signal(int)  # book id
    reviewDeckRequested = Signal(int, bool)  # deck id, start right away
    pageRequested = Signal(str)  # a navigation rail page
    browseRequested = Signal()  # open a file
    webRequested = Signal()  # open a web page

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.data: dict | None = None  # what was shown last
        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("home", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "home.html")))
        self.setObjectName("library")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        # Counts change as learning cards fall due and the day rolls over.
        self._timer = QTimer(self, interval=60_000)
        self._timer.timeout.connect(lambda: self.isVisible() and self.refresh())
        self._timer.start()

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

    def focus(self) -> None:
        self.view.setFocus()

    def refresh(self) -> None:
        data = dashboard.home(self.db, time.time())
        for b in data["reading"]:
            path = covers.cover({"hash": b["hash"], "path": b["path"]})
            b["cover"] = path.as_uri() if path else ""
        self.data = data
        self._js("show", data)
