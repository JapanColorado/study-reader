"""Claude Conversion in the app: sending batches of cards to Claude (convert.py builds and reads
them), and the window where the proposals are accepted or rejected."""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import convert
from .chat_service import ChatWorker
from .db import Database

MODELS = [("Sonnet (recommended)", "sonnet"), ("Opus", "opus"), ("Haiku (fastest)", "haiku")]
FILTERS = [("Waiting for you", ("pending", "proposed")), ("Proposed", ("proposed",)), ("Kept as is", ("kept",)),
           ("Errors", ("error",)), ("Everything open", ("pending", "proposed", "kept", "error")),
           ("Accepted", ("accepted",))]
INSTRUCTIONS_SETTING = "convert_instructions"
SHOWN = 300  # proposals shown at once


class ConversionManager(QObject):
    """Runs Claude Conversion requests through the chat worker and records the results.
    changed: a conversion started, finished or was decided."""

    changed = Signal()
    aboutToChange = Signal()  # cards are about to get new content (editors should save first)
    cardsChanged = Signal()  # an accepted proposal changed a card

    def __init__(self, db: Database, worker: ChatWorker, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._batches: dict[int, dict[int, int]] = {}  # batch id -> {card id: conversion id}
        self._next_batch = 1
        self._dialog: ConversionDialog | None = None
        worker.converted.connect(self._on_converted)
        worker.convertFailed.connect(self._on_failed)
        for c in db.conversions(("pending",)):  # the app closed while Claude was working
            db.update_conversion(c["id"], status="error", reason="interrupted (the app was closed); retry it")

    def start(self, card_ids, instructions: str = "", model: str = convert.MODEL) -> int:
        """Send cards to Claude in batches; returns how many requests went out."""
        batches = convert.batches(self.db, card_ids)
        for batch in batches:
            conversions = convert.start(self.db, batch)
            batch_id = self._next_batch
            self._next_batch += 1
            self._batches[batch_id] = conversions
            self.worker.submit_conversion(batch_id, convert.blocks(self.db, batch, instructions), model,
                                          convert.SYSTEM_PROMPT)
        self.changed.emit()
        return len(batches)

    def retry_errors(self, instructions: str = "", model: str = convert.MODEL) -> int:
        ids = [c["card_id"] for c in self.db.conversions(("error",))]
        return self.start(ids, instructions, model) if ids else 0

    def busy(self) -> bool:
        return bool(self._batches)

    def stop(self) -> None:
        """Cancel the requests still out; their cards are left as if never sent."""
        self.worker.stop_conversions()
        for conversions in self._batches.values():
            self.db.delete_conversions(self._still_pending(conversions).values())
        self._batches.clear()
        self.changed.emit()

    def _on_converted(self, batch_id: int, text: str) -> None:
        conversions = self._batches.pop(batch_id, None)
        if conversions is not None:
            convert.record(self.db, self._still_pending(conversions), text)
            self.changed.emit()

    def _on_failed(self, batch_id: int, message: str) -> None:
        conversions = self._batches.pop(batch_id, None)
        if conversions is not None:
            convert.fail(self.db, self._still_pending(conversions), message)
            self.changed.emit()

    def _still_pending(self, conversions: dict[int, int]) -> dict[int, int]:
        """Those not replaced by a newer conversion of the same card meanwhile."""
        out = {}
        for card_id, conversion_id in conversions.items():
            c = self.db.conversion(conversion_id)
            if c is not None and c["status"] == "pending":
                out[card_id] = conversion_id
        return out

    def decide(self, conversion_id: int, action: str) -> None:
        if action == "accept":
            self.aboutToChange.emit()
            if convert.accept(self.db, conversion_id):
                self.cardsChanged.emit()
        elif action in ("reject", "dismiss"):
            convert.reject(self.db, conversion_id)
        self.changed.emit()

    def accept_all(self) -> int:
        self.aboutToChange.emit()
        n = sum(convert.accept(self.db, c["id"]) for c in self.db.conversions(("proposed",)))
        if n:
            self.cardsChanged.emit()
        self.changed.emit()
        return n

    def reject_all(self, statuses=("proposed", "kept", "error")) -> None:
        for c in self.db.conversions(statuses):
            convert.reject(self.db, c["id"])
        self.changed.emit()

    def show_dialog(self, parent: QWidget | None = None) -> None:
        if self._dialog is None:
            self._dialog = ConversionDialog(self, parent)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        self._dialog.refresh()


class ConvertStartDialog(QDialog):
    """Confirm a conversion: how many cards, which model, anything to tell Claude."""

    def __init__(self, db: Database, count: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Convert with Claude")
        self.setMinimumWidth(520)
        intro = QLabel(
            f"Claude will look at {count} card{'s' * (count != 1)} and propose a better question type for each where "
            "one fits (e.g. a free response it grades, a numeric problem, or multiple choice), or keep it as is. "
            "Nothing changes until you accept a proposal; accepted cards keep their schedule and history.")
        intro.setWordWrap(True)
        self.model_box = QComboBox()
        for label, value in MODELS:
            self.model_box.addItem(label, value)
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("fieldEdit")
        self.notes.setPlaceholderText("Optional instructions for Claude, e.g. “prefer free responses”, "
                                      "“keep the cloze cards”, “these are for an exam on chapter 3”")
        self.notes.setPlainText(db.get_setting(INSTRUCTIONS_SETTING, ""))
        self.notes.setMaximumHeight(110)
        row = QHBoxLayout()
        row.addWidget(QLabel("Model"))
        row.addWidget(self.model_box)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        start = buttons.addButton("Convert", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setProperty("primary", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self.notes)
        if count > 200:
            warn = QLabel(f"That's about {(count + convert.BATCH_SIZE - 1) // convert.BATCH_SIZE} requests to Claude; "
                          "they run a few at a time and count toward your plan's usage.")
            warn.setWordWrap(True)
            warn.setProperty("role", "muted")
            layout.addWidget(warn)
        layout.addWidget(buttons)

    def instructions(self) -> str:
        text = self.notes.toPlainText().strip()
        self.db.set_setting(INSTRUCTIONS_SETTING, text)
        return text

    def model(self) -> str:
        return self.model_box.currentData()


class _Bridge(QObject):
    def __init__(self, dialog: ConversionDialog):
        super().__init__(dialog)
        self.dialog = dialog

    @Slot(int, str)
    def decide(self, conversion_id: int, action: str) -> None:
        self.dialog.manager.decide(conversion_id, action)


class ConversionDialog(QDialog):
    """Claude's proposals next to the cards as they are now; accept or reject each, or all."""

    def __init__(self, manager: ConversionManager, parent=None):
        super().__init__(parent)
        from .browser import CardView
        self.manager = manager
        self.db = manager.db
        self.setWindowTitle("Claude Conversion — Study Reader")
        self.setWindowFlag(Qt.WindowType.Window)
        self.resize(1100, 820)
        self.filter = QComboBox()
        for label, _ in FILTERS:
            self.filter.addItem(label)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.summary = QLabel()
        self.summary.setProperty("role", "muted")
        accept_all = QPushButton("Accept all proposed")
        accept_all.setProperty("primary", True)
        accept_all.clicked.connect(self.manager.accept_all)
        reject_all = QPushButton("Reject all proposed")
        reject_all.clicked.connect(lambda: self.manager.reject_all(("proposed",)))
        dismiss = QPushButton("Dismiss kept and errors")
        dismiss.clicked.connect(lambda: self.manager.reject_all(("kept", "error")))
        retry = QPushButton("Retry errors")
        retry.clicked.connect(lambda: self.manager.retry_errors(self.db.get_setting(INSTRUCTIONS_SETTING, "")))
        self.stop = QPushButton("Stop conversions")
        self.stop.setToolTip("Cancel the requests still waiting for Claude; finished proposals stay")
        self.stop.clicked.connect(self.manager.stop)
        top = QHBoxLayout()
        top.addWidget(QLabel("Show"))
        top.addWidget(self.filter)
        top.addWidget(self.summary, 1)
        for b in (self.stop, retry, dismiss, reject_all, accept_all):
            top.addWidget(b)
        self.view = CardView(_Bridge(self))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        # batches land every few seconds while converting; redraw at most once a second
        self._throttle = QTimer(self, singleShot=True, interval=1000)
        self._throttle.timeout.connect(self._throttle_over)
        self._stale = False
        manager.changed.connect(self._changed)

    def _changed(self) -> None:
        self.stop.setEnabled(self.manager.busy())
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
        self.stop.setEnabled(self.manager.busy())
        if not self.isVisible():
            return
        statuses = FILTERS[self.filter.currentIndex()][1]
        marks = ','.join('?' * len(statuses))
        rows = self.db.conn.execute(  # the latest SHOWN, those still converting last
            "SELECT * FROM (SELECT v.*, c.data AS card_data, d.name AS deck FROM conversions v "
            "JOIN cards c ON c.id=v.card_id LEFT JOIN decks d ON d.id=c.deck_id "
            f"WHERE v.status IN ({marks}) ORDER BY v.status='pending', v.id DESC LIMIT ?) "
            "ORDER BY status='pending', id",
            (*statuses, SHOWN),
        ).fetchall()
        total = self.db.conn.execute(f"SELECT COUNT(*) FROM conversions WHERE status IN ({marks})",
                                     tuple(statuses)).fetchone()[0]
        items = []
        for c in rows:
            accepted = c["status"] == "accepted"
            before = json.loads(c["original"])["data"] if accepted and c["original"] else json.loads(c["card_data"])
            after = json.loads(c["card_data"]) if accepted else json.loads(c["proposal"]) if c["proposal"] else None
            items.append({"id": c["id"], "status": c["status"], "deck": c["deck"] or "",
                          "reason": c["reason"], "before": before, "after": after})
        counts = self.db.conversion_counts()
        counts = {s: counts.get(s, 0) for s in ("pending", "proposed", "kept", "error")}
        parts = [f"{counts['pending']} converting" if counts["pending"] else "",
                 f"{counts['proposed']} proposed" if counts["proposed"] else "",
                 f"{counts['kept']} kept" if counts["kept"] else "", f"{counts['error']} errors" if counts["error"] else ""]
        if total > len(rows):
            parts.append(f"showing {len(rows)} of {total}")
        self.summary.setText(" · ".join(p for p in parts if p) or "Nothing waiting.")
        self.view.js("showProposals", items)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
