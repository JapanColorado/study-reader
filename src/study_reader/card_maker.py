"""Make cards from a highlight: Claude writes review cards on a passage or screenshot in the
background (passage_cards.py, forked from the book's session so it sees the whole book) and they
join the book's deck as new cards; a Notice at the foot of the window says how it went."""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QWidget

from . import claude_client as cc
from . import passage_cards, srs
from .chat_service import BookRef, ChatWorker, Job
from .db import Database, data_dir

NOTICE_MS = 5000


def _pages(n: int) -> str:
    return f"p. {n + 1}"


class CardMaker(QObject):
    cardsChanged = Signal()  # cards were added (their passage is highlighted from then on)
    passagesChanged = Signal()  # a request started or ended: pending_passages() changed
    notice = Signal(str, bool)  # text, whether it stays up (work still running)

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._next = 1
        # request id -> (book id, snippet without its png, screenshot file or "")
        self._pending: dict[int, tuple[int, dict, str]] = {}
        worker.carded.connect(self._on_carded)
        worker.cardsFailed.connect(self._on_failed)

    def make(self, book_id: int, snippet: dict, model: str | None = None) -> None:
        """Ask for cards on a snippet as the reader emits it: dict(kind, page, text, rects, png)."""
        book = self.db.book(book_id)
        if book is None:
            return
        snippet = dict(snippet)
        image = ""
        if png := snippet.pop("png", None):
            folder = data_dir() / "snippets"
            folder.mkdir(exist_ok=True)
            path = folder / f"cards-{uuid.uuid4().hex}.png"
            path.write_bytes(png)
            image = str(path)
        rid, self._next = self._next, self._next + 1
        self._pending[rid] = (book_id, snippet, image)
        self.worker.submit_cards(Job(
            chat_id=0,
            book=BookRef(book["id"], book["path"], book["title"], book["base_session_id"], book["context_mode"]),
            chat_session_id=None,
            question=passage_cards.request(snippet),
            snippets=[cc.Snippet(snippet["kind"], snippet["page"], snippet.get("text", ""), image)],
            page=snippet["page"],
            model=model or None,
            context=srs.tags_context(self.db),
            detached=True,
        ), rid)
        self.passagesChanged.emit()
        n = len(self._pending)
        self.notice.emit(f"Making cards from {_pages(snippet['page'])}…" if n == 1
                         else f"Making cards from {n} highlights…", True)

    def pending_passages(self, book_id: int) -> list[dict]:
        """The book's passages Claude is still making cards from, shaped like Database.card_passages."""
        return [{**sn, "id": None, "card_ids": [], "title": ""}
                for b, sn, _ in self._pending.values() if b == book_id]

    def _done(self, rid: int) -> tuple[int, dict] | None:
        found = self._pending.pop(rid, None)
        if found is None:
            return None
        book_id, snippet, image = found
        if image:
            Path(image).unlink(missing_ok=True)
        return book_id, snippet

    def _on_carded(self, rid: int, text: str) -> None:
        found = self._done(rid)
        if found is None:
            return
        book_id, snippet = found
        page = snippet["page"]
        try:
            added = passage_cards.add_cards(self.db, book_id, snippet, text)
        except (ValueError, TypeError, KeyError) as e:
            self._finished(f"Couldn't make cards from {_pages(page)}: {e}")
            return
        if added:
            self.cardsChanged.emit()
            n = len(added)
            self._finished(f"Added {n} card{'s' if n != 1 else ''} from {_pages(page)} to Review")
        else:
            self._finished(f"Claude found nothing worth a card on {_pages(page)}")

    def _on_failed(self, rid: int, message: str) -> None:
        found = self._done(rid)
        if found is not None:
            self._finished(f"Couldn't make cards from {_pages(found[1]['page'])}: {message}")

    def _finished(self, text: str) -> None:
        self.passagesChanged.emit()
        if self._pending:
            n = len(self._pending)
            text += f" · still making cards from {n} highlight{'s' if n != 1 else ''}…"
        self.notice.emit(text, bool(self._pending))


class Notice(QLabel):
    """A short message floating at the foot of a widget, gone after a few seconds unless sticky."""

    def __init__(self, parent: QWidget):
        super().__init__(parent, objectName="notice")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setWordWrap(True)
        self._timer = QTimer(self, singleShot=True, interval=NOTICE_MS)
        self._timer.timeout.connect(self.hide)
        parent.installEventFilter(self)
        self.hide()

    def show_text(self, text: str, sticky: bool = False) -> None:
        self.setText(text)
        self._place()
        self.show()
        self.raise_()
        if sticky:
            self._timer.stop()
        else:
            self._timer.start()

    def _place(self) -> None:
        parent = self.parentWidget()
        m = self.contentsMargins()
        one_line = self.fontMetrics().horizontalAdvance(self.text()) + m.left() + m.right() + 4
        self.setFixedWidth(min(one_line, max(parent.width() - 48, 200), 640))
        self.setFixedHeight(self.heightForWidth(self.width()))
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 28)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False
