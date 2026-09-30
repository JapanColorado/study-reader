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
CARD_HEIGHT = 92
MAX_WIDTH = 900


class _BookCard(QStyledItemDelegate):
    """Title, chats and last-opened date, and a reading-progress bar."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), CARD_HEIGHT)

    def paint(self, p: QPainter, option, index) -> None:
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


class LibraryView(QWidget):
    openRequested = Signal(str)  # file path
    browseRequested = Signal()

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
        top.addStretch()
        top.addWidget(browse)

        self.list = QListWidget(objectName="library")
        self.list.setItemDelegate(_BookCard(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list.itemClicked.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)

        self.empty = QWidget()
        self.empty_icon = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("No books yet", alignment=Qt.AlignmentFlag.AlignCenter)
        empty_title.setProperty("role", "heading")
        empty_text = QLabel(
            "Open a PDF or EPUB to start reading. Select any passage to ask Claude about it.",
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
        for b in books:
            chats = len(self.db.chats(b["id"]))
            when = time.strftime("%d %b %Y", time.localtime(b["opened_at"])).lstrip("0")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, b["id"])
            item.setData(BOOK_ROLE, {
                "title": b["title"], "path": b["path"], "exists": os.path.exists(b["path"]),
                "format": os.path.splitext(b["path"])[1].lstrip(".").upper()[:4] or "BOOK",
                "last_page": b["last_page"], "page_count": b["page_count"],
                "detail": f"{chats} chat{'s' * (chats != 1)} · opened {when}",
            })
            item.setToolTip(b["path"])
            self.list.addItem(item)
        self.count.setText(f"{len(books)} book{'s' * (len(books) != 1)}" if books else "")
        self.list.setVisible(bool(books))
        self.empty.setVisible(not books)

    def _activate(self, item: QListWidgetItem) -> None:
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        if book and os.path.exists(book["path"]):
            self.openRequested.emit(book["path"])

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove from library (deletes its chats)")
        if menu.exec(self.list.mapToGlobal(pos)) is remove:
            self.db.delete_book(item.data(Qt.ItemDataRole.UserRole))
            self.refresh()
