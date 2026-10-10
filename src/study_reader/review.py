"""Reviewing quiz questions as spaced-repetition cards (scheduled by srs.py): the ReviewPanel,
rendered in review.html. One sits in the right-hand slot for the open book; another fills the
center for reviewing every book from the library."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from . import quiz, srs
from .chat_service import BookRef, ChatWorker, Job
from .db import Database
from .theme import theme

ASSETS = Path(__file__).parent / "assets"


def when(t: float, now: float) -> str:
    """'in 5m', 'in 3d' …"""
    return f"in {srs.format_interval(t - now)}"


class _Page(QWebEnginePage):
    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return False
        return url.isLocalFile()


class _Bridge(QObject):
    """What review.html calls through the web channel."""

    def __init__(self, panel: ReviewPanel):
        super().__init__(panel)
        self.panel = panel

    @Slot()
    def start(self) -> None:
        self.panel.start()

    @Slot()
    def stop(self) -> None:
        self.panel.stop()

    @Slot(int, str)
    def check(self, card_id: int, response: str) -> None:
        self.panel.check(card_id, json.loads(response))

    @Slot(int, str)
    def claude(self, card_id: int, text: str) -> None:
        self.panel.grade_with_claude(card_id, text)

    @Slot(int, int)
    def rate(self, card_id: int, rating: int) -> None:
        self.panel.rate(card_id, rating)

    @Slot(int, str)
    def act(self, card_id: int, action: str) -> None:
        self.panel.act(card_id, action)


class ReviewPanel(QWidget):
    """An overview of what is due, then one card at a time: answer it (graded where it can be),
    then rate it Again / Hard / Good / Easy. all_books: review every book's cards."""

    sourceRequested = Signal(int, int)  # book id, 0-based page (-1 if unknown)
    chatRequested = Signal(int, int)  # book id, chat id
    countsChanged = Signal()  # a card was reviewed, suspended or deleted

    def __init__(self, db: Database, worker: ChatWorker, all_books: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.all_books = all_books
        self.book_id: int | None = None
        self.card_id: int | None = None  # the card on display; None for the overview
        self._answer: dict | None = None  # its answer so far: {response, status, verdict, feedback}
        self._grading: dict[int, str] = {}  # card id -> the free response Claude is grading
        self._skip: list[int] = []  # cards just answered or skipped, not to be shown again right away
        self._reviewed = 0  # cards reviewed since the session started

        self.view = QWebEngineView()
        self.view.setPage(_Page(self.view))
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("review", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "review.html")))

        self.setObjectName("chatPanel")  # same surface as the panels it swaps with
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.view)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        worker.cardGraded.connect(self._on_graded)
        worker.cardGradeFailed.connect(self._on_grade_failed)
        # Learning cards fall due minutes apart; keep the overview's counts current.
        self._timer = QTimer(self, interval=60_000)
        self._timer.timeout.connect(lambda: self.card_id is None and self.refresh())
        self._timer.start()
        if all_books:
            self.refresh()
        else:
            self.set_book(None)

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
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self._js("setTheme", theme().colors, theme().dark)

    def focus(self) -> None:
        self.view.setFocus()

    # -- scope ---------------------------------------------------------------------------
    @property
    def scope(self) -> int | None:
        """The book whose cards are reviewed, or None for all books."""
        return None if self.all_books else self.book_id

    def set_book(self, book_id: int | None) -> None:
        self.book_id = book_id
        self.setEnabled(self.all_books or book_id is not None)
        self.stop()

    def counts(self) -> dict:
        return srs.counts(self.db, self.scope)

    def refresh(self) -> None:
        """Redraw: the card on display (unless it is gone) or the overview."""
        if self.card_id is not None and self.db.card(self.card_id) is not None:
            self._show_card()
        else:
            self.card_id = None
            self._show_overview()

    def _show_overview(self) -> None:
        if not self.all_books and self.book_id is None:
            self._js("showOverview", {"disabled": True})
            return
        now = time.time()
        c = srs.counts(self.db, self.scope, now)
        upcoming = srs.next_due(self.db, self.scope, now)
        self._js("showOverview", {
            **c,
            "all": self.all_books,
            "ready": srs.next_card(self.db, self.scope, now) is not None,
            "next": when(upcoming, now) if upcoming else "",
            "reviewed": self._reviewed,
            "new_per_day": srs.new_per_day(self.db),
        })

    # -- the session ---------------------------------------------------------------------
    def start(self) -> None:
        self._reviewed = 0
        self._skip = []
        self._next()

    def stop(self) -> None:
        self.card_id, self._answer = None, None
        self._show_overview()

    def _next(self) -> None:
        card = srs.next_card(self.db, self.scope, skip=self._skip)
        self.card_id = card["id"] if card else None
        self._answer = None
        if card is None:
            self._show_overview()
        else:
            self._show_card()

    def _show_card(self) -> None:
        card = self.db.card(self.card_id)
        now = time.time()
        a = self._answer
        if a is None and card["id"] in self._grading:  # still being graded, e.g. after a refresh
            a = {"response": {"text": self._grading[card["id"]]}, "status": "pending", "verdict": None, "feedback": ""}
        c = srs.counts(self.db, self.scope, now)
        book = self.db.book(card["book_id"])
        self._js("showCard", {
            "id": card["id"],
            "question": srs.question(card),
            "title": card["title"],
            "book": book["title"] if self.all_books else "",
            "page": card["page"],
            "chat": card["message_id"] is not None,
            "new": srs.is_new(card),
            "answer": a,
            "previews": srs.previews(card, now) if a and a["status"] != "pending" else None,
            "suggested": srs.suggested_rating(a["verdict"]) if a else None,
            "remaining": {"due": c["due"], "new": c["new"]},
        })

    def check(self, card_id: int, response: dict) -> None:
        """An answer, or a request to see the answer (a flashcard's back, a free response's model answer)."""
        card = self.db.card(card_id)
        if card is None or card_id != self.card_id or self._answer is not None:
            return
        q = srs.question(card)
        verdict = None
        if q["type"] not in ("flashcard", "free"):
            try:
                verdict = quiz.grade(q, response)
            except quiz.AnswerError as e:
                self._js("inputError", card_id, str(e))
                return
        self._answer = {"response": response, "status": "done", "verdict": verdict, "feedback": ""}
        self._show_card()

    def grade_with_claude(self, card_id: int, text: str) -> None:
        card = self.db.card(card_id)
        if card is None or card_id != self.card_id or not text.strip() or card_id in self._grading:
            return
        self._grading[card_id] = text
        self._answer = {"response": {"text": text}, "status": "pending", "verdict": None, "feedback": ""}
        self.worker.submit_card_grade(self._grading_job(card, text), card_id)
        self._show_card()

    def _grading_job(self, card, text: str) -> Job:
        """Like a quiz answer in the chat: a detached fork at the quiz's reply, or, if that reply
        is gone, from the book."""
        message = self.db.message(card["message_id"]) if card["message_id"] is not None else None
        chat = self.db.chat(message["chat_id"]) if message else None
        history = []
        if message:
            history = [(m["role"], m["text"]) for m in self.db.messages(chat["id"])
                       if m["id"] <= message["id"] and m["role"] != "error"]
        book = self.db.book(card["book_id"])
        return Job(
            chat_id=chat["id"] if chat else 0,
            book=BookRef(book["id"], book["path"], book["title"], book["base_session_id"], book["context_mode"]),
            chat_session_id=message["session_id"] if message else None,
            question=quiz.review_grading_prompt(card["title"], card["question"], srs.question(card), text),
            history=history,
            page=card["page"] or 0,
            model=(chat["model"] if chat else "") or self.db.get_setting("model") or None,
            resume_at=message["session_uuid"] if message else None,
            detached=True,
        )

    def _on_graded(self, card_id: int, text: str) -> None:
        verdict, feedback = quiz.parse_grade(text)
        self._graded(card_id, status="done", verdict=verdict, feedback=feedback)

    def _on_grade_failed(self, card_id: int, message: str) -> None:
        self._graded(card_id, status="error", verdict=None, feedback=message)

    def _graded(self, card_id: int, **fields) -> None:
        text = self._grading.pop(card_id, None)
        if text is None or card_id != self.card_id:  # rated or left meanwhile
            return
        self._answer = {"response": {"text": text}, **fields}
        self._show_card()

    def rate(self, card_id: int, rating: int) -> None:
        if card_id != self.card_id or rating not in quiz.RATINGS:
            return
        a = self._answer or {}
        srs.review(self.db, card_id, rating, response=a.get("response"), verdict=a.get("verdict"))
        self._grading.pop(card_id, None)
        self._reviewed += 1
        self._skip = [card_id]  # skipped cards may come back now
        self.countsChanged.emit()
        self._next()

    def act(self, card_id: int, action: str) -> None:
        card = self.db.card(card_id)
        if card is None:
            return
        if action == "source":
            self.sourceRequested.emit(card["book_id"], card["page"] if card["page"] is not None else -1)
        elif action == "chat":
            message = self.db.message(card["message_id"]) if card["message_id"] is not None else None
            if message:
                self.chatRequested.emit(card["book_id"], message["chat_id"])
        elif action == "skip":
            self._skip.append(card_id)
            self._next()
        elif action == "suspend":
            self.db.update_card(card_id, suspended=1)
            self.countsChanged.emit()
            self._next()
        elif action == "delete":
            answer = QMessageBox.question(
                self, "Delete card", "Delete this card and its review history? (Suspend keeps it out of review instead.)",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.db.delete_card(card_id)
                self.countsChanged.emit()
                self._next()
