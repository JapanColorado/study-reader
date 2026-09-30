"""Main window: table of contents | reader or library | chats."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QLabel, QMainWindow, QMessageBox, QSpinBox, QSplitter, QStackedWidget,
    QToolBar, QTreeWidget, QTreeWidgetItem,
)

from .chat_panel import ChatPanel
from .chat_service import ChatWorker
from .db import Database, data_dir
from .document import Document, file_hash
from .library_view import LibraryView
from .reader_view import ReaderView


class MainWindow(QMainWindow):
    def __init__(self, db: Database | None = None):
        super().__init__()
        self.setWindowTitle("Study Reader")
        self.resize(1500, 950)
        self.db = db or Database()
        self.worker = ChatWorker(data_dir() / "sessions", self)
        self.doc: Document | None = None
        self.book_id: int | None = None

        self.toc = QTreeWidget()
        self.toc.setHeaderHidden(True)
        self.toc.itemClicked.connect(lambda item: self.reader.go_to(item.data(0, Qt.ItemDataRole.UserRole)))
        self.reader = ReaderView()
        self.library = LibraryView(self.db)
        self.center = QStackedWidget()
        self.center.addWidget(self.library)
        self.center.addWidget(self.reader)
        self.chat = ChatPanel(self.db, self.worker)
        self.chat.current_page = self.reader.current_page

        split = QSplitter()
        for w in (self.toc, self.center, self.chat):
            split.addWidget(w)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 820, 460])
        self.setCentralWidget(split)

        self._build_toolbar()
        self.library.openRequested.connect(self.open_book)
        self.library.browseRequested.connect(self.browse)
        self.reader.snippetChosen.connect(self.chat.add_snippet)
        self.reader.pageChanged.connect(self._on_page_changed)
        self.chat.jumpRequested.connect(self.reader.flash)

        self._save_timer = QTimer(self, interval=10_000)
        self._save_timer.timeout.connect(self._save_position)
        self._save_timer.start()
        self.show_library()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        def action(text: str, shortcut, slot, checkable: bool = False) -> QAction:
            a = QAction(text, self, checkable=checkable)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setToolTip(f"{text} ({QKeySequence(shortcut).toString()})")
            a.triggered.connect(slot)
            self.addAction(a)
            return a

        bar.addAction(action("Library", "Ctrl+L", self.show_library))
        bar.addAction(action("Open…", QKeySequence.StandardKey.Open, self.browse))
        bar.addSeparator()
        self.book_actions = [
            action("Zoom −", "Ctrl+-", self.reader.zoom_out),
            action("Zoom +", "Ctrl+=", self.reader.zoom_in),
            action("Fit width", "Ctrl+0", self.reader.fit_width),
        ]
        bar.addActions(self.book_actions)
        self.page_spin = QSpinBox()
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.reader.go_to(v - 1))
        self.page_total = QLabel()
        bar.addWidget(self.page_spin)
        bar.addWidget(self.page_total)
        bar.addSeparator()
        self.region_action = action("Screenshot region", "Ctrl+Shift+S", self.reader.set_region_mode, checkable=True)
        bar.addAction(self.region_action)
        new_chat = action("New chat", "Ctrl+N", self.chat.new_chat)
        bar.addAction(new_chat)
        # Act on the current selection without reaching for the popup.
        action("Ask in new chat", "Ctrl+Return", lambda: self.reader.choose(True))
        action("Add to current chat", "Ctrl+Shift+Return", lambda: self.reader.choose(False))
        self.book_actions += [self.region_action, new_chat]

    # -- library / opening --------------------------------------------------------
    def show_library(self) -> None:
        """Close the open book (replies still arriving are saved) and show the library."""
        self._save_position()
        if self.region_action.isChecked():
            self.region_action.trigger()
        self.reader.set_document(None)
        if self.doc:
            self.doc.close()
        self.doc, self.book_id = None, None
        self.toc.clear()
        self.page_total.setText("")
        self.setWindowTitle("Study Reader")
        self.chat.set_book(None)
        self.library.refresh()
        self.center.setCurrentWidget(self.library)
        self._set_book_ui(False)

    def _set_book_ui(self, reading: bool) -> None:
        for a in self.book_actions:
            a.setEnabled(reading)
        self.page_spin.setEnabled(reading)
        self.toc.setVisible(reading and self.toc.topLevelItemCount() > 0)

    def browse(self) -> None:
        start = self.db.get_setting("last_dir")
        path, _ = QFileDialog.getOpenFileName(self, "Open book", start, "Books (*.pdf *.epub)")
        if path:
            self.open_book(path)

    def open_book(self, path: str) -> None:
        self._save_position()
        try:
            doc = Document(path)
            if doc.page_count == 0:
                raise ValueError("the document has no pages")
        except Exception as e:
            QMessageBox.warning(self, "Could not open book", f"{path}\n\n{e}")
            return
        if self.doc:
            self.doc.close()
        self.doc = doc
        book = self.db.upsert_book(file_hash(path), path, doc.title, doc.page_count)
        self.book_id = book["id"]
        self.db.set_setting("last_dir", str(path).rsplit("/", 1)[0])
        self.setWindowTitle(f"{book['title']} — Study Reader")

        self._fill_toc()
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, doc.page_count)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" / {doc.page_count} ")
        self.center.setCurrentWidget(self.reader)
        self._set_book_ui(True)
        self.reader.set_document(doc, book["last_page"], book["zoom"])
        self._on_page_changed(self.reader.current_page())
        self.chat.set_book(self.book_id)

    def _fill_toc(self) -> None:
        self.toc.clear()
        parents: dict[int, QTreeWidgetItem] = {}
        for level, title, page in self.doc.toc():
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, page)
            parent = parents.get(level - 1)
            parent.addChild(item) if parent else self.toc.addTopLevelItem(item)
            parents[level] = item
            for deeper in [k for k in parents if k > level]:
                del parents[deeper]

    # -- position -----------------------------------------------------------------
    def _on_page_changed(self, page: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)

    def _save_position(self) -> None:
        if self.book_id is not None and self.center.currentWidget() is self.reader:
            self.db.update_book(self.book_id, last_page=self.reader.current_page(), zoom=self.reader.saved_zoom())

    def closeEvent(self, event) -> None:
        self._save_position()
        self.worker.shutdown()
        super().closeEvent(event)
