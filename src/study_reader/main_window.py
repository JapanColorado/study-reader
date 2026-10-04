"""Main window: table of contents | reader or library | chats or calculator."""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QToolBar, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import quiz
from .calculator import CalculatorPanel
from .chat_panel import MODELS, ChatPanel
from .chat_service import ChatWorker
from .db import Database, data_dir
from .document import Document, file_hash
from .library_view import LibraryView
from .reader_view import ReaderView
from .theme import MODES, icon, theme
from .web_import import WebImportError, normalize_url
from .web_render import WebImporter


class MainWindow(QMainWindow):
    def __init__(self, db: Database | None = None):
        super().__init__()
        self.setWindowTitle("Study Reader")
        self.resize(1500, 950)
        self.db = db or Database()
        theme().configure(self.db.get_setting("theme", "system"), self.db.get_setting("dark_pages") == "1")
        self.worker = ChatWorker(data_dir() / "sessions", self)
        self.doc: Document | None = None
        self.book_id: int | None = None

        self.toc = QTreeWidget()
        self.toc.setHeaderHidden(True)
        self.toc.setIndentation(14)
        self.toc.itemClicked.connect(lambda item: self.reader.go_to(item.data(0, Qt.ItemDataRole.UserRole)))
        self.sidebar = QWidget(objectName="sidebar")
        self.sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(8, 10, 4, 8)
        side.setSpacing(6)
        side.addWidget(_section("Contents"))
        side.addWidget(self.toc, 1)

        self.reader = ReaderView()
        self.library = LibraryView(self.db)
        self.center = QStackedWidget()
        self.center.addWidget(self.library)
        self.center.addWidget(self.reader)
        self.chat = ChatPanel(self.db, self.worker)
        self.chat.current_page = self.reader.current_page
        self.calc = CalculatorPanel(self.db)
        self.right = QStackedWidget()  # chats and the calculator take turns in the same slot
        self.right.addWidget(self.chat)
        self.right.addWidget(self.calc)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        for w in (self.sidebar, self.center, self.right):
            split.addWidget(w)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 800, 460])
        self.setCentralWidget(split)

        self._build_toolbar()
        self.library.openRequested.connect(self.open_book)
        self.library.browseRequested.connect(self.browse)
        self.library.webRequested.connect(self.ask_url)
        self.library.refetchRequested.connect(lambda url: self.open_url(url, refetch=True))
        self.web = WebImporter(data_dir() / "web", self)
        self.web.status.connect(lambda text: self._web_progress and self._web_progress.setLabelText(text))
        self.web.finished.connect(self._on_web_done)
        self.web.failed.connect(self._on_web_failed)
        self._web_progress: QProgressDialog | None = None
        self.reader.snippetChosen.connect(lambda sn, new: self._show_chat(lambda: self.chat.add_snippet(sn, new)))
        self.reader.quizChosen.connect(self._quiz_on)
        self.reader.highlightClicked.connect(self._highlight_menu)
        self.chat.highlightsChanged.connect(self._refresh_highlights)
        self.reader.pageChanged.connect(self._on_page_changed)
        self.reader.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{round(z * 100)}%"))
        self.chat.jumpRequested.connect(self.reader.flash)
        self.chat.pageRequested.connect(self.reader.go_to)
        theme().changed.connect(self._on_theme_changed)
        self._on_theme_changed()

        self._save_timer = QTimer(self, interval=10_000)
        self._save_timer.timeout.connect(self._save_position)
        self._save_timer.start()
        self.show_library()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(bar)

        def action(text: str, shortcut, slot, icon_name: str = "", checkable: bool = False) -> QAction:
            a = QAction(text, self, checkable=checkable)
            if icon_name:
                a.setIcon(icon(icon_name))
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setToolTip(f"{text} ({QKeySequence(shortcut).toString()})")
            a.triggered.connect(slot)
            self.addAction(a)
            return a

        def labelled(a: QAction) -> None:
            bar.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.toc_action = action("Contents", "F9", self._toggle_toc, "panel-left", checkable=True)
        self.toc_action.setChecked(True)
        bar.addAction(self.toc_action)
        library = action("Library", "Ctrl+L", self.show_library, "library")
        bar.addAction(library)
        labelled(library)
        bar.addAction(action("Open…", QKeySequence.StandardKey.Open, self.browse, "folder-open"))
        bar.addAction(action("Open web page…", "Ctrl+Shift+O", self.ask_url, "globe"))
        self.finished_action = action("Finished", "", self._set_finished, "check-circle", checkable=True)
        self.finished_action.setToolTip("Mark this book as finished (moves it to the library's Finished section)")
        bar.addAction(self.finished_action)
        # Toolbar entries that only make sense with a book open; hidden in the library.
        book_items = [bar.addSeparator()]

        zoom_out = action("Zoom out", "Ctrl+-", self.reader.zoom_out, "zoom-out")
        zoom_in = action("Zoom in", "Ctrl+=", self.reader.zoom_in, "zoom-in")
        fit = action("Fit width", "Ctrl+0", self.reader.fit_width, "fit-width")
        self.crop_action = action("Crop margins", "Ctrl+Shift+0", self._set_crop, "crop", checkable=True)
        self.zoom_label = _muted()
        self.zoom_label.setMinimumWidth(42)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addAction(zoom_out)
        book_items += [bar.addWidget(self.zoom_label)]
        bar.addAction(zoom_in)
        bar.addAction(fit)
        bar.addAction(self.crop_action)
        book_items += [bar.addSeparator()]

        self.page_spin = QSpinBox()
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setMinimumWidth(52)
        self.page_spin.setToolTip("Go to page")
        self.page_spin.valueChanged.connect(lambda v: self.reader.go_to(v - 1))
        self.page_total = _muted()
        book_items += [bar.addWidget(self.page_spin), bar.addWidget(self.page_total), bar.addSeparator()]

        self.region_action = action(
            "Screenshot region", "Ctrl+Shift+S", self.reader.set_region_mode, "scan", checkable=True
        )
        bar.addAction(self.region_action)
        labelled(self.region_action)
        self.quiz_action = action("Quiz me", "Ctrl+Shift+Q", self.quiz, "quiz")
        self.quiz_action.setToolTip(
            "Quiz me on the selection, or else the section being read (Ctrl+Shift+Q)"
        )
        bar.addAction(self.quiz_action)
        labelled(self.quiz_action)
        new_chat = action("New chat", "Ctrl+N", self.chat.new_chat, "message-plus")
        # Act on the current selection without reaching for the popup.
        action("Ask in new chat", "Ctrl+Return", lambda: self.reader.choose(True))
        action("Add to current chat", "Ctrl+Shift+Return", lambda: self.reader.choose(False))
        self.book_actions = [
            zoom_out, zoom_in, fit, self.crop_action, self.region_action, self.quiz_action, self.finished_action, new_chat,
        ]
        self.book_items = book_items + [
            zoom_out, zoom_in, fit, self.crop_action, self.region_action, self.quiz_action, self.finished_action,
        ]

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        menu = QMenu(self)
        self.theme_menu = menu.addMenu("Theme")
        group = QActionGroup(self)
        for mode, label in MODES:
            a = self.theme_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(theme().mode == mode)
            a.triggered.connect(lambda _=False, m=mode: self._set_theme(mode=m))
            group.addAction(a)
        self.dark_pages_action = menu.addAction("Dark pages")
        self.dark_pages_action.setCheckable(True)
        self.dark_pages_action.setChecked(theme().dark_pages)
        self.dark_pages_action.setToolTip("Recolor book pages to match dark mode")
        self.dark_pages_action.toggled.connect(lambda on: self._set_theme(dark_pages=on))
        menu.addSeparator()
        models = menu.addMenu(icon("message"), "Default model for new chats")
        group = QActionGroup(self)
        for label, value in MODELS:
            a = models.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.chat.default_model() == value)
            a.triggered.connect(lambda _=False, v=value: self.chat.set_default_model(v))
            group.addAction(a)
        settings = QToolButton()
        settings.setIcon(icon("settings"))
        settings.setToolTip("Settings")
        settings.setMenu(menu)
        settings.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bar.addWidget(settings)

        self.calc_action = action("Calculator", "F11", self._toggle_calc, "calculator", checkable=True)
        bar.addAction(self.calc_action)
        self.chat_action = action("Chats", "F10", self._toggle_chat, "message", checkable=True)
        self.chat_action.setChecked(True)
        bar.addAction(self.chat_action)

    # -- appearance ---------------------------------------------------------------
    def _set_theme(self, mode: str | None = None, dark_pages: bool | None = None) -> None:
        theme().configure(mode, dark_pages)
        self.db.set_setting("theme", theme().mode)
        self.db.set_setting("dark_pages", "1" if theme().dark_pages else "0")

    def _on_theme_changed(self) -> None:
        self.theme_menu.setIcon(icon("moon" if theme().dark else "sun"))
        self.dark_pages_action.setEnabled(theme().dark)

    def _toggle_toc(self, on: bool) -> None:
        self.sidebar.setVisible(on and self.toc.topLevelItemCount() > 0)
        self._save_panels()

    def _toggle_chat(self, on: bool) -> None:
        if on:
            self.calc_action.setChecked(False)
        self._sync_right()
        self._save_panels()

    def _toggle_calc(self, on: bool) -> None:
        if on:
            self.chat_action.setChecked(False)
        self._sync_right()
        self._save_panels()
        if on and self.right.isVisible():
            self.calc.focus_input()

    def _set_finished(self, on: bool) -> None:
        if self.book_id is not None:
            self.db.set_finished(self.book_id, on)

    def _save_panels(self) -> None:
        """Remember per book which sidebars are open, so reopening it restores them."""
        if self.book_id is None:
            return
        right = "chat" if self.chat_action.isChecked() else "calc" if self.calc_action.isChecked() else ""
        self.db.update_book(self.book_id, show_toc=int(self.toc_action.isChecked()), right_panel=right)

    def _sync_right(self) -> None:
        """Show whichever of chats / calculator is toggled on, or neither."""
        panel = self.chat if self.chat_action.isChecked() else self.calc if self.calc_action.isChecked() else None
        if panel:
            self.right.setCurrentWidget(panel)
        self.right.setVisible(panel is not None and self.book_id is not None)

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
        self.calc.set_book(None)
        self.library.refresh()
        self.center.setCurrentWidget(self.library)
        self._set_book_ui(False)

    def _set_book_ui(self, reading: bool) -> None:
        for a in self.book_actions:
            a.setEnabled(reading)
        for a in self.book_items:
            a.setVisible(reading)
        self.chat_action.setEnabled(reading)
        self.calc_action.setEnabled(reading)
        self._sync_right()
        has_toc = reading and self.toc.topLevelItemCount() > 0
        self.toc_action.setEnabled(has_toc)
        self.sidebar.setVisible(has_toc and self.toc_action.isChecked())

    def browse(self) -> None:
        start = self.db.get_setting("last_dir")
        path, _ = QFileDialog.getOpenFileName(self, "Open book", start, "Books (*.pdf *.epub)")
        if path:
            self.open_book(path)

    # -- web pages ------------------------------------------------------------------
    def ask_url(self) -> None:
        dialog = _UrlDialog(self)
        if dialog.exec() and dialog.url():
            self.open_url(dialog.url())

    def open_url(self, url: str, refetch: bool = False) -> None:
        """Open the saved snapshot of a page, or fetch one (always, with `refetch`)."""
        try:
            url = normalize_url(url)
        except WebImportError as e:
            self._warn("Could not open web page", str(e))
            return
        if not refetch:
            for book in self.db.books_from_url(url):
                if os.path.exists(book["path"]):
                    self.open_book(book["path"])
                    return
        if self.web.busy:
            self._warn("Open web page", "Another page is still being fetched.", QMessageBox.Icon.Information)
            return
        self._web_progress = QProgressDialog("Fetching…", "Hide", 0, 0, self)
        self._web_progress.setWindowTitle("Open web page")
        self._web_progress.setMinimumDuration(0)
        self._web_progress.setMinimumSize(self._web_progress.sizeHint().expandedTo(QSize(380, 0)))
        keep_minimum_size(self._web_progress)
        self._web_progress.show()
        self.web.start(url)

    def _warn(self, title: str, text: str, kind=QMessageBox.Icon.Warning) -> None:
        box = QMessageBox(kind, title, text, QMessageBox.StandardButton.Ok, self)
        keep_minimum_size(box)
        box.exec()

    def _close_web_progress(self) -> None:
        if self._web_progress:
            self._web_progress.close()
            self._web_progress.deleteLater()
            self._web_progress = None

    def _on_web_done(self, path: str, url: str) -> None:
        self._close_web_progress()
        if self.open_book(path, remember_dir=False):
            self.db.update_book(self.book_id, source_url=url)

    def _on_web_failed(self, message: str) -> None:
        self._close_web_progress()
        self._warn("Could not open web page", message)

    def open_book(self, path: str, remember_dir: bool = True) -> bool:
        self._save_position()
        try:
            doc = Document(path)
            if doc.page_count == 0:
                raise ValueError("the document has no pages")
        except Exception as e:
            self._warn("Could not open book", f"{path}\n\n{e}")
            return False
        if self.doc:
            self.doc.close()
        self.doc = doc
        book = self.db.upsert_book(file_hash(path), path, doc.title, doc.page_count)
        self.book_id = book["id"]
        if remember_dir:
            self.db.set_setting("last_dir", str(path).rsplit("/", 1)[0])
        self.setWindowTitle(f"{book['title']} — Study Reader")

        self._fill_toc()
        self.toc_action.setChecked(bool(book["show_toc"]))
        self.chat_action.setChecked(book["right_panel"] == "chat")
        self.calc_action.setChecked(book["right_panel"] == "calc")
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, doc.page_count)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f"of {doc.page_count}")
        self.center.setCurrentWidget(self.reader)
        self._set_book_ui(True)
        self.crop_action.setChecked(bool(book["crop_margins"]))
        self.finished_action.setChecked(book["finished_at"] is not None)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.reader.set_document(doc, book["last_page"], book["zoom"], crop=bool(book["crop_margins"]))
        finally:
            QApplication.restoreOverrideCursor()
        self._on_page_changed(self.reader.current_page())
        self.chat.set_book(self.book_id)
        self.calc.set_book(self.book_id)
        self._refresh_highlights()
        return True

    def _fill_toc(self) -> None:
        self.toc.clear()
        parents: dict[int, QTreeWidgetItem] = {}
        for level, title, page in self.doc.toc():
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, page)
            item.setToolTip(0, title)
            parent = parents.get(level - 1)
            parent.addChild(item) if parent else self.toc.addTopLevelItem(item)
            parents[level] = item
            for deeper in [k for k in parents if k > level]:
                del parents[deeper]

    # -- highlights -----------------------------------------------------------------
    def _refresh_highlights(self) -> None:
        if self.book_id is not None:
            # Pending snippets are marked too, so a passage stays highlighted while its question is drafted.
            pending = [{**sn, "chat_id": None} for sn in self.chat.pending_snippets()]
            self.reader.set_highlights(self.db.book_snippets(self.book_id) + pending)

    def _highlight_menu(self, snippets: list[dict], pos) -> None:
        """Clicked highlight: open one of the chats that asked about it, or start another."""
        menu = QMenu(self)
        metrics = menu.fontMetrics()
        chat_ids = list(dict.fromkeys(sn["chat_id"] for sn in reversed(snippets) if sn["chat_id"] is not None))
        for chat_id in chat_ids:
            chat = self.db.chat(chat_id)
            if chat:
                title = metrics.elidedText(chat["title"], Qt.TextElideMode.ElideRight, 360)
                menu.addAction(icon("message", "muted"), title).triggered.connect(
                    lambda _=False, c=chat_id: self._show_chat(lambda: self.chat.open_chat(c))
                )
        menu.addSeparator()
        latest = snippets[-1]
        menu.addAction(icon("message-plus"), "New chat about this").triggered.connect(
            lambda: self._show_chat(lambda: self.chat.add_snippet(self.reader.snippet_from(latest), True))
        )
        menu.addAction("Add to current chat").triggered.connect(
            lambda: self._show_chat(lambda: self.chat.add_snippet(self.reader.snippet_from(latest), False))
        )
        menu.exec(pos)

    # -- quizzes --------------------------------------------------------------------
    def quiz(self) -> None:
        """Quiz on the selection if there is one, else on the section being read."""
        if self.doc is None or self.reader.choose_quiz():
            return
        page = self.reader.current_page()
        first, last, title = quiz.section_around(self.doc.toc(), page, self.doc.page_count)
        question, chat_title = quiz.request(first, last, title)
        self._show_chat(lambda: self.chat.start_quiz(question, chat_title, first))

    def _quiz_on(self, snippet: dict) -> None:
        question, title = quiz.snippet_request(snippet)
        self._show_chat(lambda: self.chat.start_quiz(question, title, snippet["page"], snippet))

    def _show_chat(self, then) -> None:
        if not self.chat_action.isChecked():
            self.chat_action.trigger()
        then()

    def _set_crop(self, on: bool) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)  # finding the margins renders pages
        try:
            self.reader.set_crop(on)
        finally:
            QApplication.restoreOverrideCursor()
        if self.book_id is not None:
            self.db.update_book(self.book_id, crop_margins=int(on))

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


