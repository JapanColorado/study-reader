"""The Migrate from Anki dialog: pick a profile and the decks to bring over (anki_import.py does
the work, from a copy of the collection; Anki is never changed)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QProgressDialog, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import anki_import
from .db import Database

DECKS_SETTING = "anki_import_decks"  # the decks picked last time (Anki ids)
NAME_ROLE = Qt.ItemDataRole.UserRole + 1  # a deck item's full name


class AnkiImportDialog(QDialog):
    """Accepted once a migration has run; report and query (a browser search for the migrated
    cards, '' if they shouldn't be shown) then say what happened."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.collection: anki_import.Collection | None = None
        self.report: anki_import.Report | None = None
        self.query = ""
        self.setWindowTitle("Migrate from Anki")
        self.resize(760, 720)

        intro = QLabel(
            "Brings decks over from Anki: their cards (as flashcards, flashcards both ways, cloze and image occlusion), tags, "
            "each card's schedule and review history, images, and Anki's FSRS parameters. Anki itself isn't changed "
            "(the app reads a copy), so delete or suspend the decks there yourself once you're happy. Running it again "
            "skips cards already brought over.")
        intro.setWordWrap(True)
        self.profile = QComboBox()
        for name, path in anki_import.find_collections():
            self.profile.addItem(f"{name}  —  {path}", str(path))
        self.profile.currentIndexChanged.connect(lambda: self._open(self.profile.currentData()))
        choose = QPushButton("Choose file…")
        choose.clicked.connect(self._choose)
        row = QHBoxLayout()
        row.addWidget(QLabel("Profile"))
        row.addWidget(self.profile, 1)
        row.addWidget(choose)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Deck", "Cards", "With subdecks", "Already here"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3):
            self.tree.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        all_ = QPushButton("Select all")
        all_.clicked.connect(lambda: self._check_all(True))
        none = QPushButton("Select none")
        none.clicked.connect(lambda: self._check_all(False))
        self.picked = QLabel()
        self.picked.setProperty("role", "muted")
        picks = QHBoxLayout()
        picks.addWidget(all_)
        picks.addWidget(none)
        picks.addWidget(self.picked, 1)

        self.scheduling = QCheckBox("Keep each card's schedule and review history")
        self.scheduling.setChecked(True)
        self.parameters = QCheckBox("Use Anki's FSRS parameters and desired retention")
        self.parameters.setChecked(True)
        self.prefix = QLineEdit(placeholderText="optional, e.g. Anki — decks keep their names otherwise")
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Put the decks under"))
        prefix_row.addWidget(self.prefix, 1)
        self.open_browser = QCheckBox("Show the migrated cards in the card browser afterwards")
        self.open_browser.setChecked(True)

        self.migrate_button = QPushButton("Migrate")
        self.migrate_button.setProperty("primary", True)
        self.migrate_button.clicked.connect(self.migrate)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close)
        buttons.addWidget(self.migrate_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self.tree, 1)
        layout.addLayout(picks)
        layout.addWidget(self.scheduling)
        layout.addWidget(self.parameters)
        layout.addLayout(prefix_row)
        layout.addWidget(self.open_browser)
        layout.addLayout(buttons)
        if self.profile.count():
            self._open(self.profile.currentData())
        else:
            self.picked.setText("No Anki profile found; choose a collection.anki2 file.")
            self.migrate_button.setEnabled(False)

    # -- the collection --------------------------------------------------------------------
    def _choose(self) -> None:
        start = str(Path.home() / ".local/share/Anki2")
        path, _ = QFileDialog.getOpenFileName(self, "Anki collection", start, "Anki collection (collection.anki2 *.anki2)")
        if path:
            self.profile.addItem(path, path)
            self.profile.setCurrentIndex(self.profile.count() - 1)

    def _open(self, path: str | None) -> None:
        if self.collection is not None:
            self.collection.close()
            self.collection = None
        self.tree.clear()
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.collection = anki_import.Collection(Path(path))
            decks = self.collection.decks()
        except Exception as e:  # noqa: BLE001 - any unreadable file
            self.picked.setText(f"Can't read this collection: {e}")
            self.migrate_button.setEnabled(False)
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.parameters.setEnabled(self.collection.fsrs_enabled())
        if not self.collection.fsrs_enabled():
            self.parameters.setChecked(False)
            self.parameters.setToolTip("FSRS is off in this Anki collection")
        here = self._already_here()
        previous = set(self.db.get_setting(DECKS_SETTING, "").split())
        items: dict[str, QTreeWidgetItem] = {}
        self.tree.blockSignals(True)
        totals: dict[str, int] = {}
        for d in decks:
            parts = d.name.split("::")
            for i in range(1, len(parts) + 1):
                totals["::".join(parts[:i])] = totals.get("::".join(parts[:i]), 0) + d.cards
        for d in decks:
            parent_name, _, leaf = d.name.rpartition("::")
            parent = items.get(parent_name)
            item = QTreeWidgetItem([leaf, str(d.cards or ""), str(totals.get(d.name, 0) or ""), str(here.get(d.id, "") or "")])
            item.setData(0, Qt.ItemDataRole.UserRole, d.id)
            item.setData(0, NAME_ROLE, d.name)
            item.setData(1, Qt.ItemDataRole.UserRole, d.cards)
            item.setToolTip(0, d.name + (f"\nNote types: {', '.join(d.note_types)}" if d.note_types else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if str(d.id) in previous else Qt.CheckState.Unchecked)
            for col in (1, 2, 3):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
            items[d.name] = item
        self.tree.blockSignals(False)
        self._update_picked()

    def _already_here(self) -> dict[int, int]:
        """Cards of each Anki deck that are in the app already."""
        if self.collection is None:
            return {}
        ours = {r[0] for r in self.db.conn.execute("SELECT anki_id FROM cards WHERE anki_id IS NOT NULL")}
        if not ours:
            return {}
        out: dict[int, int] = {}
        for card_id, deck in self.collection.conn.execute(
                "SELECT id, CASE WHEN odid THEN odid ELSE did END FROM cards"):
            if card_id in ours:
                out[deck] = out.get(deck, 0) + 1
        return out

    # -- picking decks ---------------------------------------------------------------------
    def _items(self, under: QTreeWidgetItem | None = None):
        """Every deck's item, or those under one."""
        stack = [under.child(i) for i in range(under.childCount())] if under else \
            [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack += [item.child(i) for i in range(item.childCount())]

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        state = item.checkState(0)
        self.tree.blockSignals(True)
        for child in self._items(item):  # a deck's subdecks go with it
            child.setCheckState(0, state)
        self.tree.blockSignals(False)
        self._update_picked()

    def _check_all(self, on: bool) -> None:
        self.tree.blockSignals(True)
        for item in self._items():
            item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_picked()

    def chosen(self) -> list[int]:
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self._items()
                if item.checkState(0) == Qt.CheckState.Checked]

    def check_decks(self, names) -> None:
        """Tick decks by full name (and their subdecks)."""
        wanted = set(names)
        for item in self._items():
            if item.data(0, NAME_ROLE) in wanted:
                item.setCheckState(0, Qt.CheckState.Checked)

    def _update_picked(self) -> None:
        decks = [i for i in self._items() if i.checkState(0) == Qt.CheckState.Checked]
        cards = sum(i.data(1, Qt.ItemDataRole.UserRole) or 0 for i in decks)
        self.picked.setText(f"{len(decks)} deck{'s' * (len(decks) != 1)}, {cards} card{'s' * (cards != 1)} picked")
        self.migrate_button.setEnabled(self.collection is not None and bool(decks))

    def _root_names(self) -> list[str]:
        """The outermost picked decks' full names, for a search that finds what was migrated."""
        out = []
        for item in self._items():
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            parent = item.parent()
            if parent is None or parent.checkState(0) != Qt.CheckState.Checked:
                out.append(item.data(0, NAME_ROLE))
        return out

    # -- migrating -------------------------------------------------------------------------
    def migrate(self) -> None:
        ids = self.chosen()
        if not ids or self.collection is None:
            return
        progress = QProgressDialog("Reading Anki…", None, 0, 100, self)
        progress.setWindowTitle("Migrate from Anki")
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumWidth(420)

        def step(done: int, total: int, text: str) -> None:
            progress.setMaximum(max(total, 1))
            progress.setValue(done)
            progress.setLabelText(text)
            QApplication.processEvents()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = anki_import.migrate(self.db, self.collection, ids, scheduling=self.scheduling.isChecked(),
                                         parameters=self.parameters.isChecked() and self.parameters.isEnabled(),
                                         prefix=self.prefix.text().strip(), progress=step)
        except Exception as e:  # noqa: BLE001 - report anything to the user rather than crash
            QApplication.restoreOverrideCursor()
            progress.close()
            QMessageBox.warning(self, "Migrate from Anki", f"The migration stopped: {e}")
            return
        QApplication.restoreOverrideCursor()
        progress.close()
        self.db.set_setting(DECKS_SETTING, " ".join(str(i) for i in ids))
        prefix = self.prefix.text().strip()
        roots = [f"{prefix}::{n}" if prefix else n for n in self._root_names()]
        query = " OR ".join('deck:"%s"' % n.replace("\\", "\\\\").replace('"', '\\"') for n in roots)
        QMessageBox.information(self, "Migrate from Anki", summary(report))
        self.report, self.query = report, query if self.open_browser.isChecked() else ""
        self.accept()  # the dialog is modal: it must close before the browser can be used

    def done(self, result: int) -> None:
        if self.collection is not None:
            self.collection.close()
            self.collection = None
        super().done(result)


def summary(report: anki_import.Report) -> str:
    lines = [f"Brought over {report.cards} card{'s' * (report.cards != 1)} from {report.notes} "
             f"Anki note{'s' * (report.notes != 1)}, with {report.reviews} review{'s' * (report.reviews != 1)} "
             f"and {report.media_copied} image{'s' * (report.media_copied != 1)}."]
    if report.skipped:
        lines.append(f"{report.skipped} card{'s were' if report.skipped != 1 else ' was'} already here and left as is.")
    if report.parameters:
        retention = f" and desired retention {report.retention:.2f}" if report.retention else ""
        lines.append(f"Using Anki's FSRS parameters{retention}.")
    if report.fallback_types:
        kinds = ", ".join(f"{name} ({n})" for name, n in report.fallback_types.items())
        lines.append(f"Anki note types brought over as flashcards from their fields: {kinds}.")
    if report.missing_media:
        shown = ", ".join(report.missing_media[:5]) + (" …" if len(report.missing_media) > 5 else "")
        lines.append(f"{len(report.missing_media)} image(s) weren't in Anki's media folder: {shown}")
    if report.errors:
        lines.append(f"{len(report.errors)} Anki note(s) couldn't be brought over: " + "; ".join(report.errors[:3]))
    return "\n\n".join(lines)
