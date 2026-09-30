"""Right-hand panel: the book's chats, the open transcript, and the composer."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
    QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from . import claude_client as cc
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

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
        else:
            super().keyPressEvent(event)


class ChatPanel(QWidget):
    jumpRequested = Signal(int, list)  # page, rects
    highlightsChanged = Signal()  # a chat gained or lost snippets

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.book_id: int | None = None
        self.chat_id: int | None = None
        self.current_page = lambda: 0  # set by the main window
        self._pending: list[dict] = []
        self._streams: dict[int, str] = {}  # chat id -> reply text so far
        self._status = ""
        self._draft_model = self.default_model()  # for the chat the next send creates

        self.model = QComboBox()
        for label, value in MODELS:
            self.model.addItem(label, value)
        self.model.currentIndexChanged.connect(self._on_model_chosen)
        self.model.setToolTip("Model for this chat")
        self.list_toggle = QToolButton(objectName="section")
        self.list_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.list_toggle.setIconSize(QSize(14, 14))
        self.list_toggle.setToolTip("Show or hide the chat list")
        self.list_toggle.clicked.connect(lambda: self._show_list(not self.chat_list.isVisible()))
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
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("role", "hint")
        self.status.setContentsMargins(6, 0, 6, 0)

        self.chips = QVBoxLayout()
        self.chips.setSpacing(4)
        self.input = _Composer(objectName="composerInput")
        self.input.setPlaceholderText("Ask about the book…")
        self.input.submitted.connect(self.send)
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

        self.composer = QFrame(objectName="composer")
        box = QVBoxLayout(self.composer)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(4)
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
        self.book_id = book_id
        self.chat_id = None
        self._draft_model = self.default_model()
        self._status = ""
        self._pending.clear()
        self._refresh_chips()
        self.setEnabled(book_id is not None)
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()
        self._refresh_status()

    def new_chat(self) -> None:
        self.chat_id = None
        self.chat_list.blockSignals(True)
        self.chat_list.setCurrentItem(None)
        self.chat_list.blockSignals(False)
        self._draft_model = self.default_model()
        self._sync_model()
        self._refresh_transcript()
        self.input.setFocus()

    def open_chat(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self._refresh_list()
        self._sync_model()
        self._refresh_transcript()

    def add_snippet(self, snippet: dict, new_chat: bool) -> None:
        """snippet: dict(kind, page, text, rects, png) from the reader."""
        if self.book_id is None:
            return
        if new_chat:
            self._pending.clear()
            self.new_chat()
        if snippet.get("png"):
            folder = data_dir() / "snippets"
            folder.mkdir(exist_ok=True)
            path = folder / f"{uuid.uuid4().hex}.png"
            path.write_bytes(snippet["png"])
            snippet = {**snippet, "image_path": str(path)}
        snippet.pop("png", None)
        self._pending.append(snippet)
        self._refresh_chips()
        self.input.setFocus()

    def _on_chat_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is not None:
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
            self.db.delete_chat(chat_id)
            if chat_id == self.chat_id:
                self.chat_id = None
                self._draft_model = self.default_model()
                self._sync_model()
            self._refresh_list()
            self._refresh_transcript()
            self.highlightsChanged.emit()

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
        book = self.db.book(self.book_id)

        if self.chat_id is None:
            first = snippets[0] if snippets else None
            title = question if question != DEFAULT_QUESTION or not first else (
                first["text"] or f"Screenshot, p. {first['page'] + 1}"
            )
            self.chat_id = self.db.create_chat(self.book_id, " ".join(title.split())[:70], self._draft_model)
        chat = self.db.chat(self.chat_id)
        history = [(m["role"], m["text"]) for m in self.db.messages(self.chat_id) if m["role"] != "error"]
        self.db.add_message(self.chat_id, "user", question, snippets)

        self.worker.submit(Job(
            chat_id=self.chat_id,
            book=BookRef(book["id"], book["path"], book["title"], book["base_session_id"], book["context_mode"]),
            chat_session_id=chat["session_id"],
            question=question,
            snippets=[cc.Snippet(s["kind"], s["page"], s.get("text", ""), s.get("image_path", "")) for s in snippets],
            history=history,
            page=snippets[0]["page"] if snippets else self.current_page(),
            model=chat["model"] or None,
        ))
        self._streams[self.chat_id] = ""
        self._pending.clear()
        self.input.clear()
        self._refresh_chips()
        self._refresh_list()
        self._refresh_transcript()
        if snippets:
            self.highlightsChanged.emit()

    # -- worker events -------------------------------------------------------------
    def _on_delta(self, chat_id: int, text: str) -> None:
        if chat_id not in self._streams:
            return
        self._streams[chat_id] += text
        if chat_id == self.chat_id:
            self.transcript.set_streaming(self._streams[chat_id])

    def _finish(self, chat_id: int, role: str, text: str) -> None:
        self._streams.pop(chat_id, None)
        if self.db.chat(chat_id) is None:  # deleted while waiting
            return
        self.db.add_message(chat_id, role, text)
        if chat_id == self.chat_id:
            self._refresh_transcript()

    def _on_done(self, chat_id: int, session_id: str, text: str) -> None:
        if self.db.chat(chat_id) is not None:
            self.db.update_chat(chat_id, session_id=session_id)
        self._finish(chat_id, "assistant", text)

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
        placeholder = (
            "Select text or capture a region in the book to ask about it." if self.book_id is not None
            else "Open a book to start."
        )
        self.transcript.show_messages(messages, self._streams.get(self.chat_id), placeholder)
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
