"""Reviewing quiz questions as spaced-repetition cards (scheduled by srs.py): the ReviewPanel,
rendered in review.html, which fills the Review page."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from . import cardmodel, dashboard, gamify, quiz, srs, variants
from .chat_service import ChatWorker, Job
from .db import Database, media_url
from .notes import LocalPage
from .theme import theme

ASSETS = Path(__file__).parent / "assets"
# Longer than this on one card counts as this long (the reviewer probably stepped away).
MAX_REVIEW_SECONDS = 120


def when(t: float, now: float) -> str:
    """'in 5m', 'in 3d' …"""
    return f"in {srs.format_interval(t - now)}"


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
        if card is None or card_id in self._pending or not variants.enabled(self.db) \
                or not variants.wants_variant(card):
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
        if what == "calc":
            self.panel.calculatorRequested.emit()


class ReviewPanel(QWidget):
    """An overview of what is due (every deck and book, or the deck, book or section picked), then
    one card at a time: answer it (checked where it can be), then rate it Again / Hard / Good / Easy.
    variants: asks for the next version of each calculation card reviewed."""

    sourceRequested = Signal(int, int)  # book id, 0-based page (-1 if unknown)
    chatRequested = Signal(int, int)  # book id, chat id
    countsChanged = Signal()  # a card was reviewed, suspended or deleted
    editRequested = Signal(int)  # card id, to open in the browser
    calculatorRequested = Signal()  # the Calculator button: show or hide the calculator beside it
    cardShown = Signal(object)  # the book id (None if it has none) of the card on display, each time it is drawn

    def __init__(self, db: Database, variants: VariantMaker | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.variants = variants
        self.deck_id: int | None = None  # the deck (and its subdecks) to review; None for all
        self.book_id: int | None = None  # the book to review instead, if any
        self.section: str | None = None  # the section (tag, srs.section_tag) of the book to review, if any
        self.card_id: int | None = None  # the card on display; None for the overview
        self._shown_at = 0.0  # when the card on display was first shown (monotonic), for its review time
        # The question on display and the card data it was shown for: kept while the card is up,
        # so a new version arriving meanwhile doesn't replace it (an edit to the card does).
        self._question: dict | None = None
        self._shown_data = ""
        self._answer: dict | None = None  # its answer so far: {response, status, verdict, feedback}
        self._just: list[int] = []  # the card just answered, not to be shown again right away
        self._skipped: list[int] = []  # cards skipped this session, not to be shown again until the next one
        self._session: gamify.Session | None = None  # the session under way
        self._summary: dict | None = None  # how the last session went, for the overview
        # Whether the calculator beside the panel is open; None draws no Calculator button (set_calculator).
        self.calculator: bool | None = None

        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
        self.view.page().setBackgroundColor(QColor(theme().colors["window"]))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("review", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "review.html")))

        self.setObjectName("library")  # a page: the window's surface
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        # Learning cards fall due minutes apart; keep the overview's counts current.
        self._timer = QTimer(self, interval=60_000)
        self._timer.timeout.connect(lambda: self.card_id is None and self.refresh())
        self._timer.start()
        self.refresh()

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

    def set_calculator(self, on: bool) -> None:
        """Draw a Calculator button showing whether the calculator beside the panel is open."""
        self.calculator = on
        self._js("setCalc", on)

    def _apply_theme(self) -> None:
        self.view.page().setBackgroundColor(QColor(theme().colors["window"]))
        self._js("setTheme", theme().colors, theme().dark)

    def focus(self) -> None:
        self.view.setFocus()

    # -- scope ---------------------------------------------------------------------------
    def choose_deck(self, deck_id: int | None) -> None:
        self.deck_id = deck_id  # the overview drops it if the deck is gone
        self.book_id, self.section = None, None
        self.stop()

    def choose_book(self, book_id: int | None) -> None:
        """Review one book's cards (whatever their decks)."""
        self.deck_id, self.book_id, self.section = None, book_id, None
        self.stop()

    def choose_section(self, book_id: int | None, tag: str | None) -> None:
        """Review one section of a book; None for the whole book."""
        self.deck_id, self.book_id, self.section = None, book_id, tag
        self.stop()

    def refresh(self) -> None:
        """Redraw: the card on display (unless it is gone) or the overview."""
        if self.card_id is not None and self.db.card(self.card_id) is not None:
            self._show_card()
        else:
            self.card_id = None
            self._show_overview()

    def _show_overview(self) -> None:
        now = time.time()
        if self.deck_id is not None and self.db.deck(self.deck_id) is None:
            self.deck_id = None
        if self.book_id is not None and self.db.book(self.book_id) is None:
            self.book_id, self.section = None, None
        if self.section is not None and not self.db.count_cards(self.book_id, tag=self.section):
            self.section = None  # its cards are gone (or retagged)
        c = srs.counts(self.db, self.book_id, now, deck=self.deck_id, tag=self.section)
        upcoming = srs.next_due(self.db, self.book_id, now, deck=self.deck_id, tag=self.section)
        overview = {
            **c,
            "title": self._scope_title(),
            "section": self.section,
            "ready": srs.next_card(self.db, self.book_id, now, deck=self.deck_id, tag=self.section) is not None,
            "next": when(upcoming, now) if upcoming else "",
            "reviewed": self._summary["reviewed"] if self._summary else 0,
            "summary": self._summary,
            "new_per_day": srs.new_per_day(self.db),
            "game": gamify.status(self.db, now) if gamify.enabled(self.db) else None,
            "quick": {"cards": gamify.QUICK_CARDS, "minutes": gamify.QUICK_SECONDS // 60},
            "calc": self.calculator,
            **self._decks_overview(now, c if self.book_id is None and self.deck_id is None else None),
            **self._books_overview(now),
        }
        self._js("showOverview", overview)

    def _scope_title(self) -> str:
        """What is being reviewed, for the overview's heading ('' for everything)."""
        section = " › ".join(p.replace("_", " ") for p in self.section.split("::")[1:]) if self.section else ""
        if self.book_id is not None:
            book = self.db.book(self.book_id)
            return " › ".join(t for t in (book["title"] if book else "", section) if t)
        deck = self.db.deck(self.deck_id) if self.deck_id is not None else None
        return deck["name"].replace("::", " › ") if deck else ""

    def _books_overview(self, now: float) -> dict:
        """The books with cards, each with its counts and sections, for picking what to review."""
        left = srs.new_left_today(self.db, now)
        counts = dashboard.book_counts(self.db, now)
        titles = {r["id"]: r["title"] for r in self.db.conn.execute("SELECT id, title FROM books")}
        sections = srs.sections(self.db, now)
        return {
            "books": [{
                "id": book_id, "title": titles[book_id], "due": c["due"], "new": c["new"],
                "sections": [{**n, "new": min(n["new"], left)} for n in sections.get(book_id, [])],
            } for book_id, c in sorted(counts.items(), key=lambda kv: titles.get(kv[0], "").casefold())
              if book_id in titles],
            "book": self.book_id,
        }

    def _decks_overview(self, now: float, everything: dict | None = None) -> dict:
        """The deck list for picking what to review (dashboard.deck_rows). everything: srs.counts
        over all decks, if already known."""
        everything = everything or srs.counts(self.db, None, now)
        return {
            "decks": dashboard.deck_rows(self.db, now),
            "deck": self.deck_id,
            "all_due": everything["due"], "all_new": everything["new"],
        }

    # -- the session ---------------------------------------------------------------------
    def start(self, limit: str = "") -> None:
        """Start a session: until nothing is left, or (limit "cards" or "time") a quick one."""
        self._session = gamify.Session(limit if limit in ("cards", "time") else "")
        self._summary = None
        self._just, self._skipped = [], []
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
        card = srs.next_card(self.db, self.book_id, skip=self._just, deck=self.deck_id, tag=self.section,
                             exclude=self._skipped)
        self.card_id = card["id"] if card else None
        self._answer = None
        self._shown_at = time.monotonic()
        self._shown_data = ""
        if card is None:
            # Only skipped cards left (if any): not caught up while they are still to review.
            left = self._skipped and srs.next_card(self.db, self.book_id, deck=self.deck_id, tag=self.section)
            self._finish(caught_up=not left)
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
            self._question, self._shown_data = variants.question(card, self.db), card["data"]
        now = time.time()
        a = self._answer
        c = srs.counts(self.db, self.book_id, now, deck=self.deck_id, tag=self.section)
        # The session's progress leaves out cards skipped in it, which won't come back in it.
        left = (srs.left_to_review(self.db, self.book_id, now, deck=self.deck_id, tag=self.section, exclude=self._skipped)
                if self._skipped else c["due"] + c["new"])
        book = self.db.book(card["book_id"]) if card["book_id"] is not None else None
        deck = self.db.deck(card["deck_id"])
        self.cardShown.emit(card["book_id"])
        self._js("showCard", {
            "id": card["id"],
            "question": self._question,
            "varied": self._question != srs.question(card),
            "title": card["title"],
            "deck": deck["name"] if deck else "",
            "book": book["title"] if book else "",
            "page": card["page"] if book else None,
            "chat": card["message_id"] is not None,
            "new": srs.is_new(card),
            "answer": a,
            "previews": srs.previews(card, now, self.db) if a else None,
            "suggested": srs.suggested_rating(a["verdict"]) if a else None,
            "remaining": {"due": c["due"], "new": c["new"]},
            "session": self._session.progress(left) if self._session else None,
            "rewards": gamify.enabled(self.db),
            "calc": self.calculator,
        })

    def check(self, card_id: int, response: dict) -> None:
        """An answer, or a request to see the answer (a flashcard's back, or a written answer's model
        answer beside whatever was typed: written answers are self-rated here)."""
        card = self.db.card(card_id)
        if card is None or card_id != self.card_id or self._answer is not None:
            return
        q = self._question
        verdict = None
        if q["type"] not in (*cardmodel.SELF_RATED, "written"):
            try:
                verdict = quiz.grade(q, response)
            except quiz.AnswerError as e:
                self._js("inputError", card_id, str(e))
                return
        self._answer = {"response": response, "status": "done", "verdict": verdict, "feedback": ""}
        self._show_card()

    def rate(self, card_id: int, rating: int) -> None:
        if card_id != self.card_id or rating not in quiz.RATINGS:
            return
        a = self._answer or {}
        seconds = time.monotonic() - self._shown_at
        duration = int(min(seconds, MAX_REVIEW_SECONDS) * 1000)
        before = self.db.card(card_id)
        srs.review(self.db, card_id, rating, response=a.get("response"), verdict=a.get("verdict"), duration=duration)
        if self.variants is not None:
            self.variants.after_review(card_id)
        session = self._session = self._session or gamify.Session()
        events = session.record(before, self.db.card(card_id), rating, seconds)
        events = gamify.reviewed(self.db, srs.is_new(before), session.multiplier, combo=session.best,
                                 session=session.reviewed) + events
        session.xp += sum(e["xp"] for e in events if e["kind"] == "xp")
        self._just = [card_id]
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
            self._skipped.append(card_id)
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
