"""Right-hand panel: the book's chats, the open transcript, and the composer."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from . import claude_client as cc
from .chat_service import BookRef, ChatWorker, Job
from .db import Database, data_dir
from .transcript_view import TranscriptView

DEFAULT_QUESTION = "Explain this."
MODELS = [("Default model", ""), ("Opus", "opus"), ("Sonnet", "sonnet"), ("Haiku", "haiku")]
CONTEXT_LABELS = {
    None: "The whole book is shared with Claude on your first question.",
    "full": "Whole book in context.",
    "partial": "Book too large to share whole — nearby pages are sent instead.",
}


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

        self.model = QComboBox()
        for label, value in MODELS:
            self.model.addItem(label, value)
        self.model.setCurrentIndex(max(self.model.findData(db.get_setting("model")), 0))
        self.model.currentIndexChanged.connect(lambda: db.set_setting("model", self.model.currentData()))
        new_btn = QPushButton("New chat")
        new_btn.clicked.connect(self.new_chat)
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Chats</b>"))
        header.addStretch()
        header.addWidget(self.model)
        header.addWidget(new_btn)

        self.chat_list = QListWidget()
        self.chat_list.currentItemChanged.connect(self._on_chat_selected)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self._chat_menu)

        self.transcript = TranscriptView()
        self.transcript.jumpRequested.connect(self._on_jump)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")

        self.chips = QVBoxLayout()
        self.chips.setSpacing(2)
        self.input = _Composer()
        self.input.setPlaceholderText("Ask about the book…  (Enter to send, Shift+Enter for a new line)")
        self.input.setFixedHeight(80)
        self.input.submitted.connect(self.send)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)
        bottom = QHBoxLayout()
        bottom.addWidget(self.input)
        bottom.addWidget(self.send_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.transcript, 1)
        lower_layout.addWidget(self.status)
        lower_layout.addLayout(self.chips)
        lower_layout.addLayout(bottom)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.chat_list)
        split.addWidget(lower)
        split.setStretchFactor(1, 1)
        split.setSizes([110, 600])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(header)
        layout.addWidget(split, 1)

        worker.delta.connect(self._on_delta)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.base.connect(self._on_base)
        worker.status.connect(self._on_status)
        self.set_book(None)

    # -- book / chat selection ---------------------------------------------------
    def set_book(self, book_id: int | None) -> None:
        self.book_id = book_id
        self.chat_id = None
        self._status = ""
        self._pending.clear()
        self._refresh_chips()
        self.setEnabled(book_id is not None)
        self._refresh_list()
        self._refresh_transcript()
        self._refresh_status()

    def new_chat(self) -> None:
        self.chat_id = None
        self.chat_list.blockSignals(True)
        self.chat_list.setCurrentItem(None)
        self.chat_list.blockSignals(False)
        self._refresh_transcript()
        self.input.setFocus()

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
            self._refresh_transcript()

    def _chat_menu(self, pos) -> None:
        item = self.chat_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        delete = menu.addAction("Delete chat")
        if menu.exec(self.chat_list.mapToGlobal(pos)) is delete:
            chat_id = item.data(Qt.ItemDataRole.UserRole)
            self.db.delete_chat(chat_id)
            if chat_id == self.chat_id:
                self.chat_id = None
            self._refresh_list()
            self._refresh_transcript()

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
            self.chat_id = self.db.create_chat(self.book_id, " ".join(title.split())[:70])
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
            model=self.model.currentData() or None,
        ))
        self._streams[self.chat_id] = ""
        self._pending.clear()
        self.input.clear()
        self._refresh_chips()
        self._refresh_list()
        self._refresh_transcript()

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
        for chat in self.db.chats(self.book_id) if self.book_id is not None else []:
            item = QListWidgetItem(chat["title"])
            item.setData(Qt.ItemDataRole.UserRole, chat["id"])
            item.setToolTip(chat["title"])
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
            label = snippet["text"] if snippet["kind"] == "text" else "Screenshot"
            text = QLabel(f"p. {snippet['page'] + 1} · " + " ".join(label.split())[:60])
            text.setStyleSheet("font-size: 11px;")
            remove = QPushButton("×")
            remove.setFixedSize(20, 20)
            remove.clicked.connect(lambda _=False, s=snippet: self._remove_pending(s))
            chip = QWidget()
            row = QHBoxLayout(chip)
            row.setContentsMargins(4, 0, 0, 0)
            row.addWidget(text, 1)
            row.addWidget(remove)
            self.chips.addWidget(chip)

    def _remove_pending(self, snippet: dict) -> None:
        self._pending = [s for s in self._pending if s is not snippet]
        self._refresh_chips()
