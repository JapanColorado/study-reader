"""Notes on passages of a book: Qt-free search and Markdown export, and the NotesPanel that
lists them (rendered in notes.html) in the right-hand slot chats and the calculator use."""

from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QToolButton, QVBoxLayout, QWidget

from .db import Database
from .theme import icon, theme

ASSETS = Path(__file__).parent / "assets"
EMPTY = "Select text in the book and choose Note (Ctrl+Shift+N) to annotate it."
NO_MATCH = "No notes match your search."


def matches(note: dict, query: str) -> bool:
    """Every word of the query appears in the note or its passage, ignoring case."""
    haystack = f"{note['text']}\n{note['body']}".lower()
    return all(word in haystack for word in query.lower().split())


def to_markdown(title: str, notes: list[dict]) -> str:
    """A book's notes, in reading order, as a Markdown document: each passage quoted, then its note."""
    lines = [f"# Notes on {title}", ""]
    page = None
    for n in notes:
        if n["page"] != page:
            page = n["page"]
            lines += [f"## Page {page + 1}", ""]
        lines += [f"> {' '.join(n['text'].split())}", ""]
        if body := n["body"].strip():
            lines += [body, ""]
    return "\n".join(lines).rstrip() + "\n"


def export_name(title: str) -> str:
    """A file name for a book's exported notes."""
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title)
    return f"{' '.join(stem.split())[:80] or 'Book'} notes.md"


class LocalPage(QWebEnginePage):
    """Links open in the web browser; nothing navigates the page away from local files."""

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return False
        return url.isLocalFile()


class _Bridge(QObject):
    """What notes.html calls through the web channel."""

    def __init__(self, panel: NotesPanel):
        super().__init__(panel)
        self.panel = panel

    @Slot(int, str)
    def save(self, note_id: int, body: str) -> None:
        self.panel.save(note_id, body)

    @Slot(int)
    def cancel(self, note_id: int) -> None:
        self.panel.cancel(note_id)

    @Slot(int)
    def remove(self, note_id: int) -> None:
        self.panel.remove(note_id)

    @Slot(int)
    def jump(self, note_id: int) -> None:
        self.panel.jump(note_id)


class NotesPanel(QWidget):
    """Header, search and export in Qt; the note cards and their editor in notes.html."""

    jumpRequested = Signal(int, list)  # page, rects
    notesChanged = Signal()  # a note was added, edited or deleted

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.book_id: int | None = None
        self.shown: list[dict] = []  # the notes on display, after the search filter
        self._fresh: int | None = None  # a note just created, dropped if its first edit is cancelled empty

        self.title = QLabel("Notes")
        self.title.setProperty("role", "section")
        self.export_btn = QToolButton()
        self.export_btn.setIcon(icon("download"))
        self.export_btn.setIconSize(QSize(17, 17))
        self.export_btn.setToolTip("Export this book's notes to a Markdown file")
        self.export_btn.clicked.connect(self.export)
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.addWidget(self.title)
        header.addStretch()
        header.addWidget(self.export_btn)

        self.search = QLineEdit(objectName="notesSearch", placeholderText="Search notes…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)

        self.view = QWebEngineView()
        self.view.setPage(LocalPage(self.view))
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("notes", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "notes.html")))

        self.setObjectName("chatPanel")  # same surface as the chat panel it swaps with
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.search)
        layout.addWidget(self.view, 1)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        self.set_book(None)

    # -- page ----------------------------------------------------------------------------
    def _js(self, function: str, *args) -> None:
        js = f"{function}({', '.join(json.dumps(a) for a in args)})"
        if self._ready:
            self.view.page().runJavaScript(js)
        else:
            self._pending.append(js)

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        for js in self._pending:
            self.view.page().runJavaScript(js)
        self._pending.clear()

    def _apply_theme(self) -> None:
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self._js("setTheme", theme().colors)

    # -- book / list ---------------------------------------------------------------------
    def set_book(self, book_id: int | None) -> None:
        self.book_id = book_id
        self._fresh = None
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.setEnabled(book_id is not None)
        self.refresh()

    def refresh(self) -> None:
        notes = self.db.notes(self.book_id) if self.book_id is not None else []
        query = self.search.text()
        self.shown = [n for n in notes if matches(n, query)]
        self.title.setText(f"Notes · {len(notes)}" if notes else "Notes")
        self.export_btn.setEnabled(bool(notes))
        self._js("render", {
            "notes": [{"id": n["id"], "page": n["page"], "text": n["text"], "body": n["body"]} for n in self.shown],
            "empty": NO_MATCH if notes else EMPTY,
        })

    def _show(self, note_id: int) -> None:
        """Make sure the search doesn't hide a note."""
        if all(n["id"] != note_id for n in self.shown):
            self.search.clear()  # refreshes

    # -- notes ---------------------------------------------------------------------------
    def add_note(self, snippet: dict) -> int | None:
        """Annotate a text snippet from the reader, opening the new note's editor."""
        if self.book_id is None:
            return None
        note_id = self.db.add_note(self.book_id, snippet["page"], snippet["text"], snippet["rects"])
        self._fresh = note_id
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh()
        self.edit_note(note_id)
        self.notesChanged.emit()
        return note_id

    def edit_note(self, note_id: int) -> None:
        self._show(note_id)
        self.view.setFocus()
        self._js("edit", note_id)
        self._js("reveal", note_id)

    def reveal(self, note_id: int) -> None:
        self._show(note_id)
        self._js("reveal", note_id)

    def save(self, note_id: int, body: str) -> None:
        if self.db.note(note_id) is None:
            return
        self.db.update_note(note_id, body)
        if note_id == self._fresh:
            self._fresh = None
        self.refresh()
        self.notesChanged.emit()

    def cancel(self, note_id: int) -> None:
        note = self.db.note(note_id)
        if note_id == self._fresh:
            self._fresh = None
            if note is not None and not note["body"].strip():
                self.remove(note_id)

    def remove(self, note_id: int) -> None:
        if note_id == self._fresh:
            self._fresh = None
        self.db.delete_note(note_id)
        self.refresh()
        self.notesChanged.emit()

    def jump(self, note_id: int) -> None:
        if note := self.db.note(note_id):
            self.jumpRequested.emit(note["page"], note["rects"])

    def export(self) -> None:
        if self.book_id is None:
            return
        book = self.db.book(self.book_id)
        start = Path(self.db.get_setting("export_dir") or Path.home()) / export_name(book["title"])
        path, _ = QFileDialog.getSaveFileName(self, "Export notes", str(start), "Markdown (*.md)")
        if not path:
            return
        Path(path).write_text(to_markdown(book["title"], self.db.notes(self.book_id)), encoding="utf-8")
        self.db.set_setting("export_dir", str(Path(path).parent))
