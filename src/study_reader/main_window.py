"""Main window: the navigation rail | the open book (contents | reader | chats, calculator, notes
or review) or a page: Home, Library, Review, Cards, Stats."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import threading

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QToolBar, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import gamify, quiz, srs, variants
from .calculator import CalculatorPanel
from .chat_panel import MODELS, ChatPanel
from .chat_service import ChatWorker
from .convert_dialog import ConversionManager
from .db import Database, data_dir
from .document import Document, file_hash
from .find_view import BookFind
from .home_view import HomeView
from .library_view import LibraryView
from .nav import PAGES, NavRail
from .notes import NotesPanel
from .phone_sync_dialog import PhoneSync, PhoneSyncDialog
from .read_aloud import ReadAloud
from .render_dialog import AudioSync, ColabRender, RenderDialog, local_audio
from .reader_view import ReaderView
from .review import ReviewPanel, VariantMaker
from .theme import MODES, icon, theme
from .tts_engines import ENGINES
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
        srs.backfill(self.db)  # once: cards for quizzes from before there were cards
        self._learn_tocs()  # once a book: section tags for quiz cards from before there were any
        self.conversions = ConversionManager(self.db, self.worker, self)  # Claude Conversion of cards
        self.conversions.cardsChanged.connect(lambda: self._on_cards_changed())
        self.browser = None  # browser.CardBrowser, the Cards page, made when first shown
        self.stats_window = None  # stats_view.StatsWindow, the Stats page, likewise
        self._optimizer: _Optimizer | None = None
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
        self.sidebar_title = _section("Contents")
        side.addWidget(self.sidebar_title)
        self.reader = ReaderView()
        self.find = BookFind(self.reader, self)  # Ctrl+F; its results take the contents' place
        self.find.activeChanged.connect(self._on_find_active)
        self.find.summaryChanged.connect(lambda text: self.find.active and self.sidebar_title.setText(text))
        self.side_stack = QStackedWidget()
        self.side_stack.addWidget(self.toc)
        self.side_stack.addWidget(self.find.list)
        side.addWidget(self.side_stack, 1)

        self.home = HomeView(self.db)
        self.read_aloud = ReadAloud(self.db, self.reader, self)
        self.read_aloud.renderRequested.connect(self.render_on_colab)
        self.audio_sync = AudioSync(self.db, self)  # audio the old Drive notebook rendered, copied here
        self.audio_sync.synced.connect(self._on_audio_synced)
        self.colab = ColabRender(self)  # read-aloud audio rendered on a Colab GPU through the colab CLI
        self.colab.synced.connect(self._on_audio_synced)
        self.colab.resume()
        self.book_view = QWidget()  # the reader, with the read-aloud bar under it
        col = QVBoxLayout(self.book_view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self.reader, 1)
        col.addWidget(self.read_aloud.bar)
        self.library = LibraryView(self.db)
        self.variants = VariantMaker(self.db, self.worker, self)  # new numbers for reviewed calculation cards
        self.review_all = ReviewPanel(self.db, all_books=True, variants=self.variants)  # every book's cards: the Review page
        self.cards_page = _Page()  # holds the browser once made
        self.stats_page = _Page()  # holds the statistics once made
        self.center = QStackedWidget()
        # The pages the navigation rail switches between; "book" is the open book.
        self.pages = {
            "home": self.home, "library": self.library, "review": self.review_all,
            "cards": self.cards_page, "stats": self.stats_page, "book": self.book_view,
        }
        for w in self.pages.values():
            self.center.addWidget(w)
        self.chat = ChatPanel(self.db, self.worker, self.variants)
        self.chat.current_page = self.reader.current_page
        self.calc = CalculatorPanel(self.db)
        self.notes = NotesPanel(self.db)
        self.review = ReviewPanel(self.db, variants=self.variants)
        self.right = QStackedWidget()  # chats, the calculator, notes and review take turns in the same slot
        for w in (self.chat, self.calc, self.notes, self.review):
            self.right.addWidget(w)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        for w in (self.sidebar, self.center, self.right):
            split.addWidget(w)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 800, 460])

        self._build_actions()
        self.nav = NavRail(self._build_settings_menu())
        self.nav.pageRequested.connect(self.show_page)
        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.nav)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self.toolbar)  # the reader's: beside the rail, over the book
        col.addWidget(split, 1)
        row.addLayout(col, 1)
        self.setCentralWidget(central)

        self.home.startRequested.connect(self.review_everything)
        self.home.openRequested.connect(self._open_book_id)
        self.home.reviewBookRequested.connect(self.review_book)
        self.home.reviewDeckRequested.connect(self._review_deck)
        self.home.pageRequested.connect(self.show_page)
        self.home.browseRequested.connect(self.browse)
        self.home.webRequested.connect(self.ask_url)
        self.library.openRequested.connect(self.open_book)
        self.library.browseRequested.connect(self.browse)
        self.library.webRequested.connect(self.ask_url)
        self.library.refetchRequested.connect(self.refetch)
        self.library.reviewBookRequested.connect(self.review_book)
        self.web = WebImporter(data_dir() / "web", self)
        self.web.status.connect(lambda text: self._web_progress and self._web_progress.setLabelText(text))
        self.web.finished.connect(self._on_web_done)
        self.web.failed.connect(self._on_web_failed)
        self._web_progress: QProgressDialog | None = None
        self._refetching: int | None = None  # the book a fresh web snapshot replaces
        self.reader.snippetChosen.connect(lambda sn, new: self._show_chat(lambda: self.chat.add_snippet(sn, new)))
        self.reader.quizChosen.connect(self._quiz_on)
        self.reader.noteChosen.connect(lambda sn: self._show_notes(lambda: self.notes.add_note(sn)))
        self.reader.highlightClicked.connect(self._highlight_menu)
        self.reader.readRequested.connect(self.read_aloud.read_selection)
        self.chat.listenRequested.connect(lambda mid: self.read_aloud.read_reply(self.chat.transcript, mid))
        self.read_aloud.activeChanged.connect(self._on_reading)
        self.chat.highlightsChanged.connect(self._refresh_highlights)
        self.notes.notesChanged.connect(self._refresh_highlights)
        self.notes.jumpRequested.connect(self.reader.flash)
        self.reader.pageChanged.connect(self._on_page_changed)
        self.reader.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{round(z * 100)}%"))
        self.chat.jumpRequested.connect(self.reader.flash)
        self.chat.pageRequested.connect(self.reader.go_to)
        self.chat.cardsChanged.connect(self._on_cards_changed)
        for panel in (self.review, self.review_all):
            panel.countsChanged.connect(self._on_cards_changed)
            panel.sourceRequested.connect(self._review_source)
            panel.chatRequested.connect(self._review_chat)
            panel.browseRequested.connect(self.show_browser)
            panel.statsRequested.connect(lambda p=panel: self.show_stats(p.deck, None if p.all_books else p.book_id))
            panel.editRequested.connect(lambda card_id: self.show_browser().show_card(card_id))
        theme().changed.connect(self._on_theme_changed)
        self._on_theme_changed()

        self._save_timer = QTimer(self, interval=10_000)
        self._save_timer.timeout.connect(self._save_position)
        self._save_timer.start()
        # The phone app's reviews come in through a shared folder; new numbers for the calculation cards it reviewed.
        self.phone_sync = PhoneSync(self.db, after_review=self.variants.after_review, parent=self)
        self.phone_sync.synced.connect(lambda result: result.applied and self._on_cards_changed())
        QTimer.singleShot(0, self.phone_sync.sync)
        self.show_page("home")

    def _build_actions(self) -> None:
        """Window-wide actions and the reader's toolbar (shown only while reading)."""
        bar = self.toolbar = QToolBar("Main")
        bar.setMovable(False)
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

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

        # Pages, from anywhere (the rail shows them too).
        action("Library", "Ctrl+L", lambda: self.show_page("library"))
        for n, (name, *_rest) in enumerate(PAGES, 1):
            action(name, f"Alt+{n}", lambda _=False, page=name: self.show_page(page))
        action("Book", f"Alt+{len(PAGES) + 1}", lambda: self.show_page("book"))
        action("Open…", QKeySequence.StandardKey.Open, self.browse)
        self.web_action = action("Open web page…", "Ctrl+Shift+O", self.ask_url)
        action("Browse cards", "Ctrl+Shift+B", self.show_browser)
        self.close_action = action("Close book", "Ctrl+W", self.close_book)
        find = action("Find in book", QKeySequence.StandardKey.Find, self.find.open, "search")
        find_next = action("Find next", "F3", self.find.next)
        find_prev = action("Find previous", "Shift+F3", self.find.prev)

        self.toc_action = action("Contents", "F9", self._toggle_toc, "panel-left", checkable=True)
        self.toc_action.setChecked(True)
        bar.addAction(self.toc_action)
        self.book_title = _muted()
        self.book_title.setObjectName("bookTitle")
        self.book_title.setMaximumWidth(280)
        bar.addWidget(self.book_title)
        self.finished_action = action("Finished", "", self._set_finished, "check-circle", checkable=True)
        self.finished_action.setToolTip("Mark this book as finished (moves it to the library's Finished section)")
        bar.addAction(self.finished_action)
        bar.addSeparator()

        zoom_out = action("Zoom out", "Ctrl+-", self.reader.zoom_out, "zoom-out")
        zoom_in = action("Zoom in", "Ctrl+=", self.reader.zoom_in, "zoom-in")
        fit = action("Fit width", "Ctrl+0", self.reader.fit_width, "fit-width")
        self.crop_action = action("Crop margins", "Ctrl+Shift+0", self._set_crop, "crop", checkable=True)
        self.zoom_label = _muted()
        self.zoom_label.setMinimumWidth(42)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addAction(zoom_out)
        bar.addWidget(self.zoom_label)
        bar.addAction(zoom_in)
        bar.addAction(fit)
        bar.addAction(self.crop_action)
        bar.addAction(find)
        bar.addSeparator()

        self.page_spin = QSpinBox()
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setMinimumWidth(52)
        self.page_spin.setToolTip("Go to page")
        self.page_spin.valueChanged.connect(lambda v: self.reader.go_to(v - 1))
        self.page_total = _muted()
        bar.addWidget(self.page_spin)
        bar.addWidget(self.page_total)
        bar.addSeparator()

        self.region_action = action(
            "Screenshot region", "Ctrl+Shift+S", self.reader.set_region_mode, "scan", checkable=True
        )
        bar.addAction(self.region_action)
        labelled(self.region_action)
        self.quiz_action = action("Quiz me", "Ctrl+Shift+Q", self.quiz, "quiz")
        self.quiz_action.setToolTip(
            "Quiz me on the selection, or else the section being read (Ctrl+Shift+Q); "
            "the arrow picks how much to cover"
        )
        bar.addAction(self.quiz_action)
        labelled(self.quiz_action)
        self.quiz_menu = QMenu(self)
        self.quiz_menu.aboutToShow.connect(self._fill_quiz_menu)
        quiz_button = bar.widgetForAction(self.quiz_action)
        quiz_button.setMenu(self.quiz_menu)
        quiz_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        new_chat = action("New chat", "Ctrl+N", self.chat.new_chat, "message-plus")
        # Act on the current selection without reaching for the popup. Only while the reader has
        # focus: as window shortcuts they'd swallow Ctrl+Enter in the review, quiz and note pages.
        for text, shortcut, new in (("Ask in new chat", "Ctrl+Return", True),
                                    ("Add to current chat", "Ctrl+Shift+Return", False)):
            a = QAction(text, self.reader, shortcut=QKeySequence(shortcut))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            a.triggered.connect(lambda _=False, new=new: self.reader.choose(new))
            self.reader.addAction(a)
        add_note = action("Add note", "Ctrl+Shift+N", self.reader.choose_note)
        ra = self.read_aloud
        self.read_action = action("Read aloud", "", self._toggle_reading, "volume", checkable=True)
        self.read_action.setToolTip("Read the book aloud from this page, following along on the page (F5 plays and pauses)")
        bar.addAction(self.read_action)
        labelled(self.read_action)
        self.play_action = action("Play or pause reading aloud", "F5", ra.play_pause)
        self.stop_reading_action = action("Stop reading aloud", "Shift+F5", ra.stop)
        self.prev_sentence_action = action("Previous sentence", "F4", ra.prev_sentence)
        self.next_sentence_action = action("Next sentence", "F6", ra.next_sentence)
        self.book_actions = [
            zoom_out, zoom_in, fit, self.crop_action, self.region_action, self.quiz_action,
            self.finished_action, new_chat, add_note, self.close_action, self.read_action, self.play_action,
            find, find_next, find_prev,
        ]
        self._on_reading(False)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        # The right-hand panels; Review leads, with the open book's count on it.
        self.review_action = action("Review", "F8", lambda on: self._toggle_right("review", on), "cards", checkable=True)
        bar.addAction(self.review_action)
        labelled(self.review_action)
        self.chat_action = action("Chats", "F10", lambda on: self._toggle_right("chat", on), "message", checkable=True)
        self.chat_action.setChecked(True)
        bar.addAction(self.chat_action)
        labelled(self.chat_action)
        self.notes_action = action("Notes", "F12", lambda on: self._toggle_right("notes", on), "note", checkable=True)
        bar.addAction(self.notes_action)
        self.calc_action = action("Calculator", "F11", lambda on: self._toggle_right("calc", on), "calculator", checkable=True)
        bar.addAction(self.calc_action)
        # The right-hand slot's panels, by the name books.right_panel stores; one at most is shown.
        self.right_panels = {
            "chat": (self.chat_action, self.chat),
            "calc": (self.calc_action, self.calc),
            "notes": (self.notes_action, self.notes),
            "review": (self.review_action, self.review),
        }

    def _build_settings_menu(self) -> QMenu:
        """The rail's Settings menu."""
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
        voice_menu = menu.addMenu(icon("volume"), "Read-aloud voice")
        group = QActionGroup(self)
        self.engine_actions = {}
        for name, label in ENGINES:
            a = voice_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.read_aloud.engine_name == name)
            a.triggered.connect(lambda _=False, n=name: self.read_aloud.set_engine(n))
            group.addAction(a)
            self.engine_actions[name] = a
        voice_menu.addSeparator()
        voice_menu.addAction("Render audio on Colab…").triggered.connect(self.render_on_colab)
        menu.addSeparator()
        menu.addAction(icon("import"), "Migrate from Anki…").triggered.connect(self.migrate_from_anki)
        review_menu = menu.addMenu(icon("cards"), "Review settings")
        review_menu.setToolTipsVisible(True)
        review_menu.addAction("New cards per day…").triggered.connect(self._set_new_per_day)
        review_menu.addAction("Desired retention…").triggered.connect(self._set_retention)
        review_menu.addAction("Optimize FSRS parameters…").triggered.connect(self._optimize)
        review_menu.addAction("Use default FSRS parameters").triggered.connect(self._reset_parameters)
        review_menu.addSeparator()
        self.rewards_action = review_menu.addAction("Rewards and streaks")
        self.rewards_action.setCheckable(True)
        self.rewards_action.setChecked(gamify.enabled(self.db))
        self.rewards_action.toggled.connect(self._set_rewards)
        review_menu.addAction("Daily goal…").triggered.connect(self._set_daily_goal)
        review_menu.addSeparator()
        self.variants_action = review_menu.addAction("New numbers for problems after each review")
        self.variants_action.setToolTip(
            "After you review a numeric or free-response card, Claude writes a fresh version of the problem "
            "for next time. Off: cards always show their own question. Single cards can be turned off in "
            "the card browser.")
        self.variants_action.setCheckable(True)
        self.variants_action.setChecked(variants.enabled(self.db))
        self.variants_action.toggled.connect(self._set_variants)
        menu.addAction(icon("cards"), "Phone sync…").triggered.connect(lambda: PhoneSyncDialog(self.phone_sync, self).exec())
        return menu

    # -- appearance ---------------------------------------------------------------
    def _set_theme(self, mode: str | None = None, dark_pages: bool | None = None) -> None:
        theme().configure(mode, dark_pages)
        self.db.set_setting("theme", theme().mode)
        self.db.set_setting("dark_pages", "1" if theme().dark_pages else "0")

    def _on_theme_changed(self) -> None:
        self.theme_menu.setIcon(icon("moon" if theme().dark else "sun"))
        self.dark_pages_action.setEnabled(theme().dark)

    def _toggle_toc(self, on: bool) -> None:
        if self.find.active and not on:
            self.find.close()  # the button hides the results too
        self._sync_sidebar()
        self._save_panels()

    def _sync_sidebar(self) -> None:
        """The sidebar shows find results while the find bar is open, else the contents if toggled on."""
        has_toc = self.reading and self.toc.topLevelItemCount() > 0
        finding = self.reading and self.find.active
        self.toc_action.setEnabled(has_toc)
        self.side_stack.setCurrentWidget(self.find.list if finding else self.toc)
        if not finding:
            self.sidebar_title.setText("Contents")
        self.sidebar.setVisible(finding or (has_toc and self.toc_action.isChecked()))

    def _on_find_active(self, on: bool) -> None:
        self._sync_sidebar()

    def _toggle_right(self, name: str, on: bool) -> None:
        if on:
            for other, (a, _) in self.right_panels.items():
                if other != name:
                    a.setChecked(False)
        self._sync_right()
        self._save_panels()
        if on and name == "calc" and self.right.isVisible():
            self.calc.focus_input()
        if on and name == "review" and self.right.isVisible():
            self.review.refresh()
            self.review.focus()

    def _set_finished(self, on: bool) -> None:
        if self.book_id is not None:
            self.db.set_finished(self.book_id, on)

    def _save_panels(self) -> None:
        """Remember per book which sidebars are open, so reopening it restores them."""
        if self.book_id is None:
            return
        right = next((name for name, (a, _) in self.right_panels.items() if a.isChecked()), "")
        self.db.update_book(self.book_id, show_toc=int(self.toc_action.isChecked()), right_panel=right)

    def _sync_right(self) -> None:
        """Show whichever of chats / calculator / notes / review is toggled on, or none."""
        panel = next((w for a, w in self.right_panels.values() if a.isChecked()), None)
        if panel:
            self.right.setCurrentWidget(panel)
        self.right.setVisible(panel is not None and self.reading)

    # -- pages ----------------------------------------------------------------------
    @property
    def reading(self) -> bool:
        """The open book is on screen (not another page)."""
        return self.doc is not None and self.center.currentWidget() is self.book_view

    def page(self) -> str:
        """The page on screen: a name from self.pages."""
        return next(name for name, w in self.pages.items() if w is self.center.currentWidget())

    def show_page(self, name: str, refresh: bool = True) -> None:
        """Switch the center to a page of the rail. The open book stays open behind the other
        pages; "book" goes back to it."""
        if name == "book" and self.doc is None:
            name = "home"
        current = self.center.currentWidget()
        if current is self.book_view and name != "book":
            self._save_position()
            self.read_aloud.pause()  # its controls are on the book page; F5 there resumes
            if self.region_action.isChecked():
                self.region_action.trigger()
        if current is self.cards_page and self.browser is not None and name != "cards":
            self.browser.save_note()
        if name == "cards":
            self._ensure_browser()
        elif name == "stats":
            self._ensure_stats()
        self.center.setCurrentWidget(self.pages[name])
        self.nav.set_current(name)
        self._set_book_ui(name == "book")
        # The browser's own Ctrl+Shift+O (Open in book) wins while it is on screen.
        self.web_action.setEnabled(name != "cards")
        if name == "book":
            self.setWindowTitle(f"{self.db.book(self.book_id)['title']} — Study Reader")
        else:
            title = {"library": "Library", "review": "Review", "cards": "Cards", "stats": "Statistics"}.get(name)
            self.setWindowTitle(f"{title} — Study Reader" if title else "Study Reader")
        if refresh:
            if name == "home":
                self.home.refresh()
                self.home.focus()
            elif name == "library":
                self.library.refresh()
                self.library.list.setFocus()
            elif name == "review":
                self.review_all.refresh()
                self.review_all.focus()
            elif name == "cards":
                self.browser.refresh()
            elif name == "stats":
                self.stats_window.show_scope()
        self._update_review_counts()

    def show_library(self) -> None:
        self.show_page("library")

    def close_book(self) -> None:
        """Close the open book (replies still arriving are saved) and go Home."""
        self._save_position()
        if self.region_action.isChecked():
            self.region_action.trigger()
        self.read_aloud.set_document(None)
        self.find.set_document(None)
        self.reader.set_document(None)
        if self.doc:
            self.doc.close()
        self.doc, self.book_id = None, None
        self.toc.clear()
        self.page_total.setText("")
        self.book_title.setText("")
        self.chat.set_book(None)
        self.calc.set_book(None)
        self.notes.set_book(None)
        self.review.set_book(None)
        self.nav.set_book(None)
        self.show_page("home")

    def _set_book_ui(self, reading: bool) -> None:
        self.toolbar.setVisible(reading)
        for a in self.book_actions:
            a.setEnabled(reading)
        for a, _ in self.right_panels.values():
            a.setEnabled(reading)
        self._sync_right()
        self._sync_sidebar()

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

    def refetch(self, book_id: int) -> None:
        """Fetch a web snapshot's page again and swap it into the same library entry."""
        book = self.db.book(book_id)
        if book and book["source_url"] and self.open_url(book["source_url"], refetch=True):
            self._refetching = book_id

    def open_url(self, url: str, refetch: bool = False) -> bool:
        """Open the saved snapshot of a page, or fetch one (always, with `refetch`).
        True if a fetch was started."""
        try:
            url = normalize_url(url)
        except WebImportError as e:
            self._warn("Could not open web page", str(e))
            return False
        if not refetch:
            for book in self.db.books_from_url(url):
                if os.path.exists(book["path"]):
                    self.open_book(book["path"])
                    return False
        if self.web.busy:
            self._warn("Open web page", "Another page is still being fetched.", QMessageBox.Icon.Information)
            return False
        self._refetching = None
        self._web_progress = QProgressDialog("Fetching…", "Hide", 0, 0, self)
        self._web_progress.setWindowTitle("Open web page")
        self._web_progress.setMinimumDuration(0)
        self._web_progress.setMinimumSize(self._web_progress.sizeHint().expandedTo(QSize(380, 0)))
        keep_minimum_size(self._web_progress)
        self._web_progress.show()
        self.web.start(url)
        return True

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
        book_id, self._refetching = self._refetching, None
        old = self.db.book(book_id) if book_id is not None else None
        if old:
            path = self._replace_snapshot(old, path)
        if self.open_book(path, remember_dir=False):
            self.db.update_book(self.book_id, source_url=url)

    def _replace_snapshot(self, old, path: str) -> str:
        """Put a fresh snapshot in place of an old one, in the same library entry (so its
        chats and notes stay), and return the path to open."""
        try:
            doc = Document(path)
            title, pages = doc.title, doc.page_count
            doc.close()
        except Exception:
            return path  # open_book reports it
        new_hash = file_hash(path)
        old_path = Path(old["path"])
        if new_hash == old["hash"] and old_path.exists():
            os.remove(path)  # the page hasn't changed: keep the file the entry already has
            return str(old_path)
        if self.db.book_by_hash(new_hash):
            return path  # this snapshot is already another entry; open that one
        self.db.replace_book_file(old["id"], new_hash, path, title, pages)
        self.worker.forget_base(old["id"])
        if old_path != Path(path) and old_path.parent == data_dir() / "web" and old_path.exists():
            old_path.unlink()  # only snapshots the app made itself
        return path

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
        srs.learn_toc(self.db, self.book_id, doc.toc())

        self.find.set_document(doc)
        self._fill_toc()
        self.toc_action.setChecked(bool(book["show_toc"]))
        for name, (a, _) in self.right_panels.items():
            a.setChecked(book["right_panel"] == name)
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, doc.page_count)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f"of {doc.page_count}")
        self.book_title.setText(self.book_title.fontMetrics().elidedText(book["title"], Qt.TextElideMode.ElideRight, 260))
        self.book_title.setToolTip(book["title"])
        self.nav.set_book(book["title"])
        self.show_page("book", refresh=False)
        self.crop_action.setChecked(bool(book["crop_margins"]))
        self.finished_action.setChecked(book["finished_at"] is not None)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.reader.set_document(doc, book["last_page"], book["zoom"], crop=bool(book["crop_margins"]))
        finally:
            QApplication.restoreOverrideCursor()
        self.read_aloud.set_document(doc, local_audio(book["hash"]))
        self.audio_sync.start(book["hash"])  # bring in any audio the Drive notebook has finished since
        self._on_page_changed(self.reader.current_page())
        self.chat.set_book(self.book_id)
        self.calc.set_book(self.book_id)
        self.notes.set_book(self.book_id)
        self.review.set_book(self.book_id)
        self._update_review_counts()
        self._refresh_highlights()
        return True

    def _learn_tocs(self) -> None:
        """The contents of books whose quiz cards have pages but which haven't been opened since
        cards got section tags, so those cards get them."""
        for row in self.db.conn.execute(
            "SELECT id, path FROM books b WHERE toc IS NULL AND EXISTS "
            "(SELECT 1 FROM cards c WHERE c.book_id=b.id AND c.quiz IS NOT NULL AND c.page IS NOT NULL)"
        ).fetchall():
            try:
                doc = Document(row["path"])
            except Exception:
                continue  # missing for now; tagged when it's next opened
            try:
                srs.learn_toc(self.db, row["id"], doc.toc())
            finally:
                doc.close()

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
            self.reader.set_highlights(self.db.book_snippets(self.book_id) + pending, self.db.notes(self.book_id))

    def _highlight_menu(self, snippets: list[dict], notes: list[dict], pos) -> None:
        """Clicked highlight: open its note or one of the chats that asked about it, or start another."""
        menu = QMenu(self)
        metrics = menu.fontMetrics()
        for note in notes:
            body = " ".join(note["body"].split()) or "Empty note"
            title = metrics.elidedText(body, Qt.TextElideMode.ElideRight, 360)
            menu.addAction(icon("note", "muted"), title).triggered.connect(
                lambda _=False, n=note["id"]: self._show_notes(lambda: self.notes.reveal(n))
            )
        if notes:
            menu.addSeparator()
        chat_ids = list(dict.fromkeys(sn["chat_id"] for sn in reversed(snippets) if sn["chat_id"] is not None))
        for chat_id in chat_ids:
            chat = self.db.chat(chat_id)
            if chat:
                title = metrics.elidedText(chat["title"], Qt.TextElideMode.ElideRight, 360)
                menu.addAction(icon("message", "muted"), title).triggered.connect(
                    lambda _=False, c=chat_id: self._show_chat(lambda: self.chat.open_chat(c))
                )
        menu.addSeparator()
        latest = snippets[-1] if snippets else {**notes[-1], "kind": "text"}
        menu.addAction(icon("message-plus"), "New chat about this").triggered.connect(
            lambda: self._show_chat(lambda: self.chat.add_snippet(self.reader.snippet_from(latest), True))
        )
        menu.addAction("Add to current chat").triggered.connect(
            lambda: self._show_chat(lambda: self.chat.add_snippet(self.reader.snippet_from(latest), False))
        )
        if notes:
            menu.addAction(icon("trash", "muted"), "Delete note" if len(notes) == 1 else "Delete notes").triggered.connect(
                lambda: [self.notes.remove(n["id"]) for n in notes]
            )
        elif latest["kind"] == "text":
            menu.addAction(icon("note"), "Add note…").triggered.connect(
                lambda: self._show_notes(lambda: self.notes.add_note(latest))
            )
        menu.exec(pos)

    # -- quizzes --------------------------------------------------------------------
    def quiz(self) -> None:
        """Quiz on the selection if there is one, else on the section being read."""
        if self.doc is None or self.reader.choose_quiz():
            return
        self._quiz_scope(self._quiz_level())

    def _quiz_level(self) -> int:
        """The contents depth "Quiz me" covers in this book: the one last picked from its menu."""
        return int(self.db.get_setting(f"quiz_level:{self.book_id}", str(quiz.DEFAULT_LEVEL)))

    def _quiz_scope(self, level: int) -> None:
        page = self.reader.current_page()
        first, last, title = quiz.section_around(self.doc.toc(), page, self.doc.page_count, level)
        whole = (first, last) == (0, self.doc.page_count - 1) and level == quiz.DOCUMENT_LEVEL
        question, chat_title = quiz.request(first, last, title, whole)
        self._show_chat(lambda: self.chat.start_quiz(question, chat_title, first, last=last))

    def _fill_quiz_menu(self) -> None:
        """The whole document (if short), the contents entries around the page being read, outermost
        first, and the page alone; the one the button would quiz on is checked. Picking one quizzes on
        it and remembers its depth."""
        self.quiz_menu.clear()
        if self.doc is None:
            return
        page = self.reader.current_page()
        path = quiz.scopes(self.doc.toc(), page, self.doc.page_count)
        current = quiz.section_around(self.doc.toc(), page, self.doc.page_count, self._quiz_level())

        def pick(level: int) -> None:
            self.db.set_setting(f"quiz_level:{self.book_id}", str(level))
            self._quiz_scope(level)

        group = QActionGroup(self.quiz_menu)
        n = self.doc.page_count
        if n <= quiz.WHOLE_DOCUMENT_PAGES:
            a = self.quiz_menu.addAction(f"Whole document  ({quiz.pages_label(0, n - 1)})")
            a.setCheckable(True)
            a.setChecked(self._quiz_level() == quiz.DOCUMENT_LEVEL)
            a.triggered.connect(lambda: pick(quiz.DOCUMENT_LEVEL))
            group.addAction(a)
            self.quiz_menu.addSeparator()
        for depth, (level, first, last, title) in enumerate(path):
            a = self.quiz_menu.addAction("    " * depth + f"{title}  ({quiz.pages_label(first, last)})".replace("&", "&&"))
            a.setCheckable(True)
            a.setChecked((first, last, title) == current)
            a.triggered.connect(lambda _=False, lvl=level: pick(lvl))
            group.addAction(a)
        if path:
            self.quiz_menu.addSeparator()
        a = self.quiz_menu.addAction(f"This page only  ({quiz.pages_label(page, page)})")
        a.setCheckable(True)
        a.setChecked(self._quiz_level() == quiz.PAGE_LEVEL)
        a.triggered.connect(lambda: pick(quiz.PAGE_LEVEL))
        group.addAction(a)

    def _quiz_on(self, snippet: dict) -> None:
        question, title = quiz.snippet_request(snippet)
        self._show_chat(lambda: self.chat.start_quiz(question, title, snippet["page"], snippet))

    # -- review ---------------------------------------------------------------------
    def show_review_all(self) -> None:
        """The Review page: every deck and book's cards."""
        self.show_page("review")

    def review_everything(self, limit: str = "") -> None:
        """Start reviewing every deck (Home's Start review): "" or a quick session's limit."""
        self.show_page("review")
        self.review_all.choose_deck(None)
        self.review_all.start(limit)
        self.review_all.focus()

    def review_book(self, book_id: int) -> None:
        """Start reviewing one book's cards on the Review page (the Library's and Home's Review)."""
        self.show_page("review")
        self.review_all.choose_book(book_id)
        self.review_all.start()
        self.review_all.focus()

    def _on_cards_changed(self) -> None:
        for panel in (self.review, self.review_all):
            # A card being answered stays put; a hidden panel refreshes when it is shown.
            if panel.card_id is None and panel.isVisible():
                panel.refresh()
        self._update_review_counts()
        if self.home.isVisible():
            self.home.refresh()
        elif self.library.isVisible():
            self.library.refresh()
        elif self.stats_window is not None and self.stats_window.isVisible():
            self.stats_window.refresh()

    def _update_review_counts(self) -> None:
        """The counts on the rail's Review and the reader's Review button."""
        everything = srs.counts(self.db, None)
        self.nav.set_review_badge(everything["due"] + everything["new"])
        c = srs.counts(self.db, self.book_id) if self.book_id is not None else None
        waiting = c["due"] + c["new"] if c else 0
        self.review_action.setText(f"Review · {waiting}" if waiting else "Review")
        self.review_action.setToolTip(
            f"Review this book's cards (F8) · {waiting} card{'s' * (waiting != 1)} to review" if c else "Review (F8)"
        )

    def _review_source(self, book_id: int, page: int) -> None:
        """A card's page: in the open book, or the book opened with its Review panel."""
        if book_id != self.book_id and not self._open_book_id(book_id):
            return
        self.show_page("book")
        if not self.review_action.isChecked():
            self.review_action.trigger()
        if page >= 0:
            self.reader.go_to(page)

    def _review_chat(self, book_id: int, chat_id: int) -> None:
        if book_id != self.book_id and not self._open_book_id(book_id):
            return
        self.show_page("book")
        self._show_chat(lambda: self.chat.open_chat(chat_id))

    def _open_book_id(self, book_id: int) -> bool:
        book = self.db.book(book_id)
        if book is None or not os.path.exists(book["path"]):
            self._warn("Could not open book", f"The book's file is missing:\n{book['path'] if book else book_id}")
            return False
        if book_id == self.book_id and self.doc is not None:
            self.show_page("book")
            return True
        return self.open_book(book["path"], remember_dir=False)

    # -- cards: browser, statistics, Anki --------------------------------------------
    def _ensure_browser(self) -> None:
        if self.browser is None:
            from .browser import CardBrowser
            self.browser = CardBrowser(self.db, self.conversions, embedded=True)
            self.browser.cardsChanged.connect(self._on_cards_changed)
            self.browser.bookRequested.connect(self._browser_book)
            self.browser.reviewDeckRequested.connect(self._review_deck)
            self.browser.statsRequested.connect(lambda deck: self.show_stats(deck or None))
            self.browser.migrateRequested.connect(self.migrate_from_anki)
            self.cards_page.set_widget(self.browser)

    def _ensure_stats(self) -> None:
        if self.stats_window is None:
            from .stats_view import StatsWindow
            self.stats_window = StatsWindow(self.db, window=False)
            self.stats_page.set_widget(self.stats_window)

    def show_browser(self):
        """The Cards page (the browser, made on first use)."""
        self.show_page("cards")
        return self.browser

    def show_stats(self, deck_id: int | None = None, book_id: int | None = None) -> None:
        """The Stats page, for a deck, a book or (neither) everything."""
        self.show_page("stats", refresh=False)
        self.stats_window.show_scope(deck_id=deck_id, book_id=book_id)

    def migrate_from_anki(self) -> None:
        from .anki_dialog import AnkiImportDialog
        dialog = AnkiImportDialog(self.db, self)
        if dialog.exec():
            self._on_migrated(dialog.query)

    def _on_migrated(self, query: str) -> None:
        self._on_cards_changed()
        if query:
            self.show_browser().set_search(query)
        elif self.browser is not None:
            self.browser.refresh()

    def _browser_book(self, book_id: int, page: int) -> None:
        if book_id != self.book_id and not self._open_book_id(book_id):
            return
        self.show_page("book")
        if page >= 0:
            self.reader.go_to(page)

    def _review_deck(self, deck_id: int, start: bool = False) -> None:
        self.show_page("review")
        self.review_all.choose_deck(deck_id)
        if start:
            self.review_all.start()
        self.review_all.focus()

    def _set_retention(self) -> None:
        r, ok = QInputDialog.getDouble(
            self, "Desired retention", "How likely you want to be to remember a card when it comes back (0.70–0.99).\n"
            "Higher means shorter intervals and more reviews.", srs.desired_retention(self.db), 0.7, 0.99, 2)
        if ok:
            self.db.set_setting(srs.RETENTION_SETTING, f"{r:.2f}")
            self._on_cards_changed()

    def _optimize(self) -> None:
        n = srs.optimizable_reviews(self.db)
        if n < srs.MIN_OPTIMIZE_REVIEWS:
            self._warn("Optimize FSRS parameters",
                       f"There are {n} reviews to learn from; optimizing needs at least {srs.MIN_OPTIMIZE_REVIEWS} "
                       "(reviews of cards after their first). Until then the current parameters are a better bet.",
                       QMessageBox.Icon.Information)
            return
        if self._optimizer is not None:
            return
        if importlib.util.find_spec("torch") is None:  # the optimizer's; importing it here would stall the GUI
            self._warn("Optimize FSRS parameters", "The optimizer needs PyTorch, which isn't installed.")
            return
        progress = QProgressDialog(f"Fitting FSRS to {n} reviews…", None, 0, 0, self)
        progress.setWindowTitle("Optimize FSRS parameters")
        progress.setMinimumDuration(0)
        progress.setMinimumWidth(380)
        progress.show()
        self._optimizer = _Optimizer(srs.review_logs(self.db), self)
        self._optimizer.finished.connect(lambda weights, error: self._optimized(weights, error, progress))
        self._optimizer.start()

    def _optimized(self, weights: list, error: str, progress: QProgressDialog) -> None:
        progress.close()
        self._optimizer = None
        if error:
            self._warn("Optimize FSRS parameters", f"Optimizing failed: {error}")
            return
        old = srs.parameters(self.db)
        changed = max(abs(a - b) for a, b in zip(old, weights)) if len(old) == len(weights) else 1
        if changed < 1e-4:
            self._warn("Optimize FSRS parameters", "The current parameters already fit your reviews.",
                       QMessageBox.Icon.Information)
            return
        answer = QMessageBox.question(
            self, "Optimize FSRS parameters",
            "New parameters fitted to your reviews:\n\n" + ", ".join(f"{w:.4f}" for w in weights)
            + "\n\nUse them? Cards keep their memory state; future intervals follow the new parameters.")
        if answer == QMessageBox.StandardButton.Yes:
            try:
                srs.set_parameters(self.db, weights)
            except ValueError as e:
                self._warn("Optimize FSRS parameters", f"These parameters can't be used: {e}")
                return
            self._on_cards_changed()

    def _reset_parameters(self) -> None:
        if QMessageBox.question(self, "FSRS parameters", "Go back to FSRS's default parameters?") \
                == QMessageBox.StandardButton.Yes:
            srs.set_parameters(self.db, None)
            self._on_cards_changed()

    def _set_new_per_day(self) -> None:
        n, ok = QInputDialog.getInt(
            self, "New review cards per day", "Cards never reviewed before, introduced per day across all books:",
            srs.new_per_day(self.db), 0, 9999,
        )
        if ok:
            self.db.set_setting(srs.NEW_PER_DAY_SETTING, str(n))
            self._on_cards_changed()

    def _set_rewards(self, on: bool) -> None:
        self.db.set_setting(gamify.ENABLED_SETTING, "1" if on else "0")
        self._on_cards_changed()

    def _set_variants(self, on: bool) -> None:
        self.db.set_setting(variants.ENABLED_SETTING, "1" if on else "0")
        self._on_cards_changed()

    def _set_daily_goal(self) -> None:
        n, ok = QInputDialog.getInt(
            self, "Daily goal", "Reviews a day that meet the daily goal (reviewing everything due meets it too):",
            gamify.daily_goal(self.db), 1, 9999,
        )
        if ok:
            self.db.set_setting(gamify.GOAL_SETTING, str(n))
            self._on_cards_changed()

    def _show_chat(self, then) -> None:
        if not self.chat_action.isChecked():
            self.chat_action.trigger()
        then()

    def _show_notes(self, then) -> None:
        if not self.notes_action.isChecked():
            self.notes_action.trigger()
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
        if self.book_id is not None and self.center.currentWidget() is self.book_view:
            self.db.update_book(self.book_id, last_page=self.reader.current_page(), zoom=self.reader.saved_zoom())

    # -- reading aloud ----------------------------------------------------------------
    def render_on_colab(self) -> None:
        if self.book_id is None:
            QMessageBox.information(self, "Render audio on Colab", "Open a book first.")
            return
        voice = self.db.get_setting("tts_voice:kokoro", "af_heart")
        RenderDialog(self.db, self.db.book(self.book_id), voice, self.read_aloud.speed, self.colab, self).exec()

    def _on_audio_synced(self, file_hash: str, copied: int) -> None:
        if copied > 0 and self.book_id is not None and self.db.book(self.book_id)["hash"] == file_hash:
            self.read_aloud.set_audio(local_audio(file_hash))

    def _toggle_reading(self, on: bool) -> None:
        if on:
            self.read_aloud.start_from_view()
            if not self.read_aloud.active:
                self.read_action.setChecked(False)
        else:
            self.read_aloud.stop()

    def _on_reading(self, on: bool) -> None:
        self.read_action.blockSignals(True)
        self.read_action.setChecked(on)
        self.read_action.blockSignals(False)
        for a in (self.stop_reading_action, self.prev_sentence_action, self.next_sentence_action):
            a.setEnabled(on)

    def closeEvent(self, event) -> None:
        self.read_aloud.shutdown()
        self._save_position()
        if self.browser is not None:
            self.browser.save_note()
        self.phone_sync.sync()  # what was reviewed here, for the phone
        self.worker.shutdown()
        super().closeEvent(event)


class _Page(QWidget):
    """A center page whose content is made when it is first shown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("library")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def set_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)


class _Optimizer(QObject):
    """Fits FSRS parameters on a thread (PyTorch can take a while); finished(weights, error)."""

    finished = Signal(list, str)

    def __init__(self, logs: list, parent=None):
        super().__init__(parent)
        self.logs = logs

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="fsrs-optimizer").start()

    def _run(self) -> None:
        try:
            self.finished.emit(srs.fit(self.logs), "")
        except Exception as e:  # noqa: BLE001 - reported to the user
            self.finished.emit([], str(e) or type(e).__name__)


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
