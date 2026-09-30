"""Start screen listing previously opened books."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QVBoxLayout, QWidget,
)

from .db import Database


class LibraryView(QWidget):
    openRequested = Signal(str)  # file path
    browseRequested = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        title = QLabel("<h2>Library</h2>")
        browse = QPushButton("Open PDF or EPUB…")
        browse.clicked.connect(self.browseRequested)
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch()
        top.addWidget(browse)

        self.list = QListWidget()
        self.list.setSpacing(4)
        self.list.itemActivated.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.empty = QLabel("No books yet. Open a PDF or EPUB to get started.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty, 1)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        books = self.db.recent_books()
        for b in books:
            exists = os.path.exists(b["path"])
            chats = len(self.db.chats(b["id"]))
            when = time.strftime("%d %b %Y", time.localtime(b["opened_at"]))
            detail = f"p. {b['last_page'] + 1} of {b['page_count']} · {chats} chat{'s' * (chats != 1)} · {when}"
            item = QListWidgetItem(f"{b['title']}\n{detail if exists else 'File not found: ' + b['path']}")
            item.setData(Qt.ItemDataRole.UserRole, b["id"])
            item.setToolTip(b["path"])
            if not exists:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(item)
        self.list.setVisible(bool(books))
        self.empty.setVisible(not books)

    def _activate(self, item: QListWidgetItem) -> None:
        book = self.db.book(item.data(Qt.ItemDataRole.UserRole))
        if book:
            self.openRequested.emit(book["path"])

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove from library (deletes its chats)")
        if menu.exec(self.list.mapToGlobal(pos)) is remove:
            self.db.delete_book(item.data(Qt.ItemDataRole.UserRole))
            self.refresh()
