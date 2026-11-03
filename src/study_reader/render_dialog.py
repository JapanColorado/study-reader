"""Rendering a book's read-aloud audio on Colab: queueing it for colab_render's watcher (which
drives a Colab GPU through the colab CLI), showing how far it has got, and copying audio the old
Drive notebook made into the app."""

from __future__ import annotations

import threading
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout

from . import colab_render as cr
from .audio_cache import AudioCache, book_key, render_speed, sync
from .db import Database, data_dir

# The shared Drive folder the old notebook rendered into; its audio is still copied in.
FOLDER_SETTING = "tts_colab_folder"
DEFAULT_FOLDER = Path.home() / "Google-Drive" / "study-reader-tts"


def colab_folder(db: Database) -> Path:
    return Path(db.get_setting(FOLDER_SETTING, str(DEFAULT_FOLDER)))


def remote_audio(db: Database, file_hash: str) -> Path:
    return colab_folder(db) / "books" / book_key(file_hash)


def local_audio(file_hash: str) -> Path:
    return data_dir() / "audio" / book_key(file_hash)


class _Task(QObject):
    """Runs a function on a thread and hands its result (or error) back on the GUI thread."""

    done = Signal(object)
    failed = Signal(str)

    def run(self, fn) -> None:
        def go():
            try:
                result, error = fn(), None
            except Exception as e:  # noqa: BLE001 - shown to the user
                result, error = None, str(e) or type(e).__name__
            try:
                self.failed.emit(error) if error is not None else self.done.emit(result)
            except RuntimeError:  # its dialog was closed (and deleted) meanwhile
                pass

        threading.Thread(target=go, daemon=True).start()


class AudioSync(QObject):
    """Copies a book's audio the Drive notebook finished into the app's data folder."""

    synced = Signal(str, int)  # book hash, parts copied (-1: the copy failed)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._tasks: list[_Task] = []

    def start(self, file_hash: str) -> None:
        task = _Task(self)
        self._tasks.append(task)
        src, dst = remote_audio(self.db, file_hash) / "audio", local_audio(file_hash)

        def finish(copied, task=task):
            self._tasks.remove(task)
            self.synced.emit(file_hash, copied)

        task.done.connect(finish)
        task.failed.connect(lambda _e, task=task: finish(-1, task))
        task.run(lambda: sync(src, dst))


def book_sentences(path: str, progress=None) -> list[str]:
    """Every sentence read aloud in the book, in order, as the reader speaks them. Uses its
    own Document, so it can run on a thread while the reader shows the book."""
    from .document import Document
    from .speech_pages import BookText

    doc = Document(path)
    try:
        book = BookText(doc)
        texts, page = [], -1
        for u in book.utterances((0, 0)):
            texts.append(u.text)
            if progress and u.first[0] != page:
                page = u.first[0]
                progress(page, doc.page_count)
        return texts
    finally:
        doc.close()


class ColabRender(QObject):
    """The app's side of colab_render: queues books, keeps the watcher going, and reports what
    it does (it runs in its own process, so it carries on after the app closes)."""

    changed = Signal()
    synced = Signal(str, int)  # book hash, parts downloaded since the last look
    CHECK_MS = 3000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.base = cr.colab_dir()
        self._mtime = None
        self.state = cr.read_state(self.base)
        self._copied = {k: b.get("copied", 0) for k, b in self.state.get("books", {}).items()}
        self._timer = QTimer(self, interval=self.CHECK_MS)
        self._timer.timeout.connect(self._check)
        self._timer.start()

    def resume(self) -> None:
        """At startup: carry on with what was queued, or release a VM left running."""
        if cr.failure(self.base) is None and (cr.pending(self.base) or self.state.get("session")):
            self._ensure_watcher()

    def _ensure_watcher(self) -> None:
        if not cr.watcher_running(self.base):
            cr.start_watcher(self.base)

    @property
    def running(self) -> bool:
        return cr.watcher_running(self.base)

    def render(self, file_hash: str, title: str, voice: str, speed: float, gpu: str, texts, drive=None) -> int:
        n = cr.queue(self.base, file_hash, title, voice, speed, gpu, texts, drive)
        self._ensure_watcher()
        self.changed.emit()
        return n

    def stop(self) -> None:
        cr.request_stop(self.base)
        self._ensure_watcher()
        self.changed.emit()

    def _check(self) -> None:
        try:
            mtime = (self.base / "state.json").stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self.state = cr.read_state(self.base)
        for key, book in self.state.get("books", {}).items():
            copied = book.get("copied", 0)
            if copied > self._copied.get(key, 0) and book.get("hash"):
                self.synced.emit(book["hash"], copied - self._copied.get(key, 0))
            self._copied[key] = copied
        self.changed.emit()


