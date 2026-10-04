"""Start screen listing previously opened books."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .db import Database
from .theme import icon, rounded, theme

BOOK_ROLE = Qt.ItemDataRole.UserRole + 1
HEADER_ROLE = Qt.ItemDataRole.UserRole + 2  # section headers carry (label, expanded)
CARD_HEIGHT = 92
HEADER_HEIGHT = 44
MAX_WIDTH = 900
FINISHED_COLLAPSED = "library_finished_collapsed"


class _BookCard(QStyledItemDelegate):
    """Title, chats and last-opened date, and a reading-progress bar; or a section header."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), HEADER_HEIGHT if index.data(HEADER_ROLE) else CARD_HEIGHT)

    def paint(self, p: QPainter, option, index) -> None:
        if header := index.data(HEADER_ROLE):
            self._paint_header(p, option, *header)
            return
        b = index.data(BOOK_ROLE)
        c = theme().colors
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and b["exists"]
        card = rounded(QRectF(option.rect).adjusted(0, 4, 0, -4))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c["accent"] if hover else c["border"]), 1))
        p.setBrush(QColor(c["raised"]))
        p.drawRoundedRect(card, 10, 10)

        # Format tile, shaped like a small book cover.
        tile = QRectF(card.left() + 16, card.top() + 14, 42, card.height() - 28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c["accent_soft"] if b["exists"] else c["hover"]))
        p.drawRoundedRect(tile, 5, 5)
        small = QFont(option.font)
        small.setPointSizeF(small.pointSizeF() * 0.8)
        small.setWeight(QFont.Weight.Bold)
        p.setFont(small)
        p.setPen(QColor(c["accent"] if b["exists"] else c["faint"]))
        p.drawText(tile, Qt.AlignmentFlag.AlignCenter, b["format"])

        x, right = tile.right() + 16, card.right() - 18
        width = right - x
        title = QFont(option.font)
        title.setPointSizeF(title.pointSizeF() * 1.1)
        title.setWeight(QFont.Weight.DemiBold)
        p.setFont(title)
        p.setPen(QColor(c["fg"] if b["exists"] else c["faint"]))
        text = QFontMetrics(title).elidedText(b["title"], Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QRectF(x, card.top() + 13, width, 24), Qt.AlignmentFlag.AlignVCenter, text)

        meta = QFont(option.font)
        meta.setPointSizeF(meta.pointSizeF() * 0.9)
        p.setFont(meta)
        line = QRectF(x, card.top() + 37, width, 18)
        if b["exists"]:
            p.setPen(QColor(c["muted"]))
            p.drawText(line, Qt.AlignmentFlag.AlignVCenter, b["detail"])
            if b["finished"]:
                progress, label = 1.0, "Finished"
            else:
                progress = (b["last_page"] + 1) / max(b["page_count"], 1)
                label = f"p. {b['last_page'] + 1} of {b['page_count']}"
            bar = QRectF(x, card.bottom() - 21, min(width - 120, 320), 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["hover"]))
            p.drawRoundedRect(bar, 2, 2)
            p.setBrush(QColor(c["accent"]))
            p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(bar.width() * progress, 4), bar.height()), 2, 2)
            p.setPen(QColor(c["muted"]))
            p.drawText(QRectF(bar.right() + 12, bar.center().y() - 9, 120, 18), Qt.AlignmentFlag.AlignVCenter, label)
        else:
            p.setPen(QColor(c["error"]))
            missing = QFontMetrics(meta).elidedText(f"File not found: {b['path']}", Qt.TextElideMode.ElideMiddle, int(width))
            p.drawText(line, Qt.AlignmentFlag.AlignVCenter, missing)
        p.restore()

    def _paint_header(self, p: QPainter, option, label: str, expanded: bool) -> None:
        c = theme().colors
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        r = QRectF(option.rect)
        p.save()
        chevron = icon("chevron-down" if expanded else "chevron-right", "fg" if hover else "muted")
        chevron.paint(p, int(r.left() + 2), int(r.center().y() - 8 + 4), 16, 16)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(c["fg"] if hover else c["muted"]))
        p.drawText(QRectF(r.left() + 24, r.top() + 4, r.width() - 24, r.height() - 4), Qt.AlignmentFlag.AlignVCenter, label)
        p.restore()


