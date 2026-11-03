"""Ctrl+F: the find bar over the reader, the results list that takes the Contents' place in the
sidebar while it is open, and ``BookFind``, which runs a ``book_search.BookSearch`` a step per
turn of the event loop and keeps the two and the reader's highlights in step."""

from __future__ import annotations

import html

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QTextDocument
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QStyle, QStyledItemDelegate, QToolButton, QWidget,
)

from . import book_search
from .book_search import BookSearch, Match, SearchIndex
from .document import Document
from .theme import icon, theme

TYPING_PAUSE = 150  # ms after a keystroke before searching


class FindBar(QFrame):
    """The query, "3 of 41", previous / next and close, floating at the top right of the reader."""

    queryChanged = Signal(str)
    nextRequested = Signal()
    prevRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent, objectName="popup")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 5, 5, 5)
        row.setSpacing(4)
        self.edit = QLineEdit(objectName="findInput", placeholderText="Find in book")
        self.edit.setMinimumWidth(220)
        self.edit.textChanged.connect(self.queryChanged)
        self.edit.installEventFilter(self)
        self.count = QLabel()
        self.count.setProperty("role", "muted")
        self.count.setMinimumWidth(QFontMetrics(self.count.font()).horizontalAdvance("0000 of 5000+") + 4)
        self.count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.edit)
        row.addWidget(self.count)
        self.buttons = []
        for name, tip, signal in (("arrow-up", "Previous match (Shift+Enter, Shift+F3)", self.prevRequested),
                                  ("chevron-down", "Next match (Enter, F3)", self.nextRequested),
                                  ("x", "Close (Escape)", self.closeRequested)):
            b = QToolButton(autoRaise=True, toolTip=tip)
            b.setIcon(icon(name))
            b.clicked.connect(signal)
            row.addWidget(b)
            self.buttons.append(b)
        theme().changed.connect(self._restyle)
        parent.installEventFilter(self)  # follow the viewport's size
        self.hide()

    def _restyle(self) -> None:
        for b, name in zip(self.buttons, ("arrow-up", "chevron-down", "x")):
            b.setIcon(icon(name))

    def place(self) -> None:
        self.adjustSize()
        self.move(self.parentWidget().width() - self.width() - 14, 10)
        self.raise_()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.place()
        elif obj is self.edit and event.type() == QEvent.Type.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                (self.prevRequested if mods & Qt.KeyboardModifier.ShiftModifier else self.nextRequested).emit()
                return True
            if key == Qt.Key.Key_Escape:
                self.closeRequested.emit()
                return True
        return False


