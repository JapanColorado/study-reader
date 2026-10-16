"""Reviewing quiz questions as spaced-repetition cards (scheduled by srs.py): the ReviewPanel,
rendered in review.html. One sits in the right-hand slot for the open book; another fills the
center for reviewing every book from the library."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from . import cardmodel, gamify, quiz, srs, variants
from .chat_service import BookRef, ChatWorker, Job
from .db import Database, media_url
from .notes import LocalPage
from .theme import theme

ASSETS = Path(__file__).parent / "assets"
# Longer than this on one card counts as this long (the reviewer probably stepped away).
MAX_REVIEW_SECONDS = 120


def when(t: float, now: float) -> str:
    """'in 5m', 'in 3d' …"""
    return f"in {srs.format_interval(t - now)}"


def card_job(db: Database, card, question: str) -> Job:
    """Grading an answer to a card: like a quiz answer in the chat, a detached fork at the quiz's
    reply, or, if that reply is gone, from the book."""
    message = db.message(card["message_id"]) if card["message_id"] is not None else None
    chat = db.chat(message["chat_id"]) if message else None
    history = []
    if message:
        history = [(m["role"], m["text"]) for m in db.messages(chat["id"])
                   if m["id"] <= message["id"] and m["role"] != "error"]
    book = db.book(card["book_id"]) if card["book_id"] is not None else None
    return Job(
        chat_id=chat["id"] if chat else 0,
        # A card from no book (made in the browser, or migrated) is graded in a fresh session.
        book=BookRef(book["id"], book["path"], book["title"], book["base_session_id"], book["context_mode"])
        if book else None,
        chat_session_id=message["session_id"] if message else None,
        question=question,
        history=history,
        page=card["page"] or 0,
        model=(chat["model"] if chat else "") or db.get_setting("model") or None,
        resume_at=message["session_uuid"] if message else None,
        detached=True,
    )


def card_title(db: Database, card) -> tuple[str, int | None]:
    """How to name a card to Claude: (its quiz's title, its index in the quiz), or, if it isn't
    from a quiz in a chat, (its deck, None)."""
    if card["message_id"] is not None and db.message(card["message_id"]) is not None:
        return card["title"], card["question"]
    deck = db.deck(card["deck_id"])
    return (deck["name"] if deck else ""), None


class VariantMaker(QObject):
    """After a calculation card is reviewed, has Claude write its next version with new numbers
    (variants.py, in a fresh bookless session) and keeps it on the card for review to show. Failures are silent: the card
    shows its last version again."""

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._pending: dict[int, str] = {}  # card id -> its data when asked, to drop answers an edit outdated
        worker.varied.connect(self._on_varied)
        worker.variantFailed.connect(lambda card_id, _: self._pending.pop(card_id, None))

    def after_review(self, card_id: int) -> None:
        card = self.db.card(card_id)
        if card is None or card_id in self._pending or not variants.wants_variant(card):
            return
        self._pending[card_id] = card["data"]
        last = variants.question(card) if variants.is_varied(card) else None
        book = self.db.book(card["book_id"]) if card["book_id"] is not None else None
        title, _ = card_title(self.db, card)
        message = self.db.message(card["message_id"]) if card["message_id"] is not None else None
        chat = self.db.chat(message["chat_id"]) if message else None
        prompt = variants.request(srs.question(card), last, book["title"] if book else "", title)
        # Without the book: the problem and its solution are enough, and far cheaper to send.
        self.worker.submit_variant(Job(
            chat_id=0, book=None, chat_session_id=None, question=prompt,
            model=(chat["model"] if chat else "") or self.db.get_setting("model") or None,
        ), card_id)

    def _on_varied(self, card_id: int, text: str) -> None:
        data = self._pending.pop(card_id, None)
        card = self.db.card(card_id)
        if card is None or card["data"] != data:
            return
        try:
            variant = variants.parse(text, srs.question(card))
        except (ValueError, TypeError, KeyError):
            return
        self.db.update_card(card_id, variant=cardmodel.dumps(variant))


class _Bridge(QObject):
    """What review.html calls through the web channel."""

    def __init__(self, panel: ReviewPanel):
        super().__init__(panel)
        self.panel = panel

    @Slot(str)
    def start(self, limit: str) -> None:
        self.panel.start(limit)

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

    @Slot(int)
    def chooseDeck(self, deck_id: int) -> None:
        self.panel.choose_deck(deck_id or None)

    @Slot(int)
    def chooseBook(self, book_id: int) -> None:
        self.panel.choose_book(book_id or None)

    @Slot(int, str)
    def chooseSection(self, book_id: int, tag: str) -> None:
        self.panel.choose_section(book_id or None, tag or None)

    @Slot()
    def refresh(self) -> None:
        self.panel.refresh()

    @Slot(str)
    def open(self, what: str) -> None:
        (self.panel.browseRequested if what == "browse" else self.panel.statsRequested).emit()


class ReviewPanel(QWidget):
    """An overview of what is due, then one card at a time: answer it (graded where it can be),
    then rate it Again / Hard / Good / Easy. all_books: review every book's cards. variants: asks
    for the next version of each calculation card reviewed."""

    sourceRequested = Signal(int, int)  # book id, 0-based page (-1 if unknown)
    chatRequested = Signal(int, int)  # book id, chat id
    countsChanged = Signal()  # a card was reviewed, suspended or deleted
    browseRequested = Signal()
    statsRequested = Signal()
    editRequested = Signal(int)  # card id, to open in the browser

    def __init__(self, db: Database, worker: ChatWorker, all_books: bool = False,
                 variants: VariantMaker | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.variants = variants
        self.all_books = all_books
        self.book_id: int | None = None
        self.deck_id: int | None = None  # all_books: the deck (and its subdecks) to review; None for all
        self.picked_book: int | None = None  # all_books: the book to review instead, if any
        self.section: str | None = None  # the section (tag, srs.section_tag) of the book to review, if any
        self.card_id: int | None = None  # the card on display; None for the overview
        self._shown_at = 0.0  # when the card on display was first shown (monotonic), for its review time
        # The question on display and the card data it was shown for: kept while the card is up,
        # so a new version arriving meanwhile doesn't replace it (an edit to the card does).
        self._question: dict | None = None
        self._shown_data = ""
        self._answer: dict | None = None  # its answer so far: {response, status, verdict, feedback}
        self._grading: dict[int, str] = {}  # card id -> the free response Claude is grading
        self._skip: list[int] = []  # cards just answered or skipped, not to be shown again right away
        self._session: gamify.Session | None = None  # the session under way
        self._summary: dict | None = None  # how the last session went, for the overview

        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
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
        self.view.page().runJavaScript(f"setMedia({json.dumps(media_url())})")
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
        return self.picked_book if self.all_books else self.book_id

    def set_book(self, book_id: int | None) -> None:
        self.book_id = book_id
        self.section = None
        self.setEnabled(self.all_books or book_id is not None)
        self.stop()
        self._summary = None

    def counts(self) -> dict:
        return srs.counts(self.db, self.scope, deck=self.deck, tag=self.section)

    @property
    def deck(self) -> int | None:
        return self.deck_id if self.all_books else None

    def choose_deck(self, deck_id: int | None) -> None:
        self.deck_id = deck_id  # the overview drops it if the deck is gone
        self.picked_book, self.section = None, None
        self.stop()

    def choose_book(self, book_id: int | None) -> None:
        """all_books: review one book's cards (whatever their decks)."""
        self.deck_id, self.picked_book, self.section = None, book_id, None
        self.stop()

    def choose_section(self, book_id: int | None, tag: str | None) -> None:
        """Review one section of a book (of the open one, unless all_books); None for the whole book."""
        if self.all_books:
            self.deck_id, self.picked_book = None, book_id
        self.section = tag
        self.stop()

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
        if self.deck_id is not None and self.db.deck(self.deck_id) is None:
            self.deck_id = None
        if self.picked_book is not None and self.db.book(self.picked_book) is None:
            self.picked_book, self.section = None, None
        if self.section is not None and not self.db.count_cards(self.scope, tag=self.section):
            self.section = None  # its cards are gone (or retagged)
        c = srs.counts(self.db, self.scope, now, deck=self.deck, tag=self.section)
        upcoming = srs.next_due(self.db, self.scope, now, deck=self.deck, tag=self.section)
        overview = {
            **c,
            "all": self.all_books,
            "title": self._scope_title(),
            "section": self.section,
            "ready": srs.next_card(self.db, self.scope, now, deck=self.deck, tag=self.section) is not None,
            "next": when(upcoming, now) if upcoming else "",
            "reviewed": self._summary["reviewed"] if self._summary else 0,
            "summary": self._summary,
            "new_per_day": srs.new_per_day(self.db),
            "game": gamify.status(self.db, now) if gamify.enabled(self.db) else None,
            "quick": {"cards": gamify.QUICK_CARDS, "minutes": gamify.QUICK_SECONDS // 60},
        }
        if self.all_books:
            overview.update(self._decks_overview(now, c if self.scope is None and self.deck is None else None))
            overview.update(self._books_overview(now))
        elif self.book_id is not None:
            left = srs.new_left_today(self.db, now)
            whole = srs.counts(self.db, self.book_id, now) if self.section else c
            overview["sections"] = [{**n, "new": min(n["new"], left)}
                                    for n in srs.sections(self.db, self.book_id, now).get(self.book_id, [])]
            overview.update(whole_due=whole["due"], whole_new=whole["new"])
        self._js("showOverview", overview)

    def _scope_title(self) -> str:
        """What is being reviewed, for the overview's heading ('' for everything, or the whole open book)."""
        section = " › ".join(p.replace("_", " ") for p in self.section.split("::")[1:]) if self.section else ""
        if not self.all_books:
            return section
        if self.picked_book is not None:
            book = self.db.book(self.picked_book)
            return " › ".join(t for t in (book["title"] if book else "", section) if t)
        deck = self.db.deck(self.deck_id) if self.deck_id is not None else None
        return deck["name"].replace("::", " › ") if deck else ""

    def _books_overview(self, now: float) -> dict:
        """all_books: the books with cards, each with its counts and sections, for picking what to review."""
        left = srs.new_left_today(self.db, now)
        rows = self.db.conn.execute(
            f"SELECT c.book_id, b.title, SUM({srs.due_sql('c.')}) AS due, SUM(c.suspended=0 AND c.stage=0) AS new "
            "FROM cards c JOIN books b ON b.id=c.book_id GROUP BY c.book_id ORDER BY b.title COLLATE NOCASE",
            (srs.next_day(now), now),
        ).fetchall()
        sections = srs.sections(self.db, None, now)
        return {
            "books": [{
                "id": r["book_id"], "title": r["title"], "due": r["due"] or 0, "new": min(r["new"] or 0, left),
                "sections": [{**n, "new": min(n["new"], left)} for n in sections.get(r["book_id"], [])],
            } for r in rows],
            "book": self.picked_book,
        }

    def _decks_overview(self, now: float, everything: dict | None = None) -> dict:
        """The deck list for picking what to review: each deck with its parent and counts.
        everything: srs.counts over all decks, if already known."""
        counts = srs.deck_counts(self.db, now)
        # Books' own decks are left out (books have their own list), unless other decks are under them.
        book_decks = {r[0] for r in self.db.conn.execute("SELECT deck_id FROM books WHERE deck_id IS NOT NULL")}
        all_decks = [d for d in self.db.decks() if counts[d["id"]]["total"]]
        shown = {d["name"] for d in all_decks if d["id"] not in book_decks}
        keep = {"::".join(n.split("::")[:i]) for n in shown for i in range(1, n.count("::") + 2)}
        decks = [d for d in all_decks if d["name"] in keep]
        by_name = {d["name"]: d["id"] for d in decks}
        left = srs.new_left_today(self.db, now)
        everything = everything or srs.counts(self.db, None, now)
        return {
            "decks": [{
                "id": d["id"], "name": d["name"].rsplit("::", 1)[-1], "path": d["name"],
                "parent": by_name.get(d["name"].rsplit("::", 1)[0]) if "::" in d["name"] else None,
                "due": counts[d["id"]]["due"], "new": min(counts[d["id"]]["new"], left),
            } for d in decks],
            "deck": self.deck_id,
            "all_due": everything["due"], "all_new": everything["new"],
        }

    # -- the session ---------------------------------------------------------------------
    def start(self, limit: str = "") -> None:
        """Start a session: until nothing is left, or (limit "cards" or "time") a quick one."""
        self._session = gamify.Session(limit if limit in ("cards", "time") else "")
        self._summary = None
        self._skip = []
        self._next()

    def stop(self) -> None:
        if self._session is not None and self._session.reviewed:
            self._summary = self._session.summary(caught_up=False)
        self._session = None
        self.card_id, self._answer = None, None
        self._show_overview()

    def _next(self) -> None:
        if self._session is not None and self._session.over():
            self._finish(caught_up=False)
            return
        card = srs.next_card(self.db, self.scope, skip=self._skip, deck=self.deck, tag=self.section)
        self.card_id = card["id"] if card else None
        self._answer = None
        self._shown_at = time.monotonic()
        self._shown_data = ""
        if card is None:
            self._finish(caught_up=True)
        else:
            self._show_card()

    def _finish(self, caught_up: bool) -> None:
        """The session is over: everything is reviewed, or a quick session's time or cards are up."""
        session, self._session = self._session, None
        self.card_id, self._answer = None, None
        if session is None or not session.reviewed:
            self._show_overview()
            return
        self._summary = session.summary(caught_up)
        events = gamify.finished(self.db, caught_up, self._summary["quick"])
        self._show_overview()
        self._celebrate([*events, {"kind": "done", "caught_up": caught_up, "quick": self._summary["quick"]}])

    def _celebrate(self, events: list[dict]) -> None:
        if events and gamify.enabled(self.db):
            self._js("celebrate", events)

    def _show_card(self) -> None:
        card = self.db.card(self.card_id)
        if card["data"] != self._shown_data:
            self._question, self._shown_data = variants.question(card), card["data"]
        now = time.time()
        a = self._answer
        if a is None and card["id"] in self._grading:  # still being graded, e.g. after a refresh
            a = {"response": {"text": self._grading[card["id"]]}, "status": "pending", "verdict": None, "feedback": ""}
        c = srs.counts(self.db, self.scope, now, deck=self.deck, tag=self.section)
        book = self.db.book(card["book_id"]) if card["book_id"] is not None else None
        deck = self.db.deck(card["deck_id"])
        self._js("showCard", {
            "id": card["id"],
            "question": self._question,
            "varied": self._question != srs.question(card),
            "title": card["title"],
            "deck": deck["name"] if deck else "",
            "book": book["title"] if self.all_books and book else "",
            "page": card["page"] if book else None,
            "chat": card["message_id"] is not None,
            "new": srs.is_new(card),
            "answer": a,
            "previews": srs.previews(card, now, self.db) if a and a["status"] != "pending" else None,
            "suggested": srs.suggested_rating(a["verdict"]) if a else None,
            "remaining": {"due": c["due"], "new": c["new"]},
            "session": self._session.progress(c["due"] + c["new"]) if self._session else None,
            "rewards": gamify.enabled(self.db),
        })

    def check(self, card_id: int, response: dict) -> None:
        """An answer, or a request to see the answer (a flashcard's back, a free response's model answer)."""
        card = self.db.card(card_id)
        if card is None or card_id != self.card_id or self._answer is not None:
            return
        q = self._question
        verdict = None
        if q["type"] not in (*cardmodel.SELF_RATED, "free"):
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
        title, index = card_title(self.db, card)
        varied = self._question != srs.question(card)
        return card_job(self.db, card, quiz.review_grading_prompt(title, index, self._question, text, varied))

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
        seconds = time.monotonic() - self._shown_at
        duration = int(min(seconds, MAX_REVIEW_SECONDS) * 1000)
        before = self.db.card(card_id)
        srs.review(self.db, card_id, rating, response=a.get("response"), verdict=a.get("verdict"), duration=duration)
        self._grading.pop(card_id, None)
        if self.variants is not None:
            self.variants.after_review(card_id)
        session = self._session = self._session or gamify.Session()
        events = session.record(before, self.db.card(card_id), rating, seconds)
        events = gamify.reviewed(self.db, srs.is_new(before), session.multiplier, combo=session.best,
                                 session=session.reviewed) + events
        session.xp += sum(e["xp"] for e in events if e["kind"] == "xp")
        self._skip = [card_id]  # skipped cards may come back now
        self.countsChanged.emit()
        self._next()
        self._celebrate(events)

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
        elif action == "edit":
            self.editRequested.emit(card_id)
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
