"""Practicing quiz questions, each brought back right before it would be forgotten (scheduled by
practice.py): the PracticePanel, rendered in practice.html, which fills the Practice page."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from . import question_model, dashboard, gamify, quiz, practice, variants
from .chat_service import ChatWorker, Job
from .db import Database, media_url
from .notes import LocalPage
from .theme import theme

ASSETS = Path(__file__).parent / "assets"
# Longer than this on one question counts as this long (the user probably stepped away).
MAX_ATTEMPT_SECONDS = 120


def when(t: float, now: float) -> str:
    """'in 5m', 'in 3d' …"""
    return f"in {practice.format_interval(t - now)}"


def question_title(db: Database, row) -> tuple[str, int | None]:
    """How to name a question to Claude: (its quiz's title, its index in the quiz), or, if it isn't
    from a quiz in a chat, (its deck, None)."""
    if row["message_id"] is not None and db.message(row["message_id"]) is not None:
        return row["title"], row["position"]
    deck = db.deck(row["deck_id"])
    return (deck["name"] if deck else ""), None


class VariantMaker(QObject):
    """After a calculation, short-answer or multiple-choice question is attempted, has Claude write its next version,
    with new numbers or new choices (variants.py, in a fresh bookless session), and keeps it on the
    question for practice to show. Failures are silent: the question shows its last version again."""

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._pending: dict[int, str] = {}  # question id -> its data when asked, to drop answers an edit outdated
        worker.varied.connect(self._on_varied)
        worker.variantFailed.connect(lambda question_id, _: self._pending.pop(question_id, None))

    def after_attempt(self, question_id: int) -> None:
        row = self.db.question(question_id)
        if row is None or question_id in self._pending or not variants.enabled(self.db) \
                or not variants.wants_variant(row):
            return
        self._pending[question_id] = row["data"]
        last = variants.question(row) if variants.is_varied(row) else None
        book = self.db.book(row["book_id"]) if row["book_id"] is not None else None
        title, _ = question_title(self.db, row)
        message = self.db.message(row["message_id"]) if row["message_id"] is not None else None
        chat = self.db.chat(message["chat_id"]) if message else None
        prompt = variants.request(practice.question(row), last, book["title"] if book else "", title)
        # Without the book: the problem and its solution are enough, and far cheaper to send.
        self.worker.submit_variant(Job(
            chat_id=0, book=None, chat_session_id=None, message=prompt,
            model=(chat["model"] if chat else "") or self.db.get_setting("model") or None,
        ), question_id)

    def _on_varied(self, question_id: int, text: str) -> None:
        data = self._pending.pop(question_id, None)
        row = self.db.question(question_id)
        if row is None or row["data"] != data:
            return
        try:
            variant = variants.parse(text, practice.question(row))
        except (ValueError, TypeError, KeyError):
            return
        self.db.update_question(question_id, variant=question_model.dumps(variant))


class _Bridge(QObject):
    """What practice.html calls through the web channel."""

    def __init__(self, panel: PracticePanel):
        super().__init__(panel)
        self.panel = panel

    @Slot(str)
    def start(self, limit: str) -> None:
        self.panel.start(limit)

    @Slot()
    def stop(self) -> None:
        self.panel.stop()

    @Slot(int, str)
    def check(self, question_id: int, response: str) -> None:
        self.panel.check(question_id, json.loads(response))

    @Slot(int, int)
    def rate(self, question_id: int, rating: int) -> None:
        self.panel.rate(question_id, rating)

    @Slot(int, str)
    def act(self, question_id: int, action: str) -> None:
        self.panel.act(question_id, action)

    @Slot(str)
    def choose(self, picks: str) -> None:
        self.panel.choose(json.loads(picks))

    @Slot()
    def refresh(self) -> None:
        self.panel.refresh()

    @Slot(str)
    def open(self, what: str) -> None:
        if what == "calc":
            self.panel.calculatorRequested.emit()
        elif what == "chat":
            self.panel.chatPanelRequested.emit()
        elif what == "make":
            self.panel.makeRequested.emit(self.panel.picked_deck() or 0)


class PracticePanel(QWidget):
    """An overview of what is due (every deck and book, or the decks, books and sections picked), then
    one question at a time: answer it (checked where it can be), then rate it Missed / Shaky / Got it / Easy.
    variants: asks for the next version of each question attempted that has one (variants.py)."""

    sourceRequested = Signal(int, int)  # book id, 0-based page (-1 if unknown)
    chatRequested = Signal(int)  # chat id: a question's Open chat
    countsChanged = Signal()  # a question was attempted, suspended or deleted
    editRequested = Signal(int)  # question id, to open in the browser
    calculatorRequested = Signal()  # the Calculator button: show or hide the calculator beside it
    chatPanelRequested = Signal()  # the Chat button: show or hide the chat panel beside it
    makeRequested = Signal(int)  # Make a deck with Claude, under the deck picked (0: none, or several)
    questionShown = Signal(object)  # the book id (None if it has none) of the question on display, each time it is drawn
    questionChanged = Signal()  # a question was drawn (new, or answered), or the overview: see chat_question

    def __init__(self, db: Database, variants: VariantMaker | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.variants = variants
        # What to practice: any of these decks (with their subdecks), books and sections of books (tags,
        # practice.section_tag), each {"deck": id} or {"book": id, "section": tag or None}; none for every question.
        self.picks: list[dict] = []
        self.question_id: int | None = None  # the question on display; None for the overview
        self._shown_at = 0.0  # when the question on display was first shown (monotonic), for its attempt's duration
        # The question on display (content) and the row data it was shown for: kept while it is up,
        # so a new version arriving meanwhile doesn't replace it (an edit to the question does).
        self._question: dict | None = None
        self._shown_data = ""
        self._answer: dict | None = None  # its answer so far: {response, status, verdict, feedback}
        self._just: list[int] = []  # the question just answered, not to be shown again right away
        self._skipped: list[int] = []  # questions skipped this session, not to be shown again until the next one
        self._session: gamify.Session | None = None  # the session under way
        self._summary: dict | None = None  # how the last session went, for the overview
        # Whether the calculator beside the panel is open; None draws no Calculator button (set_calculator).
        self.calculator: bool | None = None
        self.chat_panel: bool | None = None  # the same for the chat panel (set_chat_panel)
        self.make_link = False  # draw the Make a deck with Claude link (the desktop's; never the phone's)

        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
        self.view.page().setBackgroundColor(QColor(theme().colors["window"]))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("practice", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "practice.html")))

        self.setObjectName("library")  # a page: the window's surface
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        # Learning questions fall due minutes apart; keep the overview's counts current
        # (while it is on screen: the page is refreshed whenever it is shown).
        self._timer = QTimer(self, interval=60_000)
        self._timer.timeout.connect(lambda: self.question_id is None and self.isVisible() and self.refresh())
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

    def set_chat_panel(self, on: bool) -> None:
        """Draw a Chat button showing whether the chat panel beside the panel is open."""
        self.chat_panel = on
        self._js("setChatPanel", on)

    def chat_question(self) -> dict | None:
        """The question on display, for the chat panel beside it (ChatPanel.set_question): its ids, a
        title, its 0-based page (or None), whether its answer has been shown, and how it is described to
        Claude; None for the overview."""
        row = self.db.question(self.question_id) if self.question_id is not None else None
        if row is None or self._question is None:
            return None
        where, number = question_title(self.db, row)
        title = f"{where}, question {number + 1}" if number is not None else where
        message = self.db.message(row["message_id"]) if row["message_id"] is not None else None
        return {
            "id": row["id"], "book_id": row["book_id"], "chat_id": message["chat_id"] if message else None,
            "title": title or "Question", "revealed": self._answer is not None, "page": row["page"],
            "context": quiz.question_context(self._question, title, self._answer),
        }

    def _apply_theme(self) -> None:
        self.view.page().setBackgroundColor(QColor(theme().colors["window"]))
        self._js("setTheme", theme().colors, theme().dark)

    def focus(self) -> None:
        self.view.setFocus()

    # -- scope ---------------------------------------------------------------------------
    def choose(self, picks: list[dict]) -> None:
        """Practice the questions in any of picks (see self.picks; the overview drops those gone since)."""
        self.picks = []
        for p in picks:
            if p.get("deck"):
                p = {"deck": int(p["deck"])}
            elif p.get("book"):
                p = {"book": int(p["book"]), "section": p.get("section") or None}
            else:
                continue
            if p not in self.picks:
                self.picks.append(p)
        self.stop()

    def choose_deck(self, deck_id: int | None) -> None:
        """Practice one deck (and its subdecks); None for every question."""
        self.choose([{"deck": deck_id}] if deck_id else [])

    def choose_book(self, book_id: int | None, section: str | None = None) -> None:
        """Practice one book's questions (whatever their decks), or one of its sections."""
        self.choose([{"book": book_id, "section": section}] if book_id else [])

    def picked_deck(self) -> int | None:
        """The deck picked, if just one is."""
        decks = [p["deck"] for p in self.picks if "deck" in p]
        return decks[0] if len(decks) == 1 else None

    def _still_there(self, pick: dict) -> dict | None:
        """pick, or (a section whose questions are gone or retagged) its book, or None if it is gone."""
        if "deck" in pick:
            return pick if self.db.deck(pick["deck"]) is not None else None
        if self.db.book(pick["book"]) is None:
            return None
        if pick["section"] is not None and not self.db.count_questions(pick["book"], tag=pick["section"]):
            return {"book": pick["book"], "section": None}
        return pick

    def refresh(self) -> None:
        """Redraw: the question on display (unless it is gone) or the overview."""
        if self.question_id is not None and self.db.question(self.question_id) is not None:
            self._show_question()
        else:
            self.question_id = None
            self._show_overview()

    def _show_overview(self) -> None:
        now = time.time()
        picks = []
        for p in map(self._still_there, self.picks):
            if p is not None and p not in picks:
                picks.append(p)
        self.picks = picks
        c = practice.counts(self.db, now, picks=self.picks)
        upcoming = practice.next_due(self.db, now, picks=self.picks)
        overview = {
            **c,
            "title": self._scope_title(),
            "picks": self.picks,
            "ready": practice.next_question(self.db, now, picks=self.picks) is not None,
            "next": when(upcoming, now) if upcoming else "",
            "attempted": self._summary["attempted"] if self._summary else 0,
            "summary": self._summary,
            "new_per_day": practice.new_per_day(self.db),
            "game": gamify.status(self.db, now) if gamify.enabled(self.db) else None,
            "quick": {"questions": gamify.QUICK_QUESTIONS, "minutes": gamify.QUICK_SECONDS // 60},
            "calc": self.calculator,
            "chat_panel": self.chat_panel,
            "make": self.make_link,
            **self._decks_overview(now, None if self.picks else c),
            **self._books_overview(now),
        }
        self._js("showOverview", overview)
        self.questionChanged.emit()

    def _scope_title(self) -> str:
        """What is being practiced, for the overview's heading ('' for everything)."""
        names = [self._pick_title(p) for p in self.picks]
        if len(names) > 3:
            names = [*names[:2], f"{len(names) - 2} more"]
        if len(names) < 2:
            return names[0] if names else ""
        return ", ".join(names[:-1]) + " and " + names[-1]

    def _pick_title(self, pick: dict) -> str:
        if "deck" in pick:
            deck = self.db.deck(pick["deck"])
            return deck["name"].replace("::", " › ") if deck else ""
        book = self.db.book(pick["book"])
        tag = pick["section"]
        section = " › ".join(p.replace("_", " ") for p in tag.split("::")[1:]) if tag else ""
        return " › ".join(t for t in (book["title"] if book else "", section) if t)

    def _books_overview(self, now: float) -> dict:
        """The books with questions, each with its counts and sections, for picking what to practice."""
        left = practice.new_left_today(self.db, now)
        counts = dashboard.book_counts(self.db, now)
        titles = {r["id"]: r["title"] for r in self.db.conn.execute("SELECT id, title FROM books")}
        sections = practice.sections(self.db, now)
        return {
            "books": [{
                "id": book_id, "title": titles[book_id], "due": c["due"], "new": c["new"],
                "sections": [{**n, "new": min(n["new"], left)} for n in sections.get(book_id, [])],
            } for book_id, c in sorted(counts.items(), key=lambda kv: titles.get(kv[0], "").casefold())
              if book_id in titles],
        }

    def _decks_overview(self, now: float, everything: dict | None = None) -> dict:
        """The deck list for picking what to practice (dashboard.deck_rows). everything: practice.counts
        over all decks, if already known."""
        everything = everything or practice.counts(self.db, now)
        return {
            "decks": dashboard.deck_rows(self.db, now),
            "all_due": everything["due"], "all_new": everything["new"],
        }

    # -- the session ---------------------------------------------------------------------
    def start(self, limit: str = "") -> None:
        """Start a session: until nothing is left, or (limit "questions", a count of them, or "time") a
        quick one."""
        self._session = gamify.Session(limit if limit in ("questions", "time") else "")
        self._summary = None
        self._just, self._skipped = [], []
        self._next()

    def stop(self) -> None:
        if self._session is not None and self._session.attempted:
            self._summary = self._session.summary(caught_up=False)
        self._session = None
        self.question_id, self._answer = None, None
        self._show_overview()

    def _next(self) -> None:
        if self._session is not None and self._session.over():
            self._finish(caught_up=False)
            return
        row = practice.next_question(self.db, skip=self._just, exclude=self._skipped, picks=self.picks)
        self.question_id = row["id"] if row else None
        self._answer = None
        self._shown_at = time.monotonic()
        self._shown_data = ""
        if row is None:
            # Only skipped questions left (if any): not caught up while they are still to practice.
            left = self._skipped and practice.next_question(self.db, picks=self.picks)
            self._finish(caught_up=not left)
        else:
            self._show_question()

    def _finish(self, caught_up: bool) -> None:
        """The session is over: everything is practiced, or a quick session's time or questions are up."""
        session, self._session = self._session, None
        self.question_id, self._answer = None, None
        if session is None or not session.attempted:
            self._show_overview()
            return
        self._summary = session.summary(caught_up)
        events = gamify.finished(self.db, caught_up, self._summary["quick"])
        self._show_overview()
        self._celebrate([*events, {"kind": "done", "caught_up": caught_up, "quick": self._summary["quick"]}])

    def _celebrate(self, events: list[dict]) -> None:
        if events and gamify.enabled(self.db):
            self._js("celebrate", events)

    def _show_question(self) -> None:
        row = self.db.question(self.question_id)
        if row["data"] != self._shown_data:
            self._question, self._shown_data = variants.question(row, self.db), row["data"]
        now = time.time()
        a = self._answer
        c = practice.counts(self.db, now, picks=self.picks)
        # The session's progress leaves out questions skipped in it, which won't come back in it.
        left = (practice.left_to_practice(self.db, None, now, exclude=self._skipped, picks=self.picks)
                if self._skipped else c["due"] + c["new"])
        book = self.db.book(row["book_id"]) if row["book_id"] is not None else None
        deck = self.db.deck(row["deck_id"])
        self.questionShown.emit(row["book_id"])
        self._js("showQuestion", {
            "id": row["id"],
            "question": self._question,
            "varied": self._question != practice.question(row),
            "title": row["title"],
            "deck": deck["name"] if deck else "",
            "book": book["title"] if book else "",
            "page": row["page"] if book else None,
            "chat": row["message_id"] is not None,
            "new": practice.is_new(row),
            "answer": a,
            "previews": practice.previews(row, now, self.db) if a else None,
            "suggested": practice.suggested_rating(a["verdict"]) if a else None,
            "remaining": {"due": c["due"], "new": c["new"]},
            "session": self._session.progress(left) if self._session else None,
            "rewards": gamify.enabled(self.db),
            "calc": self.calculator,
            "chat_panel": self.chat_panel,
        })
        self.questionChanged.emit()

    def check(self, question_id: int, response: dict) -> None:
        """An answer, or a request to see the answer (a self-check's back, or a short answer's model
        answer beside whatever was typed: short answers are self-rated here)."""
        row = self.db.question(question_id)
        if row is None or question_id != self.question_id or self._answer is not None:
            return
        q = self._question
        verdict = None
        if q["type"] not in (*question_model.SELF_RATED, "short_answer"):
            try:
                verdict = quiz.grade(q, response)
            except quiz.AnswerError as e:
                self._js("inputError", question_id, str(e))
                return
        self._answer = {"response": response, "status": "done", "verdict": verdict, "feedback": ""}
        self._show_question()

    def rate(self, question_id: int, rating: int) -> None:
        if question_id != self.question_id or rating not in quiz.RATINGS:
            return
        a = self._answer or {}
        seconds = time.monotonic() - self._shown_at
        duration = int(min(seconds, MAX_ATTEMPT_SECONDS) * 1000)
        before = self.db.question(question_id)
        practice.attempt(self.db, question_id, rating, response=a.get("response"), verdict=a.get("verdict"), duration=duration)
        if self.variants is not None:
            self.variants.after_attempt(question_id)
        session = self._session = self._session or gamify.Session()
        events = session.record(before, self.db.question(question_id), rating, seconds)
        events = gamify.attempted(self.db, practice.is_new(before), session.multiplier, combo=session.best,
                                  session=session.attempted) + events
        session.xp += sum(e["xp"] for e in events if e["kind"] == "xp")
        self._just = [question_id]
        self.countsChanged.emit()
        self._next()
        self._celebrate(events)

    def act(self, question_id: int, action: str) -> None:
        row = self.db.question(question_id)
        if row is None:
            return
        if action == "source":
            self.sourceRequested.emit(row["book_id"], row["page"] if row["page"] is not None else -1)
        elif action == "chat":
            message = self.db.message(row["message_id"]) if row["message_id"] is not None else None
            if message:
                self.chatRequested.emit(message["chat_id"])
        elif action == "edit":
            self.editRequested.emit(question_id)
        elif action == "skip":
            self._skipped.append(question_id)
            self._next()
        elif action == "suspend":
            self.db.update_question(question_id, suspended=1)
            self.countsChanged.emit()
            self._next()
        elif action == "delete":
            answer = QMessageBox.question(
                self, "Delete question",
                "Delete this question and its history of attempts? (Suspend keeps it out of practice instead.)",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.db.delete_question(question_id)
                self.countsChanged.emit()
                self._next()
