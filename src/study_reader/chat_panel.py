"""The chat panels: a book's chats, the open transcript, and the composer. There are two, the
reader's (beside the open book) and the Review page's (beside the card under review, following it
from card to card); they share a ChatHub."""

from __future__ import annotations

import json
import uuid

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
    QPushButton, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from . import claude_client as cc
from . import gamify, quiz, srs
from .chat_service import BookRef, ChatWorker, Job
from .db import Database, data_dir, last_page
from .theme import icon, theme
from .transcript_view import TranscriptView

DEFAULT_QUESTION = "Explain this."
MODELS = [("Claude Code default", ""), ("Opus", "opus"), ("Sonnet", "sonnet"), ("Haiku", "haiku")]
CONTEXT_LABELS = {
    None: "The whole book is shared with Claude on your first question.",
    "full": "Whole book in context.",
    "partial": "Book too large to share whole — nearby pages are sent instead.",
}
# The Review page's: the book's sharing state after its title, or for cards with no book.
BOOKLESS_LABEL = "No book: Claude sees the card and this chat."
INPUT_LINES = (2, 10)  # composer height range, in lines


class ChatHub:
    """What the chat panels share: the replies on their way (chat id -> text so far) and the panel
    that asked for each, which alone handles its events, so a chat open in both panels streams once,
    is saved once, and can't be sent to twice at the same time."""

    def __init__(self):
        self.streams: dict[int, str] = {}
        self.owners: dict[int, ChatPanel] = {}
        self.panels: list[ChatPanel] = []


def drop_throwaway_chats(db: Database, worker: ChatWorker) -> None:
    """Throwaway chats left by a run that ended without leaving them (a crash), with their sessions."""
    for chat in db.throwaway_chats():
        sessions = db.chat_sessions(chat["id"])
        db.delete_chat(chat["id"])
        worker.forget_sessions(chat["book_id"], sessions, wait=False)  # nothing is running in them


class _Composer(QPlainTextEdit):
    submitted = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
        elif event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(event)


