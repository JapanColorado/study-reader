"""Rendering a book's read-aloud audio on Colab: sending its sentences to the shared Drive
folder, showing how far the notebook has got, and copying finished audio into the app."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .audio_cache import NOTEBOOK, AudioCache, book_key, read_request, render_speed, sync, write_request
from .db import Database, data_dir

FOLDER_SETTING = "tts_colab_folder"
DEFAULT_FOLDER = Path.home() / "Google-Drive" / "study-reader-tts"
NOTEBOOK_ASSET = Path(__file__).parent / "assets" / "colab_render.ipynb"
COLAB_URL = "https://colab.research.google.com/"


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
    """Copies a book's finished audio from the shared folder into the app's data folder."""

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


class RenderDialog(QDialog):
    """Send the open book to Colab, see how much is rendered, and copy it here."""

    progress = Signal(int, int)  # page, pages (from the export thread)

    def __init__(self, db: Database, book: dict, voice: str, speed: float, audio_sync: AudioSync, parent=None):
        super().__init__(parent)
        self.db, self.book, self.voice, self.speed, self.audio_sync = db, book, voice, speed, audio_sync
        self.setWindowTitle("Render audio on Colab")
        self.setMinimumWidth(560)
        col = QVBoxLayout(self)
        intro = QLabel(
            "Colab's GPUs make the whole book's audio in a fraction of the time your computer takes, "
            "so reading aloud starts at once and never pauses.<ol>"
            "<li><b>Send to Colab</b> writes the book's sentences to the folder below, which Google Drive shares with Colab.</li>"
            f"<li>Open Colab, then <i>File → Open notebook → Google Drive</i> and open <b>{NOTEBOOK}</b> in that folder. "
            "Choose a GPU runtime and <i>Run all</i>. It renders every book waiting there and can be rerun to finish.</li>"
            "<li>Finished audio is copied here when you open the book, or with <b>Copy rendered audio</b>.</li></ol>"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(intro)
        row = QHBoxLayout()
        self.folder = QLineEdit(str(colab_folder(db)), readOnly=True)
        row.addWidget(self.folder, 1)
        change = QPushButton("Change…")
        change.clicked.connect(self._choose_folder)
        row.addWidget(change)
        col.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.send_btn = buttons.addButton("Send to Colab", QDialogButtonBox.ButtonRole.ActionRole)
        self.send_btn.clicked.connect(self.send)
        self.copy_btn = buttons.addButton("Copy rendered audio", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_btn.clicked.connect(self.copy)
        colab = buttons.addButton("Open Colab", QDialogButtonBox.ButtonRole.ActionRole)
        colab.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(COLAB_URL)))
        col.addWidget(buttons)
        self.progress.connect(lambda p, n: self.status.setText(f"Reading the book for its sentences… page {p + 1} of {n}"))
        audio_sync.synced.connect(self._on_synced)
        self.finished.connect(lambda: audio_sync.synced.disconnect(self._on_synced))
        self._task: _Task | None = None
        self.refresh()

    @property
    def hash(self) -> str:
        return self.book["hash"]

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder shared with Colab", self.folder.text())
        if folder:
            self.db.set_setting(FOLDER_SETTING, folder)
            self.folder.setText(folder)
            self.refresh()

    def _run(self, fn, done) -> None:
        self._task = task = _Task(self)
        task.done.connect(done)
        task.failed.connect(lambda e: self._busy(False, f"Something went wrong: {e}"))
        task.run(fn)

    def _busy(self, busy: bool, text: str = "") -> None:
        self.send_btn.setEnabled(not busy)
        self.copy_btn.setEnabled(not busy)
        if text:
            self.status.setText(text)

    def refresh(self) -> None:
        remote, local = remote_audio(self.db, self.hash), local_audio(self.hash)

        def status():
            request = read_request(remote)
            try:
                rendered = json.loads((remote / "status.json").read_text())
            except (OSError, ValueError):
                rendered = None
            here = AudioCache(local).count(k for k, _ in request["sentences"]) if request else len(AudioCache(local))
            return request, rendered, here

        self._busy(True, "Checking the shared folder…")
        self._run(status, self._show_status)

    def _show_status(self, result) -> None:
        request, rendered, here = result
        self._busy(False)
        if not request:
            self.status.setText(
                f"Not sent yet. {here:,} sentences of this book are here already." if here else "Not sent yet.")
            return
        total = len(request["sentences"])
        lines = [f"Sent {total:,} sentences (voice {request['voice']}, made at {request['speed']:g}×)."]
        if rendered:
            lines.append(f"Rendered on Colab: {rendered.get('rendered', 0):,} of {rendered.get('total', total):,}.")
        else:
            lines.append("Not rendered yet: run the notebook.")
        lines.append(f"Copied here: {here:,} of {total:,}.")
        if request["voice"] != self.voice or abs(request["speed"] - render_speed(self.speed)) > 0.01:
            lines.append(f"You now read with {self.voice} at {self.speed:g}×: send again to render that.")
        self.status.setText(" ".join(lines))

    def send(self) -> None:
        folder, path, title = colab_folder(self.db), self.book["path"], self.book["title"]
        remote = remote_audio(self.db, self.hash)
        if not folder.parent.exists():
            self.status.setText(f"{folder.parent} doesn't exist. Is Google Drive mounted? Or pick another folder.")
            return

        def export():
            texts = book_sentences(path, lambda p, n: self.progress.emit(p, n))
            count = write_request(remote, title, self.voice, self.speed, texts)
            shutil.copyfile(NOTEBOOK_ASSET, folder / NOTEBOOK)
            return count

        self._busy(True, "Reading the book for its sentences…")
        self._run(export, lambda n: (self._busy(False), self.refresh()))

    def copy(self) -> None:
        self._busy(True, "Copying rendered audio…")
        self.audio_sync.start(self.hash)

    def _on_synced(self, file_hash: str, copied: int) -> None:
        if file_hash == self.hash:
            self.refresh()