class LibraryView(QWidget):
    openRequested = Signal(str)  # file path
    browseRequested = Signal()
    webRequested = Signal()
    refetchRequested = Signal(str)  # source URL of a web snapshot

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName("library")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        title = QLabel("Library")
        title.setProperty("role", "heading")
        self.count = QLabel()
        self.count.setProperty("role", "muted")
        browse = QPushButton("Open PDF or EPUB…")
        browse.setProperty("primary", True)
        browse.setIcon(icon("folder-open", "accent_fg"))
        browse.clicked.connect(self.browseRequested)
        top = QHBoxLayout()
        top.addWidget(title)
        top.addSpacing(8)
        top.addWidget(self.count)
        web = QPushButton("Open web page…")
        web.setIcon(icon("globe"))
        web.clicked.connect(self.webRequested)
        top.addStretch()
        top.addWidget(web)
        top.addWidget(browse)

        self.list = QListWidget(objectName="library")
        self.list.setItemDelegate(_BookCard(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        # Cards size to the viewport, so there is never anything to scroll sideways.
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list.itemClicked.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)

        self.empty = QWidget()
        self.empty_icon = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("No books yet", alignment=Qt.AlignmentFlag.AlignCenter)
        empty_title.setProperty("role", "heading")
        empty_text = QLabel(
            "Open a PDF, EPUB or web page to start reading. Select any passage to ask Claude about it.",
            alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True,
        )
        empty_text.setProperty("role", "muted")
        col = QVBoxLayout(self.empty)
        col.addStretch(2)
        for w in (self.empty_icon, empty_title, empty_text):
            col.addWidget(w)
        col.addStretch(3)

        body = QWidget()
        body.setMaximumWidth(MAX_WIDTH)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty, 1)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 16)
        outer.addWidget(body)

        theme().changed.connect(self._on_theme_changed)
        self._on_theme_changed()
        self.refresh()

    def _on_theme_changed(self) -> None:
        self.empty_icon.setPixmap(icon("library", "faint").pixmap(QSize(56, 56), self.devicePixelRatioF()))

    def refresh(self) -> None:
        self.list.clear()
        books = self.db.recent_books()
        reading = [b for b in books if b["finished_at"] is None]
        finished = sorted((b for b in books if b["finished_at"] is not None), key=lambda b: -b["finished_at"])
        for b in reading:
            self._add_book(b)
        if finished:
            expanded = self.db.get_setting(FINISHED_COLLAPSED) != "1"
            header = QListWidgetItem()
            header.setData(HEADER_ROLE, (f"Finished · {len(finished)}", expanded))
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setToolTip("Hide finished books" if expanded else "Show finished books")
            self.list.addItem(header)
            if expanded:
                for b in finished:
                    self._add_book(b)
        self.count.setText(f"{len(books)} book{'s' * (len(books) != 1)}" if books else "")
        self.list.setVisible(bool(books))
        self.empty.setVisible(not books)

    def _add_book(self, b) -> None:
        chats = len(self.db.chats(b["id"]))
        day = lambda t: time.strftime("%d %b %Y", time.localtime(t)).lstrip("0")
        when = f"finished {day(b['finished_at'])}" if b["finished_at"] else f"opened {day(b['opened_at'])}"
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, b["id"])
        ext = os.path.splitext(b["path"])[1].lstrip(".").upper()[:4] or "BOOK"
        item.setData(BOOK_ROLE, {
            "title": b["title"], "path": b["path"], "exists": os.path.exists(b["path"]),
            "format": "WEB" if b["source_url"] else ext,
            "last_page": b["last_page"], "page_count": b["page_count"],
            "finished": b["finished_at"] is not None,
            "detail": f"{chats} chat{'s' * (chats != 1)} · {when}",
        })
        item.setToolTip(b["source_url"] or b["path"])
        self.list.addItem(item)

    def _activate(self, item: QListWidgetItem) -> None:
        if header := item.data(HEADER_ROLE):
            self.db.set_setting(FINISHED_COLLAPSED, "1" if header[1] else "0")
            self.refresh()
            return
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        if book and os.path.exists(book["path"]):
            self.openRequested.emit(book["path"])

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None or item.data(HEADER_ROLE):
            return
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        finished = book["finished_at"] is not None
        finish = menu.addAction(icon("check-circle"), "Move back to reading" if finished else "Mark as finished")
        refetch = menu.addAction(icon("globe"), "Fetch a fresh copy") if book["source_url"] else None
        menu.addSeparator()
        remove = menu.addAction("Remove from library (deletes its chats)")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen is finish:
            self.db.set_finished(book["id"], not finished)
            self.refresh()
        elif chosen is remove:
            self.db.delete_book(book["id"])
            self.refresh()
        elif refetch is not None and chosen is refetch:
            self.refetchRequested.emit(book["source_url"])
