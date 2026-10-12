"""Right-hand panel: the book's chats, the open transcript, and the composer."""

from __future__ import annotations

import json
import uuid

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
    QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from . import claude_client as cc
from . import gamify, quiz, srs
from .chat_service import BookRef, ChatWorker, Job
from .db import Database, data_dir
from .theme import icon, theme
from .transcript_view import TranscriptView

DEFAULT_QUESTION = "Explain this."
MODELS = [("Claude Code default", ""), ("Opus", "opus"), ("Sonnet", "sonnet"), ("Haiku", "haiku")]
CONTEXT_LABELS = {
    None: "The whole book is shared with Claude on your first question.",
    "full": "Whole book in context.",
    "partial": "Book too large to share whole — nearby pages are sent instead.",
}
INPUT_LINES = (2, 10)  # composer height range, in lines


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

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.book_id: int | None = None
        self.chat_id: int | None = None
        self.current_page = lambda: 0  # set by the main window
        self._pending: list[dict] = []
        self._streams: dict[int, str] = {}  # chat id -> reply text so far
        # Chats still named after their first question (chat id -> that title): Claude names
        # them after the first reply, unless they were renamed meanwhile.
        self._untitled: dict[int, str] = {}
        self._naming: dict[int, str] = {}
        self._status = ""
        self._editing: int | None = None  # id of the message being rewritten in the composer
        self._draft: tuple[str, list[dict]] | None = None  # composer contents set aside while editing
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
        new_btn.clicked.connect(self.new_chat)
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.addWidget(self.list_toggle)
        header.addStretch()
        header.addWidget(self.model)
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

        self.transcript = TranscriptView()
        self.transcript.jumpRequested.connect(self._on_jump)
        self.transcript.editRequested.connect(self.edit_message)
        self.transcript.regenerateRequested.connect(self.regenerate)
        self.transcript.pageRequested.connect(self.pageRequested)
        self.transcript.quizAnswered.connect(self._on_quiz_answer)
        self.transcript.quizRetake.connect(self._on_quiz_retake)
        self.transcript.quizRegrade.connect(self._on_quiz_regrade)
        self.transcript.quizReviewToggled.connect(self._on_quiz_review_toggled)
        self.transcript.quizRequested.connect(self.quiz_on_reply)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("role", "hint")
        self.status.setContentsMargins(6, 0, 6, 0)

        self.chips = QVBoxLayout()
        self.chips.setSpacing(4)
        self.input = _Composer(objectName="composerInput")
        self.input.setPlaceholderText("Ask about the book…")
        self.input.submitted.connect(self.send)
        self.input.cancelled.connect(self.cancel_edit)
        self.input.document().documentLayout().documentSizeChanged.connect(self._fit_input)
        self.input.installEventFilter(self)
        self.send_btn = QToolButton(objectName="send")
        self.send_btn.setIcon(icon("arrow-up", "accent_fg"))
        self.send_btn.setIconSize(QSize(18, 18))
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setToolTip("Send (Enter)")
        self.send_btn.clicked.connect(self.send)
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

        self.composer = QFrame(objectName="composer")
        box = QVBoxLayout(self.composer)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(4)
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

    def _toggle_list(self) -> None:
        """Collapse or expand the chat list, remembering it per book like the other panels."""
        on = not self.chat_list.isVisible()
        self._show_list(on)
        if self.book_id is not None:
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
        self.cancel_edit()
        self.book_id = book_id
        self.chat_id = None
        self._draft_model = self.default_model()
        self._status = ""
        self._pending.clear()
        self._refresh_chips()
        self.setEnabled(book_id is not None)
        self._show_list(book_id is None or bool(self.db.book(book_id)["show_chat_list"]))
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()
        self._refresh_status()

    def new_chat(self) -> None:
        self.cancel_edit()
        self.chat_id = None
        self.chat_list.blockSignals(True)
        self.chat_list.setCurrentItem(None)
        self.chat_list.blockSignals(False)
        self._draft_model = self.default_model()
        self._sync_model()
        self._refresh_transcript()
        self.input.setFocus()

    def open_chat(self, chat_id: int) -> None:
        if chat_id != self.chat_id:
            self.cancel_edit()
        self.chat_id = chat_id
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()

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

    def start_quiz(self, request: str, title: str, page: int, snippet: dict | None = None) -> None:
        """Open a new chat asking Claude for a quiz; the composer's draft is left as it was.
        snippet: a passage to quiz on, as the reader emits it."""
        if self.book_id is None:
            return
        self.new_chat()
        snippets = [_stored(snippet)] if snippet else []
        self.chat_id = self.db.create_chat(self.book_id, " ".join(title.split())[:70], self._draft_model)
        self.db.add_message(self.chat_id, "user", request, snippets)
        self._ask(request, snippets, [], branch=False, page=page)
        self._refresh_list()
        self._refresh_transcript()
        if snippets:
            self.highlightsChanged.emit()

    def quiz_on_reply(self, message_id: int) -> None:
        """Ask, in the same chat, for a quiz on one of its replies."""
        if self.chat_id is None or self.chat_id in self._streams or self._editing is not None:
            return
        messages = self.db.messages(self.chat_id)
        reply = next((m for m in messages if m["id"] == message_id and m["role"] == "assistant"), None)
        if reply is None:
            return
        last = reply is next(m for m in reversed(messages) if m["role"] == "assistant")
        question = quiz.reply_request(reply["text"], last)
        self.db.add_message(self.chat_id, "user", question)
        self._ask(question, [], messages, branch=False)
        self._refresh_list()
        self._refresh_transcript()

    def pending_snippets(self) -> list[dict]:
        """Snippets waiting in the composer, not yet saved with a message."""
        return [s for s in self._pending if "id" not in s]  # an edited message's are saved already

    def _on_chat_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is not None:
            self.cancel_edit()
            self.chat_id = item.data(Qt.ItemDataRole.UserRole)
            self._sync_model()
            self._refresh_transcript()

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
            self.db.delete_chat(chat_id)
            if chat_id == self.chat_id:
                self.chat_id = None
                self._draft_model = self.default_model()
                self._sync_model()
            self._refresh_list()
            self._refresh_transcript()
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
            item.setText(title)
            item.setToolTip(f"{title}\n\nDouble-click or F2 to rename")
        else:
            item.setText(chat["title"])
        self.chat_list.blockSignals(False)

    # -- sending -----------------------------------------------------------------
    def send(self) -> None:
        if self.book_id is None or self.chat_id in self._streams:
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
            self.chat_id = self.db.create_chat(self.book_id, title, self._draft_model)
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
        if snippets or editing is not None:
            self.highlightsChanged.emit()

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
        self.worker.submit(Job(
            chat_id=self.chat_id,
            book=self._book_ref(),
            chat_session_id=session_id,
            question=question,
            snippets=[cc.Snippet(s["kind"], s["page"], s.get("text", ""), s.get("image_path", "")) for s in snippets],
            history=[(m["role"], m["text"]) for m in prior if m["role"] != "error"],
            page=page,
            model=chat["model"] or None,
            resume_at=resume_at,
            context=quiz.answers_context(prior, self.db.quiz_answers(self.chat_id)),
        ))
        self._streams[self.chat_id] = ""

    def _book_ref(self) -> BookRef:
        book = self.db.book(self.book_id)
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
        if spec["type"] == "free":
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
                gamify.reviewed(self.db, new=True)
                self.cardsChanged.emit()
        self._update_quiz(message_id, index)

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
        """Have Claude grade a free response, in a detached fork of the chat at the quiz's reply."""
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
            gamify.reviewed(self.db, new=True)
            self.cardsChanged.emit()
        message = self.db.message(message_id)
        if message is not None and message["chat_id"] == self.chat_id:
            self._update_quiz(message_id, row["quiz"])

    def _update_quiz(self, message_id: int, index: int) -> None:
        message = self.db.message(message_id)
        rows = self.db.quiz_answers(message["chat_id"]).get(message_id, [])
        self.transcript.update_quiz(message_id, index, self._quiz_view(quiz.quizzes(message["text"])[index], index, rows, message_id))

    def _quiz_view(self, q: dict, index: int, rows, message_id: int) -> dict:
        """A quiz as transcript.html draws it: with the answers given so far, and whether its
        questions are in review ("on", "off", or None without cards)."""
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
        return {**q, "answers": answers, "review": review}

    # -- editing -------------------------------------------------------------------
    def edit_message(self, message_id: int) -> None:
        """Load one of the chat's questions into the composer; sending replaces it and what follows."""
        if self.chat_id is None or self.chat_id in self._streams:
            return
        message = next((m for m in self.db.messages(self.chat_id) if m["id"] == message_id), None)
        if message is None or message["role"] != "user":
            return
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
        text, pending = self._draft or ("", [])
        self._editing, self._draft = None, None
        self._pending = pending
        self.input.setPlainText(text)
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        self.edit_bar.hide()
        self._refresh_chips()

    # -- worker events -------------------------------------------------------------
    def _on_delta(self, chat_id: int, text: str) -> None:
        if chat_id not in self._streams:
            return
        self._streams[chat_id] += text
        if chat_id == self.chat_id:
            self.transcript.set_streaming(self._streams[chat_id])

    def _finish(
        self, chat_id: int, role: str, text: str, session_id: str | None = None, session_uuid: str | None = None
    ) -> None:
        self._streams.pop(chat_id, None)
        chat = self.db.chat(chat_id)
        if chat is None:  # deleted while waiting
            return
        message_id = self.db.add_message(chat_id, role, text, session_id=session_id, session_uuid=session_uuid)
        if role == "assistant" and srs.add_quiz_cards(self.db, chat["book_id"], message_id, text):
            self.cardsChanged.emit()
        if chat_id == self.chat_id:
            self._refresh_transcript()

    def _on_done(self, chat_id: int, session_id: str, text: str, message_uuid: str) -> None:
        if self.db.chat(chat_id) is not None:
            self.db.update_chat(chat_id, session_id=session_id)
        self._finish(chat_id, "assistant", text, session_id, message_uuid or None)
        self._name_chat(chat_id, text)

    def _name_chat(self, chat_id: int, reply: str) -> None:
        """Ask for a title once the chat's first reply is in."""
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

    def _on_failed(self, chat_id: int, message: str) -> None:
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
        self.status.setText(self._status or (CONTEXT_LABELS[book["context_mode"]] if book else ""))

    def _refresh_list(self) -> None:
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        chats = self.db.chats(self.book_id) if self.book_id is not None else []
        self.list_toggle.setText(f"Chats · {len(chats)}" if chats else "Chats")
        for chat in chats:
            item = QListWidgetItem(icon("message", "muted"), chat["title"])
            item.setData(Qt.ItemDataRole.UserRole, chat["id"])
            item.setToolTip(f"{chat['title']}\n\nDouble-click or F2 to rename")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.chat_list.addItem(item)
            if chat["id"] == self.chat_id:
                self.chat_list.setCurrentItem(item)
        self.chat_list.blockSignals(False)

    def _refresh_transcript(self) -> None:
        messages = self.db.messages(self.chat_id) if self.chat_id is not None else []
        answers = self.db.quiz_answers(self.chat_id) if messages else {}
        for m in messages:
            if m["role"] == "assistant":
                m["text"], quizzes = quiz.split(m["text"])
                m["quizzes"] = [self._quiz_view(q, i, answers.get(m["id"], []), m["id"]) for i, q in enumerate(quizzes)]
        placeholder = (
            "Select text or capture a region in the book to ask about it." if self.book_id is not None
            else "Open a book to start."
        )
        self.transcript.show_messages(messages, self._streams.get(self.chat_id), placeholder, self._editing)
        self.send_btn.setEnabled(self.chat_id not in self._streams)

    def _refresh_chips(self) -> None:
        while self.chips.count():
            old = self.chips.takeAt(0).widget()
            old.hide()
            old.deleteLater()
        for snippet in self._pending:
            is_text = snippet["kind"] == "text"
            kind = QLabel()
            kind.setPixmap(icon("text" if is_text else "image", "accent").pixmap(QSize(14, 14), self.devicePixelRatioF()))
            page = QLabel(f"p. {snippet['page'] + 1}")
            page.setProperty("role", "page")
            text = QLabel(" ".join(snippet["text"].split())[:80] if is_text else "Screenshot")
            text.setProperty("role", "muted")
            text.setMinimumWidth(1)
            remove = QToolButton()
            remove.setIcon(icon("x", "muted"))
            remove.setIconSize(QSize(12, 12))
            remove.setFixedSize(20, 20)
            remove.setToolTip("Remove")
            remove.clicked.connect(lambda _=False, s=snippet: self._remove_pending(s))
            chip = QFrame(objectName="chip")
            row = QHBoxLayout(chip)
            row.setContentsMargins(8, 2, 2, 2)
            row.setSpacing(6)
            row.addWidget(kind)
            row.addWidget(page)
            row.addWidget(text, 1)
            row.addWidget(remove)
            self.chips.addWidget(chip)

    def _remove_pending(self, snippet: dict) -> None:
        self._pending = [s for s in self._pending if s is not snippet]
        self._refresh_chips()
        self.highlightsChanged.emit()


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