class _MinimumSizeKeeper(QObject):
    def eventFilter(self, dialog, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            want = dialog.size().expandedTo(dialog.minimumSize()).expandedTo(dialog.minimumSizeHint())
            if want != dialog.size():
                QTimer.singleShot(0, dialog, lambda: dialog.resize(want))
        return False


def keep_minimum_size(dialog: QDialog) -> None:
    """On Wayland (seen on COSMIC) a new dialog's first size loses the title bar's height,
    squashing its contents until it is clicked. Once that size arrives, growing back sticks."""
    dialog.installEventFilter(_MinimumSizeKeeper(dialog))


class _UrlDialog(QDialog):
    """Asks for a web address, filled in from the clipboard when it holds a link."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open web page")
        self.setMinimumWidth(560)
        keep_minimum_size(self)
        hint = _muted("LessWrong posts, sequences and wiki pages, or any article.")
        self.edit = QLineEdit(objectName="urlInput", placeholderText="https://www.lesswrong.com/…")
        self.edit.setClearButtonEnabled(True)
        clip = QApplication.clipboard().text().strip()
        if clip.startswith(("http://", "https://")) and "\n" not in clip:
            self.edit.setText(clip)
            self.edit.selectAll()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.open_button = QPushButton("Open", default=True)
        self.open_button.setProperty("primary", True)
        self.open_button.clicked.connect(self.accept)
        self.edit.textChanged.connect(lambda text: self.open_button.setEnabled(bool(text.strip())))
        self.open_button.setEnabled(bool(self.edit.text().strip()))
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.open_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(_section("Web address"))
        layout.addWidget(self.edit)
        layout.addWidget(hint)
        layout.addSpacing(6)
        layout.addLayout(buttons)

    def url(self) -> str:
        return self.edit.text().strip()


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "section")
    return label


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "muted")
    return label