class RenderDialog(QDialog):
    """Render the open book's audio on a Colab GPU and see how far it has got."""

    progress = Signal(int, int)  # page, pages (from the export thread)

    def __init__(self, db: Database, book: dict, voice: str, speed: float, colab: ColabRender, parent=None):
        super().__init__(parent)
        self.db, self.book, self.voice, self.speed, self.colab = db, book, voice, speed, colab
        self.setWindowTitle("Render audio on Colab")
        self.setMinimumWidth(560)
        col = QVBoxLayout(self)
        intro = QLabel(
            "Renders this book's audio on a Colab GPU (rented through the <code>colab</code> CLI), "
            "so reading aloud starts at once and never waits on your computer. Each finished part is "
            "copied here as it goes, and the GPU is released when every book is done."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("GPU:"))
        self.gpu = QComboBox()
        self.gpu.addItems(cr.GPUS)
        self.gpu.setCurrentText(db.get_setting(cr.GPU_SETTING, cr.DEFAULT_GPU))
        self.gpu.setToolTip("Used when a new Colab VM starts. A T4 is plenty for Kokoro; the others are faster and cost more compute units.")
        self.gpu.currentTextChanged.connect(lambda g: db.set_setting(cr.GPU_SETTING, g))
        row.addWidget(self.gpu)
        row.addStretch(1)
        col.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.send_btn = buttons.addButton("Render on Colab", QDialogButtonBox.ButtonRole.ActionRole)
        self.send_btn.clicked.connect(self.send)
        self.stop_btn = buttons.addButton("Stop rendering", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_btn.setToolTip("Release the Colab GPU and drop every book waiting to render (audio already here stays)")
        self.stop_btn.clicked.connect(self.stop)
        col.addWidget(buttons)
        self.progress.connect(lambda p, n: self.status.setText(f"Reading the book for its sentences… page {p + 1} of {n}"))
        colab.changed.connect(self.refresh)
        self.finished.connect(lambda: colab.changed.disconnect(self.refresh))
        self._task: _Task | None = None
        self._exporting = False
        self.refresh()

    @property
    def hash(self) -> str:
        return self.book["hash"]

    def _run(self, fn, done) -> None:
        self._task = task = _Task(self)
        task.done.connect(done)
        task.failed.connect(lambda e: self._busy(False, f"Something went wrong: {e}"))
        task.run(fn)

    def _busy(self, busy: bool, text: str = "") -> None:
        self._exporting = busy
        self.send_btn.setEnabled(not busy and cr.colab_exe() is not None)
        if text:
            self.status.setTextFormat(Qt.TextFormat.PlainText)
            self.status.setText(text)

    def refresh(self) -> None:
        if self._exporting:
            return
        base, local, file_hash = self.colab.base, local_audio(self.hash), self.hash

        def status():
            request = cr.request(base, file_hash)
            if request:  # made at the speed asked for: audio at other speeds is stretched, not redone
                have = cr.rendered_keys(local, request["speed"])
                here = sum(k in have for k, _ in request["sentences"])
            else:
                here = len(AudioCache(local))
            running = cr.watcher_running(base)
            stopping = (base / "stop").exists()
            return request, here, cr.read_state(base), cr.failure(base), running, stopping

        self._run(status, self._show_status)

    def _show_status(self, result) -> None:
        request, here, state, failed, running, stopping = result
        self._busy(False)
        if cr.colab_exe() is None:
            self.stop_btn.setEnabled(False)
            self.status.setTextFormat(Qt.TextFormat.RichText)
            self.status.setText("The <code>colab</code> CLI isn't installed. Install it with "
                                "<code>uv tool install google-colab-cli</code> and sign in by running "
                                "<code>colab sessions</code> in a terminal once.")
            return
        headline, details, rendering = self.describe(request, here, state, failed, running, stopping, time.time())
        active = (running or bool(state.get("session"))) and not failed
        self.stop_btn.setEnabled(active and not stopping)
        self.send_btn.setEnabled(not rendering)
        self.send_btn.setToolTip("Already rendering this book on Colab" if rendering else "")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setText(f"<p><b>{escape(headline)}</b></p>" + "".join(
            f"<p>{escape(d).replace(chr(10), '<br>')}</p>" for d in details))
        self._fit()

    def _fit(self) -> None:
        """Grow to show the whole status (never shrink, so it doesn't jump about)."""
        layout = self.layout()
        need = layout.totalHeightForWidth(self.width()) if layout.hasHeightForWidth() else layout.sizeHint().height()
        if need > self.height():
            self.resize(self.width(), need)

    def describe(self, request, here, state, failed, running, stopping, now) -> tuple[str, list[str], bool]:
        """The status as a headline and the lines under it, and whether this book is being
        rendered (so sending it again would do nothing)."""
        active = (running or bool(state.get("session"))) and not failed
        runs = state.get("runs", {})
        books = state.get("books", {})
        others = [books.get(k, {}).get("title") or "another book"
                  for k in runs if not request or k != request["key"]]
        details: list[str] = []
        if request:
            total = len(request["sentences"])
            what = f"voice {request['voice']}, made at {request['speed']:g}×"
            changed = request["voice"] != self.voice or abs(request["speed"] - render_speed(self.speed)) > 0.01
            again = f"You now read with {self.voice} at {self.speed:g}×: Render on Colab again for that."
        if failed:
            details.append(failed)
            details.append("Render on Colab tries again, carrying on where it stopped.")
            if request:
                details.insert(0, f"{here:,} of {total:,} sentences are here.")
            return "Not rendering: Colab stopped with an error.", details, False
        if stopping and active:
            return "Stopping, and releasing the Colab GPU…", details, True
        if request and not request["done"]:
            details.append(f"{here:,} of {total:,} sentences are here ({what}).")
            just_sent = now - request.get("queued", 0) < 60
            if active or just_sent:
                run = runs.get(request["key"], {})
                if run.get("id") == request["id"]:
                    headline = "Rendering on Colab now."
                    details.append(f"Rendering the {run['total']:,} that weren't here yet.")
                elif others:
                    headline = "Waiting its turn on Colab."
                    details.append(f"Colab is rendering {others[0]} first.")
                else:
                    headline = "Starting on Colab…"
                if state.get("message") and active:
                    details.append(f"Colab: {state['message']}")
                details.append("This keeps going if you close this window or quit Study Reader, "
                               "as long as the computer stays on.")
                if changed:
                    details.append(again)
                return headline, details, not changed
            details.append("Render on Colab carries on where it stopped.")
            return "Not rendering: paused partway.", details, False
        if request:  # done
            headline = f"Rendered: all {total:,} sentences are here."
            details.append(f"Made with {what}.")
            if changed:
                details.append(again)
        else:
            headline = "Not rendered yet."
            if here:
                details.append(f"{here:,} sentences of this book are here already.")
        if active and others:
            details.append(f"Colab is rendering {others[0]} now; this book would wait its turn.")
        elif not active and state.get("phase") in ("done", "stopped") and state.get("message"):
            details.append(state["message"])
        return headline, details, False

    def send(self) -> None:
        path, title, file_hash = self.book["path"], self.book["title"], self.hash
        voice, speed, gpu = self.voice, self.speed, self.gpu.currentText()

        def export():
            return book_sentences(path, lambda p, n: self.progress.emit(p, n))

        def queue(texts):
            self._busy(False)
            self.colab.render(file_hash, title, voice, speed, gpu, texts, remote_audio(self.db, file_hash) / "audio")
            self.refresh()

        self._busy(True, "Reading the book for its sentences…")
        self._run(export, queue)

    def stop(self) -> None:
        self.colab.stop()
        self.stop_btn.setEnabled(False)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setText("<p><b>Stopping, and releasing the Colab GPU…</b></p>")
