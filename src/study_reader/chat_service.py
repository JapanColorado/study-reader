"""Decides how each question reaches Claude (fork from the book session, resume
the chat's own session, or fall back) and runs it off the GUI thread."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from . import claude_client as cc
from .document import Document

PARTIAL_CONTEXT_CHARS = 200_000


@dataclass
class BookRef:
    id: int
    path: str
    title: str
    base_session_id: str | None
    context_mode: str | None  # None | "full" | "partial"


@dataclass
class Job:
    chat_id: int
    book: BookRef
    chat_session_id: str | None
    question: str
    snippets: list[cc.Snippet] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text) before this question
    page: int = 0
    model: str | None = None
    # Branch from an earlier reply (regenerate / edit): chat_session_id is that reply's
    # session and this its message uuid; the turn forks from there.
    resume_at: str | None = None


def _extract_text(path: str) -> str:
    doc = Document(path)
    try:
        return doc.full_text()
    finally:
        doc.close()


def _extract_window(path: str, page: int) -> str:
    doc = Document(path)
    try:
        return doc.text_window(min(page, doc.page_count - 1), PARTIAL_CONTEXT_CHARS)
    finally:
        doc.close()


def _too_long(error: Exception) -> bool:
    msg = str(error).lower()
    return any(s in msg for s in ("too long", "too large", "context window", "context length"))


def _replay(history: list[tuple[str, str]]) -> str:
    lines = [f"{'Me' if role == 'user' else 'You'}: {text}" for role, text in history]
    return "Earlier in this conversation:\n<transcript>\n" + "\n\n".join(lines) + "\n</transcript>"


class ChatService:
    """Events are reported through emit(name, *args):
    status(book_id, text), base(book_id, session_id, mode), delta(chat_id, text),
    done(chat_id, session_id, text, message_uuid), failed(chat_id, message)."""

    def __init__(self, emit: Callable[..., None], sessions_dir: str | Path):
        self.emit = emit
        self.sessions_dir = Path(sessions_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        self._base: dict[int, tuple[str | None, str]] = {}

    def cwd(self, book_id: int) -> Path:
        return self.sessions_dir / f"book-{book_id}"

    async def ensure_base(self, job: Job) -> tuple[str | None, str]:
        """Return (base session id, mode), priming the book session if needed."""
        book = job.book
        cwd = self.cwd(book.id)
        async with self._locks.setdefault(book.id, asyncio.Lock()):
            sid, mode = self._base.get(book.id, (book.base_session_id, book.context_mode))
            if mode == "partial":
                return None, "partial"
            if cc.session_exists(sid, cwd):
                return sid, "full"
            try:
                self.emit("status", book.id, "Reading the book…")
                text = await asyncio.to_thread(_extract_text, book.path)
                sid, mode = None, "partial"
                if cc.fits_in_context(text):
                    self.emit("status", book.id, "Sharing the book with Claude (one-time, may take a minute)…")
                    try:
                        res = await cc.run_turn(cc.priming_blocks(book.title, text), cwd=cwd, model=job.model)
                        sid, mode = res.session_id, "full"
                    except cc.ClaudeError as e:
                        if not _too_long(e):
                            raise
            finally:
                self.emit("status", book.id, "")
            self._base[book.id] = (sid, mode)
            self.emit("base", book.id, sid or "", mode)
            return sid, mode

    async def ask(self, job: Job) -> None:
        streamed = False

        def on_delta(text: str) -> None:
            nonlocal streamed
            streamed = True
            self.emit("delta", job.chat_id, text)

        try:
            try:
                res = await self._turn(job, job.chat_session_id, on_delta)
            except cc.ClaudeError:
                if not job.resume_at or streamed:
                    raise
                # The branch point couldn't be loaded: start over from the book and replay.
                res = await self._turn(job, None, on_delta)
            self.emit("done", job.chat_id, res.session_id, res.text, res.message_uuid or "")
        except Exception as e:
            self.emit("failed", job.chat_id, str(e) or type(e).__name__)

    async def _turn(self, job: Job, chat_session_id: str | None, on_delta) -> cc.TurnResult:
        cwd = self.cwd(job.book.id)
        extra: list[str] = []
        resume_at = None
        if cc.session_exists(chat_session_id, cwd):
            resume, resume_at = chat_session_id, job.resume_at
            fork = bool(resume_at)
        else:
            base, mode = await self.ensure_base(job)
            resume, fork = base, bool(base)
            if mode == "partial":
                window = await asyncio.to_thread(_extract_window, job.book.path, job.page)
                extra.append(
                    f"I am reading \"{job.book.title}\". It is too long to share in full; "
                    f"here are the pages around where I am:\n<book_excerpt>\n{window}\n</book_excerpt>"
                )
            if job.history:
                extra.append(_replay(job.history))
        blocks = cc.build_blocks(job.question, job.snippets, "\n\n".join(extra))
        return await cc.run_turn(
            blocks, cwd=cwd, model=job.model, resume=resume, fork=fork, resume_at=resume_at, on_delta=on_delta,
        )


class ChatWorker(QObject):
    """Runs ChatService on a background asyncio loop; results arrive as Qt signals."""

    status = Signal(int, str)
    base = Signal(int, str, str)
    delta = Signal(int, str)
    done = Signal(int, str, str, str)
    failed = Signal(int, str)

    def __init__(self, sessions_dir: str | Path, parent: QObject | None = None):
        super().__init__(parent)
        self.service = ChatService(self._emit, sessions_dir)
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="claude-worker")
        self._thread.start()

    def _emit(self, name: str, *args) -> None:
        getattr(self, name).emit(*args)

    def submit(self, job: Job) -> None:
        asyncio.run_coroutine_threadsafe(self.service.ask(job), self.loop)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
