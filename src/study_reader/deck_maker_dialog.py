"""Make with Claude in the app: DeckMaker sends the draft's requests through the chat worker
(deck_maker.py builds and reads them), and MakeWindow is where a deck is described or cards are
picked to improve, and where the draft is looked over, refined and added."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import cardmodel
from . import deck_maker as dm
from .chat_service import ChatWorker
from .db import Database, deck_name
from .theme import icon

FILE_FILTER = ("Material (*.pdf *.epub *.txt *.md *.markdown *.tex *.rst *.org *.csv *.tsv *.html *.htm "
               "*.png *.jpg *.jpeg *.gif *.webp);;All files (*)")
FOLLOW_UP_HINT = ("Ask Claude for changes, e.g. “more on X”, “make #4 harder”, “fewer cloze cards”, "
                  "“add worked problems”. Naming cards (#4, #12-15) sends it only to the requests holding them.")
ASK_OVER = 5  # a follow-up naming no cards asks first when it would go to more requests than this


class DeckMaker(QObject):
    """Runs the draft's requests and keeps the draft in step with Claude's replies.
    changed: the draft changed (a reply came, a card was ticked...); cardsChanged: applying it
    added or rewrote cards; aboutToChange: cards are about to change (editors should save first);
    showCards(query): the browser should show these cards (a search)."""

    changed = Signal()
    cardsChanged = Signal()
    aboutToChange = Signal()
    showCards = Signal(str)

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._out: set[int] = set()  # batches waiting for Claude
        self._window: MakeWindow | None = None
        worker.made.connect(self._on_made)
        worker.makeFailed.connect(self._on_failed)
        draft = db.draft()
        if draft is not None:  # the app closed while Claude was working
            for b in db.draft_batches(draft["id"]):
                if b["status"] == "pending":
                    dm.fail(db, b["id"], "interrupted (the app was closed); retry it")

    # -- the draft ------------------------------------------------------------------------
    def draft(self):
        return self.db.draft()

    def busy(self) -> bool:
        return bool(self._out)

    def start_new(self, description: str, notes: str = "", files=(), parent: str = "", model: str = dm.MODEL) -> None:
        self._abandon()
        draft_id, batch_id = dm.start_new(self.db, description, notes, files, parent, model)
        self._send(batch_id, dm.first_blocks(self.db, self.db.draft(), batch_id), None, None)
        self.changed.emit()

    def start_improve(self, card_ids, instructions: str = "", model: str = dm.MODEL) -> int:
        """Returns how many requests went out."""
        self._abandon()
        draft_id, batch_ids = dm.start_improve(self.db, card_ids, instructions, model)
        draft = self.db.draft()
        for batch_id in batch_ids:
            self._send(batch_id, dm.first_blocks(self.db, draft, batch_id), None, None)
        self.changed.emit()
        return len(batch_ids)

    def follow_up_targets(self, feedback: str) -> tuple[list[int], bool]:
        """The requests a follow-up would go to (dm.follow_up_targets, less those still out), and
        whether it names cards."""
        draft = self.db.draft()
        if draft is None:
            return [], False
        ids, named = dm.follow_up_targets(self.db, draft, feedback)
        return [i for i in ids if i not in self._out], named

    def follow_up(self, feedback: str) -> int:
        """Send the learner's changes to the requests holding the cards it names, or to every
        request of the draft when it names none; returns how many went out."""
        draft = self.db.draft()
        if draft is None or not feedback.strip():
            return 0
        n = 0
        for batch_id in self.follow_up_targets(feedback)[0]:
            b = self.db.draft_batch(batch_id)
            adding = dm.is_first_batch(self.db, draft, b["id"])
            blocks = dm.follow_up_blocks(self.db, draft, b["id"], feedback, adding)
            self._send(b["id"], blocks, dm.first_blocks(self.db, draft, b["id"]) + blocks, b["session_id"])
            n += 1
        self.changed.emit()
        return n

    def retry(self) -> int:
        """Send again the first requests that failed (follow-ups that failed are just sent again)."""
        draft = self.db.draft()
        if draft is None:
            return 0
        n = 0
        for b in self.db.draft_batches(draft["id"]):
            if b["status"] != "error" or b["session_id"] or b["id"] in self._out:
                continue
            for item in self.db.draft_items(draft["id"], b["id"]):
                if item["status"] == "error" and item["card_id"] is not None:
                    self.db.update_draft_item(item["id"], status="pending", reason="")
            self._send(b["id"], dm.first_blocks(self.db, draft, b["id"]), None, None)
            n += 1
        self.changed.emit()
        return n

    def can_retry(self) -> bool:
        draft = self.db.draft()
        return draft is not None and any(b["status"] == "error" and not b["session_id"] and b["id"] not in self._out
                                         for b in self.db.draft_batches(draft["id"]))

    def stop(self) -> None:
        """Cancel the requests still out; what Claude already proposed stays."""
        self.worker.stop_making()
        for batch_id in self._out:
            dm.fail(self.db, batch_id, "stopped")
        self._out.clear()
        self.changed.emit()

    def tick(self, item_id: int, on: bool) -> None:
        self.db.update_draft_item(item_id, checked=int(on))
        self.changed.emit()

    def tick_all(self, on: bool) -> None:
        draft = self.db.draft()
        if draft is not None:
            self.db.set_draft_items_checked(draft["id"], on)
            self.changed.emit()

    def set_deck(self, name: str) -> None:
        draft = self.db.draft()
        if draft is not None and deck_name(name) != draft["deck"]:
            self.db.update_draft(draft["id"], deck=deck_name(name))

    def discard(self) -> None:
        self._abandon()
        self.changed.emit()

    def _abandon(self) -> None:
        if self._out:
            self.worker.stop_making()
            self._out.clear()
        draft = self.db.draft()
        if draft is not None:
            self.db.delete_draft(draft["id"])

    def apply(self) -> dict:
        draft = self.db.draft()
        if draft is None:
            return {"added": [], "rewritten": [], "deck": None}
        if self._out:
            self.worker.stop_making()
            self._out.clear()
        self.aboutToChange.emit()
        result = dm.apply(self.db, draft["id"])
        result["deck_name"] = draft["deck"]
        if result["added"] or result["rewritten"]:
            self.cardsChanged.emit()
        self.changed.emit()
        return result

    def undo_rewrite(self, card_id: int) -> bool:
        self.aboutToChange.emit()
        if dm.undo_rewrite(self.db, card_id):
            self.cardsChanged.emit()
            return True
        return False

    # -- requests -------------------------------------------------------------------------
    def _send(self, batch_id: int, blocks, fresh, session_id) -> None:
        draft = self.db.draft()
        self.db.update_draft_batch(batch_id, status="pending", error="")
        self._out.add(batch_id)
        self.worker.submit_make(batch_id, blocks, fresh, session_id, draft["model"] or dm.MODEL)

    def _on_made(self, batch_id: int, session_id: str, text: str) -> None:
        if batch_id in self._out:
            self._out.discard(batch_id)
            dm.record(self.db, batch_id, session_id, text)
            self.changed.emit()

    def _on_failed(self, batch_id: int, message: str) -> None:
        if batch_id in self._out:
            self._out.discard(batch_id)
            dm.fail(self.db, batch_id, message)
            self.changed.emit()

    # -- the window -----------------------------------------------------------------------
    def window(self, parent: QWidget | None = None) -> MakeWindow:
        if self._window is None:
            self._window = MakeWindow(self, parent)
        return self._window

    def show_draft(self, parent: QWidget | None = None) -> None:
        self.window(parent).present()

    def make_deck(self, parent: QWidget | None = None, parent_deck: str = "") -> None:
        """Describe a new deck (under parent_deck, if given), or carry on with the draft there is."""
        w = self.window(parent)
        if self._keep_draft(w):
            w.present()
            return
        w.new_form(parent_deck)

    def improve(self, card_ids, parent: QWidget | None = None) -> None:
        """Improve these cards (asking first about a draft there is)."""
        card_ids = list(dict.fromkeys(card_ids))
        if not card_ids:
            return
        w = self.window(parent)
        if self._keep_draft(w):
            w.present()
            return
        w.improve_form(card_ids)

    def _keep_draft(self, w: MakeWindow) -> bool:
        """With a draft under way, ask whether to go on with it (True) or start over."""
        draft = self.db.draft()
        if draft is None:
            return False
        n = len(self.db.draft_items(draft["id"]))
        box = QMessageBox(w if w.isVisible() else w.parentWidget())
        box.setWindowTitle("Make with Claude")
        box.setText(f"You have a draft from Claude that isn't added yet ({n} card{'s' * (n != 1)}).")
        box.setInformativeText("Go on with it, or discard it and start something new?")
        go_on = box.addButton("Go on with the draft", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard it", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(go_on)
        box.exec()
        if box.clickedButton() is go_on:
            return True
        self.discard()
        return False


class _Bridge(QObject):
    def __init__(self, window: MakeWindow):
        super().__init__(window)
        self.window = window

    @Slot(int, bool)
    def tick(self, item_id: int, on: bool) -> None:
        self.window.maker.tick(item_id, on)


class MakeWindow(QWidget):
    """The request (a deck's description, or cards to improve), then the draft: Claude's cards,
    each ticked or not, the deck they go into, follow-ups, and Add."""

    def __init__(self, maker: DeckMaker, parent=None):
        super().__init__(parent)
        from .browser import CardView
        from .card_editor import DeckBox
        self.maker = maker
        self.db = maker.db
        self.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle("Make with Claude — Study Reader")
        self.resize(1050, 860)
        self._card_ids: list[int] = []
        self._last_feedback = ""
        self.pages = QStackedWidget()

        # -- a new deck -----------------------------------------------------------------
        new = QWidget()
        intro = QLabel("Describe the deck you want, and Claude writes its cards: flashcards, cloze, multiple choice, "
                       "numeric problems and so on, whichever fits each idea. You look them over, ask for changes, "
                       "and add the ones you want. Give Claude material to draw on by pasting notes or attaching "
                       "files (PDF, EPUB, text or images).")
        intro.setWordWrap(True)
        self.description = QPlainTextEdit(objectName="fieldEdit")
        self.description.setPlaceholderText(
            "What should the deck cover? e.g. “The Krebs cycle for an intro biochemistry exam: each step's enzyme, "
            "what goes in and out, and why it matters”, or “Spanish verbs in the preterite, with irregular ones”")
        self.notes = QPlainTextEdit(objectName="fieldEdit")
        self.notes.setPlaceholderText("Optional: notes, a syllabus or any text for Claude to draw the cards from")
        self.files = QListWidget()  # shown once there are files
        self.files.setMaximumHeight(90)
        self.files.setVisible(False)
        attach = QPushButton("Attach files…")
        attach.setToolTip("PDFs, EPUBs and text files are read; images are shown to Claude")
        attach.clicked.connect(self._attach)
        self.remove_file = QPushButton("Remove file")
        self.remove_file.setVisible(False)
        self.remove_file.clicked.connect(self._remove_files)
        file_row = QHBoxLayout()
        file_row.addWidget(attach)
        file_row.addWidget(self.remove_file)
        file_row.addStretch()
        self.parent_deck = DeckBox(self.db)
        self.parent_deck.lineEdit().setPlaceholderText("(none: a deck of its own)")
        self.new_model = self._models()
        generate = QPushButton("Make the deck")
        generate.setProperty("primary", True)
        generate.setIcon(icon("sparkles"))
        generate.setToolTip("Ctrl+Enter")
        generate.clicked.connect(self._generate)
        QShortcut(QKeySequence("Ctrl+Return"), new, self._generate, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        form = QFormLayout()
        form.addRow("Under the deck", self.parent_deck)
        form.addRow("Model", self.new_model)
        lay = QVBoxLayout(new)
        lay.addWidget(intro)
        lay.addWidget(QLabel("What do you want to learn?"))
        lay.addWidget(self.description, 2)
        lay.addWidget(QLabel("Material (optional)"))
        lay.addWidget(self.notes, 2)
        lay.addWidget(self.files)
        lay.addLayout(file_row)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(generate)
        lay.addLayout(row)
        self.pages.addWidget(new)

        # -- improving cards ------------------------------------------------------------
        improve = QWidget()
        self.improve_intro = QLabel()
        self.improve_intro.setWordWrap(True)
        self.instructions = QPlainTextEdit(objectName="fieldEdit")
        self.instructions.setPlaceholderText("Optional instructions for Claude, e.g. “prefer multiple choice”, "
                                             "“keep the cloze cards”, “these are for an exam on chapter 3”")
        self.improve_model = self._models()
        self.improve_warning = QLabel()
        self.improve_warning.setWordWrap(True)
        self.improve_warning.setProperty("role", "muted")
        self.improve_button = QPushButton("Improve")
        self.improve_button.setProperty("primary", True)
        self.improve_button.setIcon(icon("sparkles"))
        self.improve_button.clicked.connect(self._improve)
        lay = QVBoxLayout(improve)
        lay.addWidget(self.improve_intro)
        lay.addWidget(self.instructions, 1)
        form = QFormLayout()
        form.addRow("Model", self.improve_model)
        lay.addLayout(form)
        lay.addWidget(self.improve_warning)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.improve_button)
        lay.addLayout(row)
        self.pages.addWidget(improve)

        # -- the draft ------------------------------------------------------------------
        draft = QWidget()
        self.summary = QLabel()
        self.summary.setProperty("role", "muted")
        self.summary.setWordWrap(True)
        self.deck = DeckBox(self.db)
        self.deck.currentTextChanged.connect(lambda text: self.maker.set_deck(text) if text.strip() else None)
        tick = QPushButton("Tick all")
        tick.clicked.connect(lambda: self.maker.tick_all(True))
        untick = QPushButton("Untick all")
        untick.clicked.connect(lambda: self.maker.tick_all(False))
        top = QHBoxLayout()
        self.deck_label = QLabel("New cards go into")
        top.addWidget(self.deck_label)
        top.addWidget(self.deck, 1)
        top.addStretch(0)  # takes the room while the deck is hidden (improving cards adds none)
        for b in (tick, untick):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            top.addWidget(b)
        self.view = CardView(bridge=_Bridge(self))
        self.feedback = QPlainTextEdit(objectName="fieldEdit")
        self.feedback.setPlaceholderText(FOLLOW_UP_HINT)
        self.feedback.setMaximumHeight(70)
        self.send = QPushButton("Ask Claude")
        self.send.setToolTip("Send your changes to Claude (Ctrl+Enter)")
        self.send.clicked.connect(self._follow_up)
        QShortcut(QKeySequence("Ctrl+Return"), self.feedback, self._follow_up, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        ask = QHBoxLayout()
        ask.addWidget(self.feedback, 1)
        ask.addWidget(self.send, 0, Qt.AlignmentFlag.AlignBottom)
        self.discard_button = QPushButton("Discard draft")
        self.discard_button.setIcon(icon("trash", "muted"))
        self.discard_button.clicked.connect(self._discard)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Cancel the requests still waiting for Claude; what it proposed stays")
        self.stop_button.clicked.connect(self.maker.stop)
        self.retry_button = QPushButton("Retry failed")
        self.retry_button.clicked.connect(self.maker.retry)
        self.apply_button = QPushButton("Add")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self._apply)
        bottom = QHBoxLayout()
        bottom.addWidget(self.discard_button)
        bottom.addStretch()
        bottom.addWidget(self.stop_button)
        bottom.addWidget(self.retry_button)
        bottom.addWidget(self.apply_button)
        lay = QVBoxLayout(draft)
        lay.addWidget(self.summary)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        lay.addLayout(ask)
        lay.addLayout(bottom)
        self.pages.addWidget(draft)

        # -- done -----------------------------------------------------------------------
        done = QWidget()
        self.done_label = QLabel()
        self.done_label.setWordWrap(True)
        self.done_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.show_button = QPushButton("Show them in the card browser")
        self.show_button.clicked.connect(self._show_cards)
        another = QPushButton("Make another deck")
        another.clicked.connect(lambda: self.new_form(""))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        for b in (self.show_button, another, close):
            buttons.addWidget(b)
        buttons.addStretch()
        lay = QVBoxLayout(done)
        lay.addStretch()
        lay.addWidget(self.done_label)
        lay.addLayout(buttons)
        lay.addStretch()
        self.pages.addWidget(done)
        self._done_query = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.addWidget(self.pages)
        # replies land every few seconds while improving many cards: redraw at most once a second
        self._throttle = QTimer(self, singleShot=True, interval=1000)
        self._throttle.timeout.connect(self._throttle_over)
        self._stale = False
        maker.changed.connect(self._changed)

    def _models(self) -> QComboBox:
        box = QComboBox()
        for label, value in dm.MODELS:
            box.addItem(label, value)
        i = box.findData(self.db.get_setting(dm.SETTING_MODEL, dm.MODEL))
        box.setCurrentIndex(max(i, 0))
        return box

    # -- pages ----------------------------------------------------------------------------
    def present(self) -> None:
        if self.maker.draft() is not None:
            self.pages.setCurrentIndex(2)
            self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def new_form(self, parent_deck: str = "") -> None:
        self.parent_deck.reload(parent_deck)
        self.description.clear()
        self.notes.clear()
        self.files.clear()
        self._files_shown()
        self.pages.setCurrentIndex(0)
        self.setWindowTitle("Make a deck with Claude — Study Reader")
        self.present()
        self.description.setFocus()

    def improve_form(self, card_ids: list[int]) -> None:
        self._card_ids = card_ids
        n = len(card_ids)
        self.improve_intro.setText(
            f"Claude will look at {n} card{'s' * (n != 1)} and propose a better type for each where one fits (a "
            "numeric problem, multiple choice, a sharper flashcard...), split cards that ask several things, or keep "
            "them as they are. Nothing changes until you add the proposals you tick; rewritten cards keep their "
            "schedule and history, and each can be undone from the card browser.")
        self.instructions.setPlainText(self.db.get_setting(dm.SETTING_INSTRUCTIONS, ""))
        requests = len(dm.batches(self.db, card_ids))
        self.improve_warning.setText(
            f"That's {requests} requests to Claude; they run a few at a time and count toward your plan's usage."
            if requests > 10 else "")
        self.improve_button.setText(f"Improve {n} card{'s' * (n != 1)}")
        self.pages.setCurrentIndex(1)
        self.setWindowTitle("Improve cards with Claude — Study Reader")
        self.present()

    # -- actions --------------------------------------------------------------------------
    def _attach(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach material", self.db.get_setting("last_dir", str(Path.home())),
                                                FILE_FILTER)
        have = {self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())}
        for path in paths:
            if path not in have:
                item = QListWidgetItem(Path(path).name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.files.addItem(item)
        self._files_shown()

    def _remove_files(self) -> None:
        for item in self.files.selectedItems() or [self.files.item(self.files.count() - 1)]:
            if item is not None:
                self.files.takeItem(self.files.row(item))
        self._files_shown()

    def _files_shown(self) -> None:
        self.files.setVisible(self.files.count() > 0)
        self.remove_file.setVisible(self.files.count() > 0)

    def _generate(self) -> None:
        description = self.description.toPlainText().strip()
        files = [self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]
        if not description:
            if not (self.notes.toPlainText().strip() or files):
                QMessageBox.information(self, "Make a deck", "Say what the deck should cover.")
                return
            description = "Make a deck from the material I gave."
        model = self.new_model.currentData()
        self.db.set_setting(dm.SETTING_MODEL, model)
        self.maker.start_new(description, self.notes.toPlainText(), files, self.parent_deck.currentText(), model)
        self.pages.setCurrentIndex(2)
        self.refresh()

    def _improve(self) -> None:
        text = self.instructions.toPlainText().strip()
        self.db.set_setting(dm.SETTING_INSTRUCTIONS, text)
        model = self.improve_model.currentData()
        self.db.set_setting(dm.SETTING_MODEL, model)
        self.maker.start_improve(self._card_ids, text, model)
        self.pages.setCurrentIndex(2)
        self.refresh()

    def _follow_up(self) -> None:
        text = self.feedback.toPlainText().strip()
        if not text or self.maker.busy():
            return
        targets, named = self.maker.follow_up_targets(text)
        if not named and len(targets) > ASK_OVER:
            box = QMessageBox(QMessageBox.Icon.Question, "Ask Claude",
                              f"This goes to all {len(targets)} requests in the draft (one per group of cards), and "
                              "each counts toward your plan's usage.\n\nTo reach only some cards, name them, e.g. "
                              "“make #4 and #12-15 harder”: then it goes only to the requests holding them.",
                              parent=self)
            send = box.addButton(f"Send to all {len(targets)}", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not send:
                return
        if self.maker.follow_up(text):
            self._last_feedback = text
            self.feedback.clear()

    def _discard(self) -> None:
        if QMessageBox.question(self, "Discard draft", "Discard Claude's draft? Nothing from it is added.") \
                == QMessageBox.StandardButton.Yes:
            self.maker.discard()
            self.close()

    def _apply(self) -> None:
        self.maker.set_deck(self.deck.currentText())
        r = self.maker.apply()
        added, rewritten = len(r["added"]), len(r["rewritten"])
        parts = []
        if added:
            parts.append(f"Added {added} card{'s' * (added != 1)}" + (f" to {r['deck_name']}" if r["deck"] else ""))
        if rewritten:
            parts.append(f"rewrote {rewritten} card{'s' * (rewritten != 1)}" if parts
                         else f"Rewrote {rewritten} card{'s' * (rewritten != 1)}")
        self.done_label.setText((" and ".join(parts) + ".") if parts else "Nothing was ticked, so nothing changed.")
        ids = r["added"] + r["rewritten"]
        self._done_query = f"cid:{','.join(map(str, ids))}" if ids else ""
        self.show_button.setVisible(bool(ids))
        self.pages.setCurrentIndex(3)

    def _show_cards(self) -> None:
        if self._done_query:
            self.maker.showCards.emit(self._done_query)
            self.close()

    # -- the draft ------------------------------------------------------------------------
    def _changed(self) -> None:
        if self.maker.draft() is None and self.pages.currentIndex() == 2:
            self.new_form("")
            return
        if self._throttle.isActive():
            self._stale = True
        else:
            self.refresh()
            self._throttle.start()

    def _throttle_over(self) -> None:
        if self._stale:
            self._stale = False
            self.refresh()
            self._throttle.start()

    def refresh(self) -> None:
        draft = self.maker.draft()
        if draft is None or self.pages.currentIndex() != 2:
            return
        busy = self.maker.busy()
        items = self.db.draft_items(draft["id"])
        batches = self.db.draft_batches(draft["id"])
        c = dm.counts(self.db, draft["id"])
        self.view.js("showDraft", {"busy": busy, "note": "\n\n".join(b["note"] for b in batches if b["note"]),
                                   "items": [self._item(i) for i in items]})
        if draft["mode"] == "new":
            request = json.loads(draft["request"] or "{}")
            about = " ".join(request.get("description", "").split())
            head = f"A new deck: “{about[:160]}{'…' if len(about) > 160 else ''}”"
        else:
            head = f"Improving {sum(1 for i in items if i['card_id'] is not None)} cards"
        parts = [f"{c[s]} {label}" for s, label in (("pending", "waiting"), ("proposed", "proposed"),
                                                    ("kept", "kept as they are"), ("error", "failed")) if c.get(s)]
        errors = [b["error"] for b in batches if b["status"] == "error" and b["error"]]
        status = "Claude is working…" if busy else ""
        text = " · ".join(p for p in [head, status, *parts] if p)
        if errors and not busy:
            text += f"\nLast error: {errors[-1]}"
        self.summary.setText(text)
        if not self.deck.hasFocus():
            self.deck.reload(draft["deck"])
        has_new = draft["mode"] == "new" or any(i["card_id"] is None for i in items)
        self.deck.setVisible(has_new)
        self.deck_label.setVisible(has_new)
        self.stop_button.setVisible(busy)
        self.retry_button.setVisible(self.maker.can_retry())
        self.send.setEnabled(not busy and any(b["session_id"] for b in batches))
        new_n, rewrites = c.get("new_cards", 0), c.get("rewrites", 0)
        label = " and ".join(p for p in (f"add {new_n} card{'s' * (new_n != 1)}" if new_n else "",
                                         f"apply {rewrites} rewrite{'s' * (rewrites != 1)}" if rewrites else "") if p)
        self.apply_button.setText(label[:1].upper() + label[1:] if label else "Add")
        self.apply_button.setEnabled(bool(label))
        if not busy:
            if errors and self._last_feedback and not self.feedback.toPlainText().strip():
                self.feedback.setPlainText(self._last_feedback)  # a follow-up that failed, to send again
            self._last_feedback = ""

    def _item(self, item) -> dict:
        out = {"id": item["id"], "number": item["number"], "status": item["status"], "checked": bool(item["checked"]),
               "reason": item["reason"], "now": None, "kind": "", "cards": []}
        if item["card_id"] is not None:
            card = self.db.card(item["card_id"])
            note = self.db.card_note(card["note_id"]) if card else None
            if card is not None:
                out["now"] = json.loads(card["data"])
                out["kind"] = note["kind"] if note else out["now"].get("type", "")
        for spec in dm.specs(item["proposal"]):
            try:
                out["cards"].append({"kind": spec["kind"], "questions": dm.previews(spec)})
            except cardmodel.NoteError:
                continue
        return out

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def closeEvent(self, event) -> None:
        draft = self.maker.draft()
        if draft is not None:
            self.maker.set_deck(self.deck.currentText())
        super().closeEvent(event)