class _Results(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search: BookSearch | None = None
        self.section = lambda page: ""

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() or self.search is None else len(self.search.matches)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.search is None:
            return None
        m = self.search.matches[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return m
        if role == Qt.ItemDataRole.DisplayRole:
            return "".join(m.context)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {m.page + 1}" + (f" · {self.section(m.page)}" if self.section(m.page) else "")
        return None

    def reset(self, search: BookSearch | None) -> None:
        self.beginResetModel()
        self.search = search
        self.endResetModel()

    def inserted(self, at: int, count: int) -> None:
        """``count`` matches were put in at ``at`` (the search's list is already longer)."""
        self.beginInsertRows(QModelIndex(), at, at + count - 1)
        self.endInsertRows()


class _ResultDelegate(QStyledItemDelegate):
    """Page and section over the match in its surrounding text."""

    LINES = 3  # the header and two of text

    def __init__(self, model: _Results, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index) -> QSize:
        return QSize(100, QFontMetrics(option.font).lineSpacing() * self.LINES + 14)

    def paint(self, p: QPainter, option, index) -> None:
        m: Match = index.data(Qt.ItemDataRole.UserRole)
        c = theme().colors
        selected = option.state & QStyle.StateFlag.State_Selected
        p.save()
        r = option.rect.adjusted(4, 2, -4, -2)
        if selected or option.state & QStyle.StateFlag.State_MouseOver:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["accent_soft"] if selected else c["hover"]))
            p.drawRoundedRect(QRectF(r), 6, 6)
        inner = r.adjusted(8, 4, -8, -4)
        fm = QFontMetrics(option.font)
        section = self.model.section(m.page)
        head = f"p. {m.page + 1}" + (f"  ·  {section}" if section else "")
        p.setPen(QColor(c["muted"]))
        p.drawText(inner.x(), inner.y() + fm.ascent(), fm.elidedText(head, Qt.TextElideMode.ElideRight, inner.width()))
        before, hit, after = m.context
        # Keep the match on the first line: as much of the text before it as fits in half of it.
        while before and fm.horizontalAdvance(before) > inner.width() * 0.5:
            cut = before.find(" ", 2)
            before = "…" + (before[cut + 1:] if cut > 0 else before[len(before) // 2:])
        before, hit, after = html.escape(before), html.escape(hit), html.escape(after)
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setDocumentMargin(0)
        doc.setTextWidth(inner.width())
        doc.setHtml(
            f"<span style='color:{c['fg']}'>{before}<b style='color:{c['accent']}'>{hit}</b>{after}</span>"
        )
        p.translate(inner.x(), inner.y() + fm.lineSpacing())
        p.setClipRect(QRectF(0, 0, inner.width(), fm.lineSpacing() * (self.LINES - 1)))
        doc.drawContents(p)
        p.restore()


class FindResults(QListView):
    matchChosen = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent, objectName="findResults")
        self.results = _Results(self)
        self.setModel(self.results)
        self.setItemDelegate(_ResultDelegate(self.results, self))
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.clicked.connect(lambda i: self.matchChosen.emit(i.data(Qt.ItemDataRole.UserRole)))
        self.activated.connect(lambda i: self.matchChosen.emit(i.data(Qt.ItemDataRole.UserRole)))
        theme().changed.connect(self.viewport().update)

    def show_row(self, row: int | None) -> None:
        if row is None:
            self.clearSelection()
            return
        index = self.results.index(row)
        self.setCurrentIndex(index)
        self.scrollTo(index)


class BookFind(QObject):
    """Ctrl+F in the open book. ``activeChanged`` says when the find bar opens and closes, so
    the sidebar can swap the contents for the results."""

    activeChanged = Signal(bool)
    summaryChanged = Signal(str)  # for the results list's heading

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.bar = FindBar(reader.viewport())
        self.list = FindResults()
        self.index: SearchIndex | None = None
        self.search: BookSearch | None = None
        self.current: Match | None = None
        self._by_page: dict[int, list] = {}
        self._timer = QTimer(self, interval=0)
        self._timer.timeout.connect(self._step)
        self._typing = QTimer(self, singleShot=True, interval=TYPING_PAUSE)
        self._typing.timeout.connect(lambda: self.start(self.bar.edit.text()))
        self.bar.queryChanged.connect(lambda _: self._typing.start())
        self.bar.nextRequested.connect(self.next)
        self.bar.prevRequested.connect(self.prev)
        self.bar.closeRequested.connect(self.close)
        self.list.matchChosen.connect(self._choose)
        reader.escapePressed.connect(lambda: self.active and self.close())

    @property
    def active(self) -> bool:
        return self.bar.isVisible()

    def set_document(self, doc: Document | None) -> None:
        self.close()
        self.index = SearchIndex(doc) if doc else None
        self.list.results.section = book_search.section_titles(doc.toc()) if doc else (lambda page: "")
        self._clear()

    def open(self) -> None:
        """Show the bar with its query selected; a selection in the reader becomes the query."""
        if self.index is None:
            return
        picked = " ".join(self.reader.selected_text().split()) if self.reader.has_selection() else ""
        was_active = self.active
        self.bar.show()
        self.bar.place()
        self.bar.edit.setFocus()
        if picked and len(picked) <= 200 and book_search.fold(picked) != (self.search and self.search.query):
            self.reader.clear_selection()
            self.bar.edit.blockSignals(True)
            self.bar.edit.setText(picked)
            self.bar.edit.blockSignals(False)
            self.start(picked)
        elif not was_active:
            if self.search is not None and self.search.query == book_search.fold(self.bar.edit.text()):
                self._show_highlights()  # the last search, as it was
                self._summarize()
                if not self.search.done:
                    self._timer.start()
            elif self.bar.edit.text().strip():
                self.start(self.bar.edit.text())
        self.bar.edit.selectAll()
        if not was_active:
            self.activeChanged.emit(True)
        self._summarize()

    def close(self) -> None:
        if not self.active:
            return
        self._typing.stop()
        self._timer.stop()
        self.bar.hide()
        self.reader.clear_find()
        self.reader.setFocus()
        self.activeChanged.emit(False)

    def _clear(self) -> None:
        self._timer.stop()
        self.search, self.current, self._by_page = None, None, {}
        self.list.results.reset(None)
        self.reader.clear_find()
        self._summarize()

    def start(self, query: str) -> None:
        """Search the book for ``query`` from the page being read on."""
        self._clear()
        if self.index is None or not book_search.fold(query):
            return
        self.search = BookSearch(self.index, query, self.reader.current_page())
        self.list.results.reset(self.search)
        self._step()
        if not self.search.done:
            self._timer.start()

    def _step(self) -> None:
        s = self.search
        if s is None:
            self._timer.stop()
            return
        at, found = s.step()
        if found:
            self.list.results.inserted(at, len(found))
            for m in found:
                self._by_page.setdefault(m.page, []).append(m.rects)
            if self.current is None:
                self._go(found[0])  # the first match from the page being read on
            else:
                self._show_highlights()  # the list keeps its current row through the insert
        if s.done:
            self._timer.stop()
        self._summarize()

    def next(self) -> None:
        self._move(1)

    def prev(self) -> None:
        self._move(-1)

    def _move(self, by: int) -> None:
        if not self.active:
            self.open()
            return
        if self._typing.isActive():  # Enter right after typing: search now
            self._typing.stop()
            self.start(self.bar.edit.text())
            if by > 0:
                return
        s = self.search
        if s is None or not s.matches:
            return
        if self.current is None:
            i = s.after(self.reader.current_page())
        else:
            i = (s.position(self.current) + by) % len(s.matches)
        self._go(s.matches[i])

    def _choose(self, m: Match) -> None:
        self._go(m)
        self.reader.setFocus()

    def _go(self, m: Match) -> None:
        self.current = m
        self._show_highlights()
        self.reader.show_match(m.page, m.rects)
        self.list.show_row(self.search.position(m))
        self._summarize()

    def _show_highlights(self) -> None:
        cur = (self.current.page, self.current.rects) if self.current else None
        self.reader.set_find(self._by_page, cur)

    def _summarize(self) -> None:
        s = self.search
        if s is None:
            text, heading = "", "Search results"
        elif not s.matches:
            text = "Searching…" if not s.done else "No matches"
            heading = text
        else:
            n = f"{len(s.matches)}{'+' if s.truncated else ''}"
            i = s.position(self.current) + 1 if self.current else 0
            text = (f"{i} of {n}" if i else n) + ("…" if not s.done else "")
            heading = f"{n} match{'es' * (len(s.matches) != 1)}" + (
                f" · searching ({round(100 * s.pages_done / max(s.page_count, 1))}%)" if not s.done else ""
            )
        self.bar.count.setText(text)
        self.summaryChanged.emit(heading)
