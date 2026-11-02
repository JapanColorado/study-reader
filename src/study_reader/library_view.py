"""The Library page: every book and web page, with its cover, reading progress and review cards."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import covers, dashboard
from .db import Database
from .theme import icon, rounded, theme

BOOK_ROLE = Qt.ItemDataRole.UserRole + 1
HEADER_ROLE = Qt.ItemDataRole.UserRole + 2  # section headers carry (label, expanded)
CARD_HEIGHT = 112
HEADER_HEIGHT = 46
MAX_WIDTH = 1040
COVER = QSize(60, 82)
FINISHED_COLLAPSED = "library_finished_collapsed"


def review_rect(card: QRectF) -> QRectF:
    """Where a book's Review button sits on its card."""
    return QRectF(card.right() - 98, card.top() + 16, 80, 28)


def _card_rect(option_rect) -> QRectF:
    return rounded(QRectF(option_rect).adjusted(0, 5, 0, -5))


class _BookCard(QStyledItemDelegate):
    """A book: cover, title, chats, cards and last-opened date, reading progress, and its review
    counts with a Review button; or a section header."""

    def __init__(self, view: LibraryView):
        super().__init__(view.list)
        self.library = view

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), HEADER_HEIGHT if index.data(HEADER_ROLE) else CARD_HEIGHT)

    def paint(self, p: QPainter, option, index) -> None:
        if header := index.data(HEADER_ROLE):
            self._paint_header(p, option, *header)
            return
        b = index.data(BOOK_ROLE)
        c = theme().colors
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and b["exists"]
        card = _card_rect(option.rect)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(QPen(QColor(c["accent"] if hover else c["border"]), 1))
        p.setBrush(QColor(c["raised"]))
        p.drawRoundedRect(card, 12, 12)

        # The cover: the first page, or a tile while it is drawn (or if the file is gone).
        tile = QRectF(card.left() + 16, card.top() + (card.height() - COVER.height()) / 2, COVER.width(), COVER.height())
        pixmap = self.library.cover(b) if b["exists"] else None
        clip = QPainterPath()
        clip.addRoundedRect(tile, 5, 5)
        if pixmap is not None:
            p.save()
            p.setClipPath(clip)
            scaled = pixmap.size().scaled(tile.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            src_w = pixmap.width() * tile.width() / scaled.width()
            src_h = pixmap.height() * tile.height() / scaled.height()
            p.drawPixmap(tile, pixmap, QRectF(0, 0, src_w, src_h))
            p.restore()
            p.setPen(QPen(QColor(c["border"]), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(clip)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["accent_soft"] if b["exists"] else c["hover"]))
            p.drawPath(clip)
            small = QFont(option.font)
            small.setPointSizeF(small.pointSizeF() * 0.8)
            small.setWeight(QFont.Weight.Bold)
            p.setFont(small)
            p.setPen(QColor(c["accent"] if b["exists"] else c["faint"]))
            p.drawText(tile, Qt.AlignmentFlag.AlignCenter, b["format"])

        x = tile.right() + 18
        has_review = b["exists"] and b["due"] + b["new"] > 0
        right = card.right() - (112 if has_review else 18)
        width = right - x
        title = QFont(option.font)
        title.setPointSizeF(title.pointSizeF() * 1.12)
        title.setWeight(QFont.Weight.DemiBold)
        p.setFont(title)
        p.setPen(QColor(c["fg"] if b["exists"] else c["faint"]))
        text = QFontMetrics(title).elidedText(b["title"], Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QRectF(x, card.top() + 16, width, 24), Qt.AlignmentFlag.AlignVCenter, text)

        meta = QFont(option.font)
        meta.setPointSizeF(meta.pointSizeF() * 0.9)
        p.setFont(meta)
        line = QRectF(x, card.top() + 41, width, 18)
        if b["exists"]:
            p.setPen(QColor(c["muted"]))
            p.drawText(line, Qt.AlignmentFlag.AlignVCenter, QFontMetrics(meta).elidedText(
                b["detail"], Qt.TextElideMode.ElideRight, int(width)))
            if b["finished"]:
                label = "Finished"
            else:
                label = f"p. {b['last_page'] + 1} of {b['page_count']}"
            bar = QRectF(x, card.bottom() - 25, min(width - 130, 340), 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["hover"]))
            p.drawRoundedRect(bar, 2.5, 2.5)
            p.setBrush(QColor(c["success"] if b["finished"] else c["accent"]))
            p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(bar.width() * b["progress"], 5), bar.height()), 2.5, 2.5)
            p.setPen(QColor(c["muted"]))
            p.drawText(QRectF(bar.right() + 12, bar.center().y() - 9, 130, 18), Qt.AlignmentFlag.AlignVCenter, label)
        else:
            p.setPen(QColor(c["error"]))
            missing = QFontMetrics(meta).elidedText(f"File not found: {b['path']}", Qt.TextElideMode.ElideMiddle, int(width))
            p.drawText(line, Qt.AlignmentFlag.AlignVCenter, missing)

        if has_review:
            self._paint_review(p, option, card, b, hover)
        p.restore()

    def _paint_review(self, p: QPainter, option, card: QRectF, b: dict, hover: bool) -> None:
        """The due and new pills, and the Review button above them."""
        c = theme().colors
        button = review_rect(card)
        on_button = hover and button.contains(QPointF(self.library.list.viewport().mapFromGlobal(QCursor.pos())))
        p.setPen(QPen(QColor(c["accent"]), 1))
        p.setBrush(QColor(c["accent"] if on_button else c["accent_soft"] if hover else c["raised"]))
        if not hover:
            p.setPen(QPen(QColor(c["border"]), 1))
        p.drawRoundedRect(button, 8, 8)
        f = QFont(option.font)
        f.setPointSizeF(f.pointSizeF() * 0.92)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(c["accent_fg"] if on_button else c["accent"] if hover else c["fg"]))
        p.drawText(button, Qt.AlignmentFlag.AlignCenter, "Review")

        pill_font = QFont(option.font)
        pill_font.setPointSizeF(pill_font.pointSizeF() * 0.82)
        pill_font.setWeight(QFont.Weight.Bold)
        p.setFont(pill_font)
        fm = QFontMetrics(pill_font)
        pills = [(f"{b['due']} due", "accent", "accent_soft")] if b["due"] else []
        if b["new"]:
            pills.append((f"{b['new']} new", "new", "new_soft"))
        x = button.right()
        y = button.bottom() + 10
        for text, fg, bg in reversed(pills):
            w = fm.horizontalAdvance(text) + 14
            r = QRectF(x - w, y, w, 19)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c[bg]))
            p.drawRoundedRect(r, 9.5, 9.5)
            p.setPen(QColor(c[fg]))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            x -= w + 5

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
    refetchRequested = Signal(int)  # a web snapshot to replace with a fresh copy
    reviewBookRequested = Signal(int)  # review one book's cards

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName("library")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._covers: dict[str, QPixmap | None] = {}  # by book hash; None: can't be drawn
        self._queue: list[tuple[str, str]] = []  # (hash, path) of covers still to draw
        self._cover_timer = QTimer(self, singleShot=True, interval=0)
        self._cover_timer.timeout.connect(self._draw_cover)

        title = QLabel("Library")
        title.setProperty("role", "title")
        self.count = QLabel()
        self.count.setProperty("role", "muted")
        heading = QVBoxLayout()
        heading.setSpacing(0)
        heading.addWidget(title)
        heading.addWidget(self.count)
        self.filter = QLineEdit(objectName="libraryFilter", placeholderText="Filter by title…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(240)
        self.filter.textChanged.connect(lambda _: self.refresh())
        browse = QPushButton("Open PDF or EPUB…")
        browse.setProperty("primary", True)
        browse.setIcon(icon("folder-open", "accent_fg"))
        browse.clicked.connect(self.browseRequested)
        web = QPushButton("Open web page…")
        web.setIcon(icon("globe"))
        web.clicked.connect(self.webRequested)
        top = QHBoxLayout()
        top.addLayout(heading)
        top.addStretch()
        top.addWidget(self.filter)
        top.addSpacing(6)
        top.addWidget(web)
        top.addWidget(browse)

        self.list = QListWidget(objectName="library")
        self.list.setItemDelegate(_BookCard(self))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        # Cards size to the viewport, so there is never anything to scroll sideways.
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list.itemClicked.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.viewport().installEventFilter(self)

        self.empty = QWidget()
        self.empty_icon = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("No books yet", alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setProperty("role", "heading")
        self.empty_text = QLabel(
            "Open a PDF, EPUB or web page to start reading. Select any passage to ask Claude about it.",
            alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True,
        )
        self.empty_text.setProperty("role", "muted")
        col = QVBoxLayout(self.empty)
        col.addStretch(2)
        for w in (self.empty_icon, self.empty_title, self.empty_text):
            col.addWidget(w)
        col.addStretch(3)

        body = QWidget()
        body.setMaximumWidth(MAX_WIDTH)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty, 1)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 16)
        outer.addWidget(body)

        theme().changed.connect(self._on_theme_changed)
        self._on_theme_changed()
        self.refresh()

    def _on_theme_changed(self) -> None:
        self.empty_icon.setPixmap(icon("library", "faint").pixmap(QSize(56, 56), self.devicePixelRatioF()))

    def eventFilter(self, obj, event) -> bool:
        if event.type() == event.Type.MouseMove:
            self.list.viewport().update()  # the Review button lights up under the pointer
        return False

    # -- covers ----------------------------------------------------------------------------
    def cover(self, b: dict) -> QPixmap | None:
        """The book's cover if drawn; otherwise it is queued and the card repainted once it is."""
        key = b["hash"]
        if key in self._covers:
            return self._covers[key]
        if path := covers.cached(key):
            self._covers[key] = pm = QPixmap(str(path))
            return pm if not pm.isNull() else None
        if (key, b["path"]) not in self._queue:
            self._queue.append((key, b["path"]))
            self._cover_timer.start()
        return None

    def _draw_cover(self) -> None:
        """One queued cover per turn of the event loop, so the library stays responsive."""
        if not self._queue:
            return
        key, path = self._queue.pop(0)
        out = covers.cover_file(key)
        pm = QPixmap(str(out)) if covers.render(path, out) else None
        self._covers[key] = pm if pm is not None and not pm.isNull() else None
        self.list.viewport().update()
        if self._queue:
            self._cover_timer.start()

    # -- books -----------------------------------------------------------------------------
    def refresh(self) -> None:
        self.list.clear()
        books = self.db.recent_books()
        needle = self.filter.text().strip().lower()
        shown = [b for b in books if needle in b["title"].lower()] if needle else books
        counts = dashboard.book_counts(self.db)
        reading = [b for b in shown if b["finished_at"] is None]
        finished = sorted((b for b in shown if b["finished_at"] is not None), key=lambda b: -b["finished_at"])
        for b in reading:
            self._add_book(b, counts)
        if finished:
            expanded = self.db.get_setting(FINISHED_COLLAPSED) != "1" or bool(needle)
            header = QListWidgetItem()
            header.setData(HEADER_ROLE, (f"Finished · {len(finished)}", expanded))
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setToolTip("Hide finished books" if expanded else "Show finished books")
            self.list.addItem(header)
            if expanded:
                for b in finished:
                    self._add_book(b, counts)
        cards = sum(c["total"] for c in counts.values())
        self.count.setText(
            f"{len(books)} book{'s' * (len(books) != 1)}" + (f" · {cards:,} review cards" if cards else "") if books else ""
        )
        self.list.setVisible(bool(shown))
        self.empty.setVisible(not shown)
        self.empty_title.setText("No books yet" if not books else "No matching books")
        self.empty_text.setVisible(not books)
        self.filter.setVisible(bool(books))

    def _add_book(self, b, counts: dict) -> None:
        chats = len(self.db.chats(b["id"]))
        c = counts.get(b["id"], {"due": 0, "new": 0, "total": 0})
        day = lambda t: time.strftime("%d %b %Y", time.localtime(t)).lstrip("0")
        when = f"finished {day(b['finished_at'])}" if b["finished_at"] else f"opened {day(b['opened_at'])}"
        parts = [f"{chats} chat{'s' * (chats != 1)}"]
        if c["total"]:
            parts.append(f"{c['total']:,} card{'s' * (c['total'] != 1)}")
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, b["id"])
        ext = os.path.splitext(b["path"])[1].lstrip(".").upper()[:4] or "BOOK"
        item.setData(BOOK_ROLE, {
            "id": b["id"], "title": b["title"], "path": b["path"], "hash": b["hash"], "exists": os.path.exists(b["path"]),
            "format": "WEB" if b["source_url"] else ext,
            "last_page": b["last_page"], "page_count": b["page_count"], "progress": dashboard.progress(b),
            "finished": b["finished_at"] is not None,
            "detail": " · ".join(parts + [when]),
            "due": c["due"], "new": c["new"], "cards": c["total"],
        })
        item.setToolTip(b["source_url"] or b["path"])
        self.list.addItem(item)

    def _on_review_button(self, item: QListWidgetItem, pos: QPoint) -> bool:
        b = item.data(BOOK_ROLE)
        if not b or not b["exists"] or not b["due"] + b["new"]:
            return False
        return review_rect(_card_rect(self.list.visualItemRect(item))).contains(QPointF(pos))

    def _activate(self, item: QListWidgetItem) -> None:
        if header := item.data(HEADER_ROLE):
            self.db.set_setting(FINISHED_COLLAPSED, "1" if header[1] else "0")
            self.refresh()
            return
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        if not book or not os.path.exists(book["path"]):
            return
        if self._on_review_button(item, self.list.viewport().mapFromGlobal(QCursor.pos())):
            self.reviewBookRequested.emit(book["id"])
        else:
            self.openRequested.emit(book["path"])

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None or item.data(HEADER_ROLE):
            return
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        b = item.data(BOOK_ROLE)
        menu = QMenu(self)
        review = menu.addAction(icon("cards"), "Review this book's cards") if b["cards"] else None
        finished = book["finished_at"] is not None
        finish = menu.addAction(icon("check-circle"), "Move back to reading" if finished else "Mark as finished")
        refetch = menu.addAction(icon("globe"), "Fetch a fresh copy") if book["source_url"] else None
        menu.addSeparator()
        remove = menu.addAction("Remove from library (deletes its chats)")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is finish:
            self.db.set_finished(book["id"], not finished)
            self.refresh()
        elif chosen is remove:
            self.db.delete_book(book["id"])
            self.refresh()
        elif chosen is review:
            self.reviewBookRequested.emit(book["id"])
        elif chosen is refetch:
            self.refetchRequested.emit(book["id"])