class ChatPanel(QWidget):
    jumpRequested = Signal(int, list)  # page, rects
    pageRequested = Signal(int)
    highlightsChanged = Signal()  # a chat, or the composer, gained or lost snippets
    cardsChanged = Signal()  # review cards were added, answered or put in or out of review
    listenRequested = Signal(int)  # message id: read this reply aloud

    def __init__(self, db: Database, worker: ChatWorker, variants=None, hub: ChatHub | None = None,
                 review: bool = False, parent=None):
        """review: the Review page's panel, whose chats are about the card on display (set_card):
        those of the card's book, or of cards with no book."""
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.variants = variants  # review.VariantMaker: the next version of calculation cards first answered here
        self.hub = hub or ChatHub()
        self.hub.panels.append(self)
        self.review = review
        self.book_id: int | None = None
        self.chat_id: int | None = None
        self.current_page = lambda: 0  # set by the main window
        self._pending: list[dict] = []
        # Review: the card on display (ReviewPanel.chat_card), sent with questions to its chats
        # unless its chip was removed (_card_off, until the next card).
        self._card: dict | None = None
        self._card_off = False
        self._throwaway = False  # new chats are throwaway: never listed, deleted when left
        # Chats still named after their first question (chat id -> that title): Claude names
        # them after the first reply, unless they were renamed meanwhile.
        self._untitled: dict[int, str] = {}
        self._naming: dict[int, str] = {}
        self._status = ""
        self._editing: int | None = None  # id of the message being rewritten in the composer
        self._draft: tuple[str, list[dict]] | None = None  # composer contents set aside while editing or quizzing
        # A Quiz me request drafted in the composer, sent (with whatever the user adds) by the next send.
        self._quiz: dict | None = None
        self._draft_model = self.default_model()  # for the chat the next send creates
        self._grading: dict[int, int] = {}  # quiz answer id -> its message id, while Claude grades it

        self.model = QComboBox()
        for label, value in MODELS:
            self.model.addItem(label, value)
        self.model.currentIndexChanged.connect(self._on_model_chosen)
        self.model.setToolTip("Model for this chat")
        self.list_toggle = QToolButton(objectName="section")
        self.list_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.list_toggle.setIconSize(QSize(14, 14))
        self.list_toggle.setToolTip("Show or hide the chat list")
        self.list_toggle.clicked.connect(self._toggle_list)
        new_btn = QToolButton()
        new_btn.setIcon(icon("message-plus"))
        new_btn.setIconSize(QSize(18, 18))
        new_btn.setToolTip("New chat (Ctrl+N)")
        new_btn.clicked.connect(lambda: self.new_chat())
        self.throwaway_btn = QToolButton(checkable=True)
        self.throwaway_btn.setIcon(icon("ghost"))
        self.throwaway_btn.setIconSize(QSize(18, 18))
        self.throwaway_btn.setToolTip("Throwaway chats: new chats aren't saved, and are gone when you leave them")
        self.throwaway_btn.toggled.connect(self.set_throwaway)
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.addWidget(self.list_toggle)
        header.addStretch()
        header.addWidget(self.model)
        header.addWidget(self.throwaway_btn)
        header.addWidget(new_btn)

        self.chat_list = QListWidget()
        self.chat_list.setIconSize(QSize(15, 15))
        self.chat_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.chat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_list.setEditTriggers(
            QListWidget.EditTrigger.DoubleClicked | QListWidget.EditTrigger.EditKeyPressed
        )
        self.chat_list.currentItemChanged.connect(self._on_chat_selected)
        self.chat_list.itemChanged.connect(self._on_renamed)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self._chat_menu)

        self.transcript = TranscriptView(listen=not review)  # read aloud's controls are under the reader
        self.transcript.jumpRequested.connect(self._on_jump)
        self.transcript.editRequested.connect(self.edit_message)
        self.transcript.regenerateRequested.connect(self.regenerate)
        self.transcript.pageRequested.connect(self.pageRequested)
        self.transcript.quizAnswered.connect(self._on_quiz_answer)
        self.transcript.quizRetake.connect(self._on_quiz_retake)
        self.transcript.quizRegrade.connect(self._on_quiz_regrade)
        self.transcript.quizReviewToggled.connect(self._on_quiz_review_toggled)
        self.transcript.quizReviseRequested.connect(self.revise_quiz)
        self.transcript.quizRequested.connect(self.quiz_on_reply)
        self.transcript.listenRequested.connect(self.listenRequested)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("role", "hint")
        self.status.setContentsMargins(6, 0, 6, 0)

        self.chips = QVBoxLayout()
        self.chips.setSpacing(4)
        self.input = _Composer(objectName="composerInput")
        self.input.setPlaceholderText("Ask about this card…" if review else "Ask about the book…")
        self.input.submitted.connect(self.send)
        self.input.cancelled.connect(self._on_escape)
        self.input.document().documentLayout().documentSizeChanged.connect(self._fit_input)
        self.input.installEventFilter(self)
        self.send_btn = QToolButton(objectName="send")
        self.send_btn.setIcon(icon("arrow-up", "accent_fg"))
        self.send_btn.setIconSize(QSize(18, 18))
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setToolTip("Send (Enter)")
        self.send_btn.clicked.connect(lambda: self.stop() if self.chat_id in self._streams else self.send())
        hint = QLabel("Enter to send · Shift+Enter for a new line")
        hint.setProperty("role", "hint")
        actions = QHBoxLayout()
        actions.setContentsMargins(4, 0, 0, 0)
        actions.addWidget(hint)
        actions.addStretch()
        actions.addWidget(self.send_btn)

        edit_label = QLabel("Editing message · later replies will be replaced")
        edit_label.setProperty("role", "hint")
        edit_label.setMinimumWidth(1)
        cancel_edit = QToolButton()
        cancel_edit.setIcon(icon("x", "muted"))
        cancel_edit.setIconSize(QSize(12, 12))
        cancel_edit.setFixedSize(20, 20)
        cancel_edit.setToolTip("Cancel editing (Esc)")
        cancel_edit.clicked.connect(self.cancel_edit)
        self.edit_bar = QWidget()
        edit_row = QHBoxLayout(self.edit_bar)
        edit_row.setContentsMargins(4, 0, 0, 0)
        edit_row.addWidget(edit_label, 1)
        edit_row.addWidget(cancel_edit)
        self.edit_bar.hide()

        throwaway_label = QLabel("Throwaway chat · not saved, gone when you leave it")
        throwaway_label.setProperty("role", "hint")
        throwaway_label.setMinimumWidth(1)
        self.save_btn = QPushButton("Save chat")
        self.save_btn.setToolTip("Keep this chat: it joins the chat list (and its quizzes your review cards)")
        self.save_btn.clicked.connect(self.save_throwaway)
        self.throwaway_bar = QWidget()
        throwaway_row = QHBoxLayout(self.throwaway_bar)
        throwaway_row.setContentsMargins(4, 0, 0, 0)
        throwaway_row.addWidget(QLabel(pixmap=icon("ghost", "muted").pixmap(14, 14)))
        throwaway_row.addWidget(throwaway_label, 1)
        throwaway_row.addWidget(self.save_btn)
        self.throwaway_bar.hide()

        self.composer = QFrame(objectName="composer")
        box = QVBoxLayout(self.composer)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(4)
        box.addWidget(self.throwaway_bar)
        box.addWidget(self.edit_bar)
        box.addLayout(self.chips)
        box.addWidget(self.input)
        box.addLayout(actions)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(6)
        lower_layout.addWidget(self.transcript, 1)
        lower_layout.addWidget(self.status)
        lower_layout.addWidget(self.composer)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.chat_list)
        split.addWidget(lower)
        split.setStretchFactor(1, 1)
        split.setSizes([130, 600])

        self.setObjectName("chatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(split, 1)

        theme().changed.connect(self._on_theme_changed)
        self._show_list(True)
        self._fit_input()

        worker.delta.connect(self._on_delta)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.base.connect(self._on_base)
        worker.status.connect(self._on_status)
        worker.graded.connect(self._on_graded)
        worker.gradeFailed.connect(self._on_grade_failed)
        worker.titled.connect(self._on_titled)
        self.set_book(None)

    # -- layout --------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.input and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.composer.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.composer.style().polish(self.composer)
        return super().eventFilter(obj, event)

    def _fit_input(self, *_) -> None:
        """Grow the composer with its text, within INPUT_LINES."""
        lines = min(max(int(self.input.document().documentLayout().documentSize().height()), INPUT_LINES[0]), INPUT_LINES[1])
        margin = 2 * (self.input.document().documentMargin() + self.input.frameWidth())
        self.input.setFixedHeight(int(lines * QFontMetrics(self.input.font()).lineSpacing() + margin))

    def _show_list(self, on: bool) -> None:
        self.chat_list.setVisible(on)
        self.list_toggle.setIcon(icon("chevron-down" if on else "chevron-right", "muted"))

    def _list_shown(self) -> bool:
        if self.review:
            return self.db.get_setting("review_chat_list") != "0"
        return self.book_id is None or bool(self.db.book(self.book_id)["show_chat_list"])

    def _toggle_list(self) -> None:
        """Collapse or expand the chat list, remembering it per book like the other panels (the
        Review page's once for all)."""
        on = not self.chat_list.isVisible()
        self._show_list(on)
        if self.review:
            self.db.set_setting("review_chat_list", "1" if on else "0")
        elif self.book_id is not None:
            self.db.update_book(self.book_id, show_chat_list=int(on))

    def _on_theme_changed(self) -> None:
        self._refresh_chips()
        self._refresh_transcript()

    # -- model -----------------------------------------------------------------------
    def default_model(self) -> str:
        return self.db.get_setting("model")

    def set_default_model(self, model: str) -> None:
        self.db.set_setting("model", model)
        if self.chat_id is None:
            self._draft_model = model
            self._sync_model()

    def _sync_model(self) -> None:
        chat = self.db.chat(self.chat_id) if self.chat_id is not None else None
        self.model.blockSignals(True)
        self.model.setCurrentIndex(max(self.model.findData(chat["model"] if chat else self._draft_model), 0))
        self.model.blockSignals(False)

    def _on_model_chosen(self) -> None:
        if self.chat_id is None:
            self._draft_model = self.model.currentData()
        else:
            self.db.update_chat(self.chat_id, model=self.model.currentData())

    # -- book / chat selection ---------------------------------------------------
    def set_book(self, book_id: int | None) -> None:
        """The book whose chats to show (the Review page's: None for those about cards with no book)."""
        self.cancel_edit()
        self.cancel_quiz()
        self._leave()
        self.book_id = book_id
        self.chat_id = None
        self._draft_model = self.default_model()
        self._status = ""
        self._pending.clear()
        self._refresh_chips()
        self.setEnabled(book_id is not None or self.review)
        self._show_list(self._list_shown())
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()
        self._refresh_status()

    def new_chat(self, focus: bool = True) -> None:
        self.cancel_edit()
        self.cancel_quiz()
        self._leave()
        self.chat_id = None
        self.chat_list.blockSignals(True)
        self.chat_list.setCurrentItem(None)
        self.chat_list.blockSignals(False)
        self._draft_model = self.default_model()
        self._sync_model()
        self._refresh_transcript()
        self._refresh_chips()
        if focus:
            self.input.setFocus()

    def open_chat(self, chat_id: int) -> None:
        if chat_id != self.chat_id:
            self.cancel_edit()
            self.cancel_quiz()
            self._leave(chat_id)
        self.chat_id = chat_id
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()
        self._refresh_chips()

    def show_chat(self, chat_id: int) -> None:
        """Open a chat, switching to its book's chats if need be (the Review page's Open chat)."""
        chat = self.db.chat(chat_id)
        if chat is None:
            return
        if chat["book_id"] != self.book_id:
            self.set_book(chat["book_id"])
        self.open_chat(chat_id)

    # -- the Review page's card ------------------------------------------------------------
    def set_card(self, card: dict | None) -> None:
        """The card on display on the Review page (ReviewPanel.chat_card; None for the overview),
        drawn again as it is answered. A new card brings up the latest chat about it, else the
        quiz chat it came from, else a new chat about it, in its book's chats."""
        new = card is not None and (self._card is None or self._card["id"] != card["id"])
        self._card = card
        if new:
            self._card_off = False
            chat = self.db.card_chat(card["id"])
            if chat is None and card.get("chat_id"):
                chat = self.db.chat(card["chat_id"])
            if chat is None or chat["id"] != self.chat_id:
                book_id = chat["book_id"] if chat is not None else card["book_id"]
                if book_id != self.book_id:
                    self.set_book(book_id)
                if chat is not None:
                    self.open_chat(chat["id"])
                else:
                    self.new_chat(focus=False)  # the review keeps the keyboard
        self._refresh_chips()

    def _card_attached(self) -> bool:
        """Whether the card on display goes with questions in the open chat: a new chat, or one
        about that card or the quiz chat it came from, unless its chip was removed."""
        if self._card is None or self._card_off:
            return False
        if self.chat_id is None:
            return True
        chat = self.db.chat(self.chat_id)
        return chat is not None and (chat["card_id"] == self._card["id"] or chat["id"] == self._card.get("chat_id"))

    @property
    def card(self) -> dict | None:
        """The Review page's card on display (set_card)."""
        return self._card

    def _drop_card(self) -> None:
        self._card_off = True
        self._refresh_chips()

    # -- throwaway chats -----------------------------------------------------------------
    def _is_throwaway(self, chat_id: int | None = None) -> bool:
        chat = self.db.chat(self.chat_id if chat_id is None else chat_id) if (chat_id or self.chat_id) else None
        return bool(chat and chat["throwaway"])

    def set_throwaway(self, on: bool) -> None:
        """The header's toggle: from now on new chats are (or aren't) throwaway. Turning it on in a
        saved chat starts a new one; turning it off in a throwaway chat leaves it (it is gone)."""
        if self.throwaway_btn.isChecked() != on:
            self.throwaway_btn.setChecked(on)  # comes back here through toggled
            return
        self._throwaway = on
        if self.chat_id is not None and self._is_throwaway() != on:
            self.new_chat()
        else:
            self._refresh_throwaway()
            self.input.setFocus()

    def _leave(self, next_chat: int | None = None) -> None:
        """The open chat is being left for next_chat: a throwaway one is deleted."""
        if self.chat_id is not None and self.chat_id != next_chat and self._is_throwaway():
            self._discard(self.chat_id)

    def _discard(self, chat_id: int, now: bool = False) -> None:
        """Delete a throwaway chat, stopping its reply, with the Claude sessions it was in (now:
        right away, as the app closes, rather than once the reply has stopped)."""
        chat = self.db.chat(chat_id)
        if chat is None:
            return
        if self.hub.owners.get(chat_id) is not None:
            self.worker.stop(chat_id)
            self.hub.streams.pop(chat_id, None)
            self.hub.owners.pop(chat_id, None)
        sessions = self.db.chat_sessions(chat_id)
        snippets = any(m["snippets"] for m in self.db.messages(chat_id))
        self.db.delete_chat(chat_id)
        self._untitled.pop(chat_id, None)
        self.worker.forget_sessions(chat["book_id"], sessions, wait=not now)
        if chat_id == self.chat_id:
            self.chat_id = None
        if snippets:
            self.highlightsChanged.emit()

    def close_throwaway(self) -> None:
        """The app is closing: delete the open throwaway chat."""
        if self.chat_id is not None and self._is_throwaway():
            self._discard(self.chat_id, now=True)

    def save_throwaway(self) -> None:
        """Keep the open throwaway chat: it joins the list, and its quizzes become review cards."""
        chat = self.db.chat(self.chat_id) if self.chat_id is not None else None
        if chat is None or not chat["throwaway"] or self.chat_id in self._streams:
            return
        self.db.update_chat(chat["id"], throwaway=0)
        self._throwaway = False
        self.throwaway_btn.blockSignals(True)
        self.throwaway_btn.setChecked(False)
        self.throwaway_btn.blockSignals(False)
        messages = self.db.messages(chat["id"])
        made = False
        for m in messages:
            if m["role"] == "assistant":
                made |= bool(srs.add_quiz_cards(self.db, chat["book_id"], m["id"], m["text"], deck_id=self._deck_for(chat)))
        if made:
            self.cardsChanged.emit()
        reply = next((m for m in reversed(messages) if m["role"] == "assistant" and m["text"].strip()), None)
        if reply is not None:
            self._name_chat(chat["id"], reply["text"])
        self._refresh_list()
        self._refresh_transcript()
        self._changed(chat["id"])

    def _deck_for(self, chat) -> int | None:
        """The deck for a chat's quiz cards: a bookless chat's card's (a book's chats use its deck)."""
        if chat["book_id"] is None and chat["card_id"] is not None:
            card = self.db.card(chat["card_id"])
            return card["deck_id"] if card is not None else None
        return None

    def _refresh_throwaway(self) -> None:
        on = self._is_throwaway() if self.chat_id is not None else self._throwaway
        self.throwaway_bar.setVisible(on)
        self.save_btn.setVisible(self.chat_id is not None)
        self.save_btn.setEnabled(self.chat_id not in self._streams)

    # -- the hub -------------------------------------------------------------------------
    @property
    def _streams(self) -> dict[int, str]:
        return self.hub.streams

    def _changed(self, chat_id: int) -> None:
        """Tell the other panel a chat changed (a message, its title, or it is gone)."""
        for p in self.hub.panels:
            if p is not self:
                p._on_peer_changed(chat_id)

    def _on_peer_changed(self, chat_id: int) -> None:
        chat = self.db.chat(chat_id)
        if chat_id == self.chat_id:
            if chat is None:
                self.cancel_edit()
                self.cancel_quiz()
                self.chat_id = None
                self._sync_model()
            self._refresh_transcript()
        if chat is None or chat["book_id"] == self.book_id:
            self._refresh_list()

    def add_snippet(self, snippet: dict, new_chat: bool) -> None:
        """snippet: dict(kind, page, text, rects, png) from the reader."""
        if self.book_id is None:
            return
        if new_chat:
            self.new_chat()  # first: leaving an edit puts the set-aside draft back
            self._pending.clear()
        self._pending.append(_stored(snippet))
        self._refresh_chips()
        self.input.setFocus()
        self.highlightsChanged.emit()

    def start_quiz(self, request: str, title: str, page: int, snippet: dict | None = None,
                   last: int | None = None) -> None:
        """Draft a quiz request in a new chat's composer, for the user to add to and send; the
        chat (named `title`) is made only when they do. snippet: a passage to quiz on, as the
        reader emits it; else the quiz covers pages page…last."""
        if self.book_id is None:
            return
        self.new_chat()
        snippets = [_stored(snippet)] if snippet else []
        last = page if last is None else last
        if snippet:
            what = " ".join(snippet["text"].split()) if snippet["kind"] == "text" else "Screenshot"
            label = f"{page_tag(snippet)} · {what}"
        else:
            name, pages = title.removeprefix("Quiz: "), quiz.pages_label(page, last)
            label = pages if name == pages else f"{name} · {pages}"
            label = label[:1].upper() + label[1:]
        self._draft_quiz(dict(
            request=request, title=" ".join(title.split())[:70], chat_id=None, page=page,
            pages=None if snippets else (page, last), snippets=snippets, label=label,
        ))

    def quiz_on_reply(self, message_id: int) -> None:
        """Draft, in the same chat, a request for a quiz on one of its replies."""
        if self.chat_id is None or self.chat_id in self._streams or self._editing is not None:
            return
        messages = self.db.messages(self.chat_id)
        reply = next((m for m in messages if m["id"] == message_id and m["role"] == "assistant"), None)
        if reply is None:
            return
        last = reply is next(m for m in reversed(messages) if m["role"] == "assistant")
        self.cancel_quiz()
        self._draft_quiz(dict(
            request=quiz.reply_request(reply["text"], last), title=None, chat_id=self.chat_id, page=None,
            pages=None, snippets=[], label="Claude's last reply" if last else "Claude's earlier reply",
        ))

    def _draft_quiz(self, pending: dict) -> None:
        """Set the composer's contents aside and put a quiz request there instead."""
        self._draft = (self.input.toPlainText(), self._pending)
        self._pending = []
        self._quiz = pending
        self.input.setPlainText(pending["request"])
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        self._refresh_chips()
        self.input.setFocus()
        if pending["snippets"]:
            self.highlightsChanged.emit()

    def cancel_quiz(self) -> None:
        """Drop a drafted quiz request, giving the composer back what it held before."""
        if self._quiz is None:
            return
        had_snippet = bool(self._quiz["snippets"])
        self._quiz = None
        self._restore_draft()
        if had_snippet:
            self.highlightsChanged.emit()

    def _send_quiz(self) -> None:
        q = self._quiz
        question = quiz.drafted_request(q["request"], self.input.toPlainText())
        snippets = q["snippets"] + self._pending
        if q["chat_id"] is None:
            self.chat_id = self.db.create_chat(self.book_id, q["title"], self._draft_model, throwaway=self._throwaway)
            prior = []
        else:
            prior = self.db.messages(self.chat_id)
        self.db.add_message(self.chat_id, "user", question, snippets, pages=q["pages"])
        self._ask(question, snippets, prior, branch=False, page=q["page"])
        self._quiz = None
        self._restore_draft()
        self._refresh_list()
        self._refresh_transcript()
        if snippets:
            self.highlightsChanged.emit()

    def revise_quiz(self, message_id: int, index: int, change: str | None = None) -> None:
        """Ask, in the same chat, for a revision of one of its quizzes (asking what to change
        unless given); Claude's revision takes over the cards of the questions it keeps."""
        if self.chat_id is None or self.chat_id in self._streams or self._editing is not None:
            return
        found = self._quiz_of(message_id, index)
        if found is None:
            return
        title = found[1]["title"]
        if change is None:
            change, ok = QInputDialog.getMultiLineText(
                self, "Revise quiz", f"What should change in “{title}”? (e.g. make question 3 harder, drop question 5, "
                "add a question on …)",
            )
            if not ok:
                return
        if not change.strip():
            return
        question = quiz.revise_request(title, change)
        messages = self.db.messages(self.chat_id)
        self.db.add_message(self.chat_id, "user", question)
        self._ask(question, [], messages, branch=False)
        self._refresh_list()
        self._refresh_transcript()

    def pending_snippets(self) -> list[dict]:
        """Snippets waiting in the composer, not yet saved with a message."""
        quizzed = self._quiz["snippets"] if self._quiz else []
        return [s for s in self._pending + quizzed if "id" not in s]  # an edited message's are saved already

    def _on_chat_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is not None:
            self.cancel_edit()
            self.cancel_quiz()
            self._leave(item.data(Qt.ItemDataRole.UserRole))
            self.chat_id = item.data(Qt.ItemDataRole.UserRole)
            self._sync_model()
            self._refresh_transcript()
            self._refresh_chips()

    def _chat_menu(self, pos) -> None:
        item = self.chat_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        rename.setShortcut("F2")
        delete = menu.addAction("Delete chat")
        chosen = menu.exec(self.chat_list.mapToGlobal(pos))
        if chosen is rename:
            self.chat_list.editItem(item)
        elif chosen is delete:
            chat_id = item.data(Qt.ItemDataRole.UserRole)
            if chat_id == self.chat_id:
                self.cancel_edit()
                self.cancel_quiz()
            self.db.delete_chat(chat_id)
            if chat_id == self.chat_id:
                self.chat_id = None
                self._draft_model = self.default_model()
                self._sync_model()
            self._refresh_list()
            self._refresh_transcript()
            self._changed(chat_id)
            self.highlightsChanged.emit()
            self.cardsChanged.emit()  # its unreviewed cards went with it

    def _on_renamed(self, item: QListWidgetItem) -> None:
        chat = self.db.chat(item.data(Qt.ItemDataRole.UserRole))
        if chat is None:
            return
        title = " ".join(item.text().split())
        self.chat_list.blockSignals(True)
        if title:
            self.db.update_chat(chat["id"], title=title)
            self._changed(chat["id"])
            item.setText(title)
            item.setToolTip(f"{title}\n\nDouble-click or F2 to rename")
        else:
            item.setText(chat["title"])
        self.chat_list.blockSignals(False)

    # -- sending -----------------------------------------------------------------
    def send(self) -> None:
        if (self.book_id is None and not self.review) or self.chat_id in self._streams:
            return
        if self._quiz is not None:
            self._send_quiz()
            return
        question = self.input.toPlainText().strip()
        snippets = list(self._pending)
        if not question and not snippets:
            return
        question = question or DEFAULT_QUESTION

        if self.chat_id is None:
            first = snippets[0] if snippets else None
            title = question if question != DEFAULT_QUESTION or not first else (
                first["text"] or f"Screenshot, p. {first['page'] + 1}"
            )
            title = " ".join(title.split())[:70]
            self.chat_id = self.db.create_chat(
                self.book_id, title, self._draft_model, card_id=self._card["id"] if self._card_attached() else None,
                throwaway=self._throwaway,
            )
            self._untitled[self.chat_id] = title
        prior = self.db.messages(self.chat_id)
        editing = self._editing
        if editing is not None:
            prior = [m for m in prior if m["id"] < editing]
            self.db.delete_messages_from(self.chat_id, editing)
            self.cardsChanged.emit()
        self.db.add_message(self.chat_id, "user", question, snippets)
        self._ask(question, snippets, prior, branch=editing is not None)

        if editing is not None:
            self._end_edit()
        else:
            self._pending.clear()
            self.input.clear()
            self._refresh_chips()
        self._refresh_list()
        self._refresh_transcript()
        self._changed(self.chat_id)
        if snippets or editing is not None:
            self.highlightsChanged.emit()

    def stop(self) -> None:
        """Stop the reply on its way to the open chat, keeping what arrived as a stopped reply."""
        owner = self.hub.owners.get(self.chat_id)
        if owner is not None:
            owner._stop(self.chat_id)

    def _stop(self, chat_id: int) -> None:
        text = self._streams.get(chat_id, "")
        self.worker.stop(chat_id)
        self._finish(chat_id, "assistant", text, stopped=True)
        if text.strip():
            self._name_chat(chat_id, text)

    def _on_escape(self) -> None:
        if self._editing is not None or self._quiz is not None:
            self.cancel_edit()
            self.cancel_quiz()
        elif self.chat_id in self._streams:
            self.stop()

    def regenerate(self, message_id: int) -> None:
        """Ask the question before this reply again, replacing the reply."""
        if self.chat_id is None or self.chat_id in self._streams or self._editing is not None:
            return
        messages = self.db.messages(self.chat_id)
        at = next((i for i, m in enumerate(messages) if m["id"] == message_id), None)
        asked = next((i for i in range(at, -1, -1) if messages[i]["role"] == "user"), None) if at is not None else None
        if asked is None or asked + 1 >= len(messages):
            return
        question = messages[asked]
        self.db.delete_messages_from(self.chat_id, messages[asked + 1]["id"])
        self.cardsChanged.emit()
        self._ask(question["text"], question["snippets"], messages[:asked], branch=True)
        self._refresh_transcript()

    def _ask(
        self, question: str, snippets: list[dict], prior: list[dict], branch: bool, page: int | None = None
    ) -> None:
        """Send a question that follows `prior`, the chat's saved messages before it.

        branch: later turns were dropped, so rather than continuing the chat's session, fork
        from the last reply in `prior` at that reply (or, with no reply or one saved before
        branch points were kept, start over from the book and replay `prior`)."""
        chat = self.db.chat(self.chat_id)
        session_id, resume_at = chat["session_id"], None
        if branch:
            # Until the new reply lands, nothing may continue the old conversation.
            self.db.update_chat(self.chat_id, session_id=None)
            session_id = None
            reply = next((m for m in reversed(prior) if m["role"] == "assistant"), None)
            if reply and reply["session_uuid"]:
                session_id, resume_at = reply["session_id"], reply["session_uuid"]
        if page is None:
            page = snippets[0]["page"] if snippets else self.current_page()
        context = [quiz.answers_context(prior, self.db.quiz_answers(self.chat_id)), srs.tags_context(self.db)]
        if self._card_attached():
            context.insert(0, self._card["context"])
        last = next((m for m in reversed(prior) if m["role"] != "error"), None)
        if last is not None and last["role"] == "assistant" and last["stopped"]:
            asked = next((m for m in reversed(prior[:prior.index(last)]) if m["role"] == "user"), None)
            context.insert(0, quiz.stopped_context(asked["text"] if asked else "", last["text"]))
        self.worker.submit(Job(
            chat_id=self.chat_id,
            book=self._book_ref(),
            chat_session_id=session_id,
            question=question,
            snippets=[cc.Snippet(s["kind"], s["page"], s.get("text", ""), s.get("image_path", ""),
                                 last_page(s["page"], s.get("rects", []))) for s in snippets],
            history=[(m["role"], m["text"]) for m in prior if m["role"] != "error"],
            page=page,
            model=chat["model"] or None,
            resume_at=resume_at,
            context="\n\n".join(c for c in context if c),
        ))
        self._streams[self.chat_id] = ""
        self.hub.owners[self.chat_id] = self

    def _book_ref(self) -> BookRef | None:
        book = self.db.book(self.book_id) if self.book_id is not None else None
        if book is None:
            return None
        return BookRef(book["id"], book["path"], book["title"], book["base_session_id"], book["context_mode"])

    # -- quizzes -------------------------------------------------------------------
    def _quiz_of(self, message_id: int, index: int) -> tuple[dict, dict] | None:
        """(message, quiz) for a quiz in one of the open chat's replies."""
        message = self.db.message(message_id)
        if message is None or message["chat_id"] != self.chat_id:
            return None
        quizzes = quiz.quizzes(message["text"])
        if not 0 <= index < len(quizzes) or "questions" not in quizzes[index]:
            return None
        return dict(message), quizzes[index]

    def _on_quiz_answer(self, message_id: int, index: int, question: int, response_json: str) -> None:
        found = self._quiz_of(message_id, index)
        if found is None:
            return
        message, q = found
        try:
            spec = q["questions"][question]
            response = json.loads(response_json)
        except (IndexError, ValueError):
            return
        if spec["type"] == "written":
            answer_id = self.db.save_quiz_answer(message_id, index, question, response_json, "pending")
            self._grade(message, q, question, answer_id, response.get("text", ""))
        else:
            try:
                verdict = quiz.grade(spec, response)
            except quiz.AnswerError as e:
                self.transcript.quiz_input_error(message_id, index, question, str(e))
                return
            self.db.save_quiz_answer(message_id, index, question, response_json, "done", verdict)
            if srs.answered_in_chat(self.db, message_id, index, question, verdict, response):
                self._first_review(message_id, index, question)
        self._update_quiz(message_id, index)

    def _first_review(self, message_id: int, index: int, question: int) -> None:
        """A quiz question's first answer was its card's first review."""
        gamify.reviewed(self.db, new=True)
        card = self.db.card_for(message_id, index, question)
        if self.variants is not None and card is not None:
            self.variants.after_review(card["id"])
        self.cardsChanged.emit()

    def _on_quiz_retake(self, message_id: int, index: int) -> None:
        if self._quiz_of(message_id, index) is not None:
            self.db.clear_quiz_answers(message_id, index)
            self._update_quiz(message_id, index)

    def _on_quiz_review_toggled(self, message_id: int, index: int) -> None:
        if self._quiz_of(message_id, index) is None:
            return
        cards = self.db.quiz_cards(message_id, index)
        if cards:
            self.db.set_quiz_suspended(message_id, index, not any(c["suspended"] for c in cards))
            self._update_quiz(message_id, index)
            self.cardsChanged.emit()

    def _on_quiz_regrade(self, message_id: int, index: int, question: int) -> None:
        found = self._quiz_of(message_id, index)
        if found is None:
            return
        row = next((r for r in self.db.quiz_answers(self.chat_id).get(message_id, [])
                    if r["quiz"] == index and r["question"] == question), None)
        if row is None or row["id"] in self._grading:
            return
        self.db.update_quiz_answer(row["id"], status="pending", feedback="")
        self._grade(*found, question, row["id"], json.loads(row["response"]).get("text", ""))
        self._update_quiz(message_id, index)

    def _grade(self, message: dict, q: dict, question: int, answer_id: int, text: str) -> None:
        """Have Claude grade a written answer, in a detached fork of the chat at the quiz's reply."""
        chat = self.db.chat(message["chat_id"])
        page = q["questions"][question]["page"]
        prior = [m for m in self.db.messages(message["chat_id"]) if m["id"] <= message["id"] and m["role"] != "error"]
        self._grading[answer_id] = message["id"]
        self.worker.submit_grade(Job(
            chat_id=message["chat_id"],
            book=self._book_ref(),
            chat_session_id=message["session_id"],
            question=quiz.grading_prompt(q, question, text),
            history=[(m["role"], m["text"]) for m in prior],
            page=page - 1 if page else self.current_page(),
            model=chat["model"] or None,
            resume_at=message["session_uuid"],
            detached=True,
        ), answer_id)

    def _on_graded(self, answer_id: int, text: str) -> None:
        verdict, feedback = quiz.parse_grade(text)
        self._graded(answer_id, status="done", verdict=verdict, feedback=feedback)

    def _on_grade_failed(self, answer_id: int, message: str) -> None:
        self._graded(answer_id, status="error", feedback=message)

    def _graded(self, answer_id: int, **fields) -> None:
        message_id = self._grading.pop(answer_id, None)
        row = self.db.quiz_answer(answer_id)
        if message_id is None or row is None:  # retaken or deleted meanwhile
            return
        self.db.update_quiz_answer(answer_id, **fields)
        if fields.get("verdict") and srs.answered_in_chat(
            self.db, message_id, row["quiz"], row["question"], fields["verdict"], json.loads(row["response"])
        ):
            self._first_review(message_id, row["quiz"], row["question"])
        message = self.db.message(message_id)
        if message is not None and message["chat_id"] == self.chat_id:
            self._update_quiz(message_id, row["quiz"])

    def _update_quiz(self, message_id: int, index: int) -> None:
        message = self.db.message(message_id)
        rows = self.db.quiz_answers(message["chat_id"]).get(message_id, [])
        revised = self._revisions(self.db.messages(message["chat_id"]))
        self.transcript.update_quiz(message_id, index, self._quiz_view(
            quiz.quizzes(message["text"])[index], index, rows, message_id, revised.get((message_id, index))))

    @staticmethod
    def _revisions(messages) -> dict[tuple[int, int], int]:
        """{(message id, quiz index): id of the reply whose quiz revises it}, latest revision winning."""
        earlier: list[tuple[int, int, dict]] = []
        out = {}
        for m in messages:
            if m["role"] != "assistant":
                continue
            quizzes = quiz.quizzes(m["text"])
            for q in quizzes:
                if q.get("revises") and (old := quiz.revised_quiz(earlier, q["revises"])):
                    out[old] = m["id"]
            earlier += [(m["id"], i, q) for i, q in enumerate(quizzes)]
        return out

    def _quiz_view(self, q: dict, index: int, rows, message_id: int, revised_by: int | None = None) -> dict:
        """A quiz as transcript.html draws it: with the answers given so far, whether its
        questions are in review ("on", "off", or None without cards), and the reply whose quiz
        revises it, if any."""
        answers = {}
        for r in rows:
            if r["quiz"] != index:
                continue
            status, feedback = r["status"], r["feedback"]
            if status == "pending" and r["id"] not in self._grading:  # the app closed mid-grading
                status, feedback = "error", "grading was interrupted"
            answers[r["question"]] = {
                "response": json.loads(r["response"]), "status": status, "verdict": r["verdict"], "feedback": feedback,
            }
        cards = self.db.quiz_cards(message_id, index) if "questions" in q else []
        review = ("off" if all(c["suspended"] for c in cards) else "on") if cards else None
        return {**q, "answers": answers, "review": review, "revised_by": revised_by}

    # -- editing -------------------------------------------------------------------
    def edit_message(self, message_id: int) -> None:
        """Load one of the chat's questions into the composer; sending replaces it and what follows."""
        if self.chat_id is None or self.chat_id in self._streams:
            return
        message = next((m for m in self.db.messages(self.chat_id) if m["id"] == message_id), None)
        if message is None or message["role"] != "user":
            return
        self.cancel_quiz()
        if self._editing is None:
            self._draft = (self.input.toPlainText(), self._pending)
        self._editing = message_id
        self._pending = [dict(s) for s in message["snippets"]]
        self.input.setPlainText(message["text"])
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        self.edit_bar.show()
        self._refresh_chips()
        self._refresh_transcript()
        self.input.setFocus()
        self.highlightsChanged.emit()

    def cancel_edit(self) -> None:
        if self._editing is not None:
            self._end_edit()
            self._refresh_transcript()
            self.highlightsChanged.emit()

    def _end_edit(self) -> None:
        """Leave editing, giving the composer back what it held before."""
        self._editing = None
        self.edit_bar.hide()
        self._restore_draft()

    def _restore_draft(self) -> None:
        """Give the composer back what it held before editing or a drafted quiz set it aside."""
        text, pending = self._draft or ("", [])
        self._draft = None
        self._pending = pending
        self.input.setPlainText(text)
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        self._refresh_chips()

    # -- worker events -------------------------------------------------------------
    def _on_delta(self, chat_id: int, text: str) -> None:
        if self.hub.owners.get(chat_id) is not self:  # the panel that asked handles its reply
            return
        self._streams[chat_id] += text
        for p in self.hub.panels:
            if p.chat_id == chat_id:
                p.transcript.set_streaming(self._streams[chat_id])

    def _finish(
        self, chat_id: int, role: str, text: str, session_id: str | None = None, session_uuid: str | None = None,
        stopped: bool = False,
    ) -> None:
        self._streams.pop(chat_id, None)
        self.hub.owners.pop(chat_id, None)
        chat = self.db.chat(chat_id)
        if chat is None:  # deleted while waiting
            return
        message_id = self.db.add_message(chat_id, role, text, session_id=session_id, session_uuid=session_uuid,
                                         stopped=stopped)
        if role == "assistant" and not chat["throwaway"] and srs.add_quiz_cards(
                self.db, chat["book_id"], message_id, text, deck_id=self._deck_for(chat)):
            self.cardsChanged.emit()
        if chat_id == self.chat_id:
            self._refresh_transcript()
        self._changed(chat_id)

    def _on_done(self, chat_id: int, session_id: str, text: str, message_uuid: str) -> None:
        if self.hub.owners.get(chat_id) is not self:
            return
        if self.db.chat(chat_id) is not None:
            self.db.update_chat(chat_id, session_id=session_id)
        self._finish(chat_id, "assistant", text, session_id, message_uuid or None)
        self._name_chat(chat_id, text)

    def _name_chat(self, chat_id: int, reply: str) -> None:
        """Ask for a title once the chat's first reply is in (a throwaway chat's once it is saved)."""
        if self._is_throwaway(chat_id):
            return
        title = self._untitled.pop(chat_id, None)
        messages = self.db.messages(chat_id) if title is not None and self.db.chat(chat_id) else []
        first = next((m for m in messages if m["role"] == "user"), None)
        if first is None:
            return
        passage = next((s["text"] for s in first["snippets"] if s["kind"] == "text" and s["text"]), "")
        self._naming[chat_id] = title
        self.worker.submit_title(chat_id, first["text"], passage, reply)

    def _on_titled(self, chat_id: int, title: str) -> None:
        chat = self.db.chat(chat_id)
        if chat is None or chat["title"] != self._naming.pop(chat_id, None):  # gone, or renamed by hand
            return
        self.db.update_chat(chat_id, title=title)
        # In place rather than _refresh_list, which would close a rename in progress.
        self.chat_list.blockSignals(True)
        for row in range(self.chat_list.count()):
            item = self.chat_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                item.setText(title)
                item.setToolTip(f"{title}\n\nDouble-click or F2 to rename")
        self.chat_list.blockSignals(False)
        self._changed(chat_id)

    def _on_failed(self, chat_id: int, message: str) -> None:
        if self.hub.owners.get(chat_id) is self:
            self._finish(chat_id, "error", f"Claude request failed: {message}")

    def _on_base(self, book_id: int, session_id: str, mode: str) -> None:
        self.db.update_book(book_id, base_session_id=session_id or None, context_mode=mode)
        self._refresh_status()

    def _on_status(self, book_id: int, text: str) -> None:
        if book_id == self.book_id:
            self._status = text
            self._refresh_status()

    def _on_jump(self, snippet_id: int) -> None:
        s = self.db.snippet(snippet_id)
        if s:
            self.jumpRequested.emit(s["page"], s["rects"])

    # -- refresh -------------------------------------------------------------------
    def _refresh_status(self) -> None:
        book = self.db.book(self.book_id) if self.book_id is not None else None
        label = CONTEXT_LABELS[book["context_mode"]] if book else ""
        if self.review:  # which book's chats these are, as they follow the cards
            label = f"{book['title']} — {label}" if book else BOOKLESS_LABEL
        self.status.setText(self._status or label)

    def _refresh_list(self) -> None:
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        chats = self.db.chats(self.book_id) if self.book_id is not None or self.review else []
        self.list_toggle.setText(f"Chats · {len(chats)}" if chats else "Chats")
        for chat in chats:
            card = chat["card_id"] is not None
            item = QListWidgetItem(icon("cards" if card else "message", "muted"), chat["title"])
            item.setData(Qt.ItemDataRole.UserRole, chat["id"])
            about = "\n\nAbout a review card" if card else ""
            item.setToolTip(f"{chat['title']}{about}\n\nDouble-click or F2 to rename")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.chat_list.addItem(item)
            if chat["id"] == self.chat_id:
                self.chat_list.setCurrentItem(item)
        self.chat_list.blockSignals(False)

    def _refresh_transcript(self) -> None:
        messages = self.db.messages(self.chat_id) if self.chat_id is not None else []
        answers = self.db.quiz_answers(self.chat_id) if messages else {}
        revised = self._revisions(messages)
        for m in messages:
            if m["role"] == "assistant":
                m["text"], quizzes = quiz.split(m["text"])
                m["quizzes"] = [self._quiz_view(q, i, answers.get(m["id"], []), m["id"], revised.get((m["id"], i)))
                                for i, q in enumerate(quizzes)]
        if self.review:
            placeholder = ("Ask about the card on the left: Claude sees it, but won't give the answer away "
                           "until you've answered." if self._card is not None else
                           "Start a review to ask Claude about its cards.")
        elif self.book_id is not None:
            placeholder = "Select text or capture a region in the book to ask about it."
        else:
            placeholder = "Open a book to start."
        if self._is_throwaway() or (self.chat_id is None and self._throwaway):
            placeholder = "Throwaway chat: it isn't saved, and is gone when you leave it."
        self.transcript.show_messages(messages, self._streams.get(self.chat_id), placeholder, self._editing)
        streaming = self.chat_id in self._streams
        self.send_btn.setIcon(icon("stop" if streaming else "arrow-up", "accent_fg"))
        self.send_btn.setToolTip("Stop the reply (Esc)" if streaming else "Send (Enter)")
        self._refresh_throwaway()

    def _refresh_chips(self) -> None:
        while self.chips.count():
            old = self.chips.takeAt(0).widget()
            old.hide()
            old.deleteLater()
        if self._card_attached():
            seen = "with its answer" if self._card["revealed"] else "answer kept back"
            self.chips.addWidget(self._chip(
                "cards", "Card", f"{self._card['title']} · {seen}", self._drop_card,
                "Don't send the card with this chat's questions (until the next card)",
                "Claude sees this card with your questions; until you've answered, it won't give the answer away",
            ))
        if self._quiz is not None:
            self.chips.addWidget(self._chip(
                "quiz", "Quiz", self._quiz["label"], self.cancel_quiz, "Don't ask for a quiz (Esc)",
                "Sends a quiz request, with anything you add here as instructions",
            ))
        for snippet in self._pending:
            is_text = snippet["kind"] == "text"
            self.chips.addWidget(self._chip(
                "text" if is_text else "image", page_tag(snippet),
                " ".join(snippet["text"].split())[:80] if is_text else "Screenshot",
                lambda s=snippet: self._remove_pending(s), "Remove",
            ))

    def _chip(self, icon_name: str, tag: str, text: str, remove, remove_tip: str, tip: str = "") -> QFrame:
        """A composer attachment: icon, tag, text and a remove button."""
        kind = QLabel()
        kind.setPixmap(icon(icon_name, "accent").pixmap(QSize(14, 14), self.devicePixelRatioF()))
        page = QLabel(tag)
        page.setProperty("role", "page")
        label = QLabel(text[:80])
        label.setProperty("role", "muted")
        label.setMinimumWidth(1)
        button = QToolButton()
        button.setIcon(icon("x", "muted"))
        button.setIconSize(QSize(12, 12))
        button.setFixedSize(20, 20)
        button.setToolTip(remove_tip)
        button.clicked.connect(lambda _=False: remove())
        chip = QFrame(objectName="chip")
        chip.setToolTip(tip)
        row = QHBoxLayout(chip)
        row.setContentsMargins(8, 2, 2, 2)
        row.setSpacing(6)
        row.addWidget(kind)
        row.addWidget(page)
        row.addWidget(label, 1)
        row.addWidget(button)
        return chip

    def _remove_pending(self, snippet: dict) -> None:
        self._pending = [s for s in self._pending if s is not snippet]
        self._refresh_chips()
        self.highlightsChanged.emit()


def page_tag(snippet: dict) -> str:
    """Where a snippet is: "p. 12", or "pp. 12–13" for a passage selected across pages."""
    first, last = snippet["page"], last_page(snippet["page"], snippet.get("rects", []))
    return f"p. {first + 1}" if last == first else f"pp. {first + 1}–{last + 1}"


def _stored(snippet: dict) -> dict:
    """A snippet from the reader, with its screenshot (if any) saved to a file."""
    snippet = dict(snippet)
    if png := snippet.pop("png", None):
        folder = data_dir() / "snippets"
        folder.mkdir(exist_ok=True)
        path = folder / f"{uuid.uuid4().hex}.png"
        path.write_bytes(png)
        snippet["image_path"] = str(path)
    return snippet
