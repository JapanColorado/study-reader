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
CONVERSIONS_AT_ONCE = 3  # Claude Conversion requests running at the same time


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
    book: BookRef | None  # None: not about a book (a review card from none), asked in a fresh session
    chat_session_id: str | None
    question: str
    snippets: list[cc.Snippet] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text) before this question
    page: int = 0
    model: str | None = None
    # Branch from an earlier reply (regenerate / edit): chat_session_id is that reply's
    # session and this its message uuid; the turn forks from there.
    resume_at: str | None = None
    # Sent with the question but not part of it (e.g. the user's quiz answers).
    context: str = ""
    # A side question (grading a quiz answer): always fork, so the chat's session doesn't continue it.
    detached: bool = False


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
    done(chat_id, session_id, text, message_uuid), failed(chat_id, message),
    for quiz grading graded(answer_id, text), gradeFailed(answer_id, message), and for grading
    a card in review cardGraded(card_id, text), cardGradeFailed(card_id, message),
    titled(chat_id, title) when a chat's auto-title is ready, and for Claude Conversion
    converted(batch_id, text), convertFailed(batch_id, message)."""

    def __init__(self, emit: Callable[..., None], sessions_dir: str | Path):
        self.emit = emit
        self.sessions_dir = Path(sessions_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        self._base: dict[int, tuple[str | None, str]] = {}
        self._conversions: asyncio.Semaphore | None = None  # made on the worker's loop

    def forget_base(self, book_id: int) -> None:
        """Drop the cached base session, e.g. after the book's file was replaced."""
        self._base.pop(book_id, None)

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

    async def grade(self, job: Job, answer_id: int, card: bool = False) -> None:
        """Ask Claude to grade a quiz answer, as a detached turn from the quiz's reply.
        card: answer_id is a review card's id, and the events are cardGraded / cardGradeFailed."""
        graded, failed = ("cardGraded", "cardGradeFailed") if card else ("graded", "gradeFailed")
        try:
            try:
                res = await self._turn(job, job.chat_session_id, None)
            except cc.ClaudeError:
                if not job.chat_session_id:
                    raise
                res = await self._turn(job, None, None)
            self.emit(graded, answer_id, res.text)
        except Exception as e:
            self.emit(failed, answer_id, str(e) or type(e).__name__)

    async def title(self, chat_id: int, question: str, passage: str, reply: str) -> None:
        """Name a chat from its first exchange. Failures are ignored: the chat keeps its title."""
        try:
            title = await cc.chat_title(question, passage, reply, cwd=self.sessions_dir / "titles")
        except Exception:
            return
        if title:
            self.emit("titled", chat_id, title)

    async def convert(self, batch_id: int, blocks: list[dict], model: str, system_prompt: str) -> None:
        """Ask Claude to rewrite a batch of review cards (convert.py), in a fresh bookless session."""
        if self._conversions is None:
            self._conversions = asyncio.Semaphore(CONVERSIONS_AT_ONCE)
        try:
            async with self._conversions:
                res = await cc.run_turn(blocks, cwd=self.sessions_dir / "convert", model=model,
                                        system_prompt=system_prompt)
            self.emit("converted", batch_id, res.text)
        except Exception as e:
            self.emit("convertFailed", batch_id, str(e) or type(e).__name__)

    async def _turn(self, job: Job, chat_session_id: str | None, on_delta) -> cc.TurnResult:
        if job.book is None:
            blocks = cc.build_blocks(job.question, job.snippets, job.context)
            return await cc.run_turn(blocks, cwd=self.sessions_dir / "cards", model=job.model, on_delta=on_delta)
        cwd = self.cwd(job.book.id)
        extra: list[str] = []
        resume_at = None
        if cc.session_exists(chat_session_id, cwd):
            resume, resume_at = chat_session_id, job.resume_at
            fork = bool(resume_at) or job.detached
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
        if job.context:
            extra.append(job.context)
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
    graded = Signal(int, str)
    gradeFailed = Signal(int, str)
    cardGraded = Signal(int, str)
    cardGradeFailed = Signal(int, str)
    titled = Signal(int, str)
    converted = Signal(int, str)
    convertFailed = Signal(int, str)

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

    def forget_base(self, book_id: int) -> None:
        self.loop.call_soon_threadsafe(self.service.forget_base, book_id)

    def submit_grade(self, job: Job, answer_id: int) -> None:
        asyncio.run_coroutine_threadsafe(self.service.grade(job, answer_id), self.loop)

    def submit_card_grade(self, job: Job, card_id: int) -> None:
        asyncio.run_coroutine_threadsafe(self.service.grade(job, card_id, card=True), self.loop)

    def submit_title(self, chat_id: int, question: str, passage: str, reply: str) -> None:
        asyncio.run_coroutine_threadsafe(self.service.title(chat_id, question, passage, reply), self.loop)

    def submit_conversion(self, batch_id: int, blocks: list[dict], model: str, system_prompt: str) -> None:
        asyncio.run_coroutine_threadsafe(self.service.convert(batch_id, blocks, model, system_prompt), self.loop)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
