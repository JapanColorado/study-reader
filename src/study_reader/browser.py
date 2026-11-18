"""The question browser: every question in a searchable, sortable table (Anki-style search, see
search.py), a sidebar of decks, tags, books and saved searches, an editor and preview for the
current question, and actions on the selection (move, tag, suspend, flag, reschedule, delete, find
and replace, improve with Claude)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QEvent, QItemSelection, QItemSelectionModel, QModelIndex, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QColor, QFontMetrics, QKeySequence
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QTableView, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import question_model, quiz, search, practice
from .question_editor import AddDialog, QuestionEditor, DeckBox
from .db import Database, media_url
from .notes import LocalPage
from .theme import icon, theme

ASSETS = Path(__file__).parent / "assets"

COLUMNS = {
    "question": "Question", "answer": "Answer", "deck": "Deck", "due": "Due", "interval": "Interval",
    "stability": "Stability", "difficulty": "Difficulty", "retrievability": "Retrievability", "reps": "Attempts",
    "lapses": "Lapses", "type": "Type", "tags": "Tags", "book": "Book", "added": "Created",
    "modified": "Modified", "attempted": "Last attempt",
}
# Column ids saved by older versions (in the column, width and sort settings).
OLD_COLUMNS = {"reviewed": "attempted"}
DEFAULT_COLUMNS = ["question", "answer", "deck", "due", "interval", "retrievability", "added"]
COLUMNS_SETTING, SORT_SETTING, WIDTHS_SETTING = "browser_columns", "browser_sort", "browser_widths"
# Columns that share the table's width (weight, least and most width); the others are as wide
# as their header and the longest text they can hold (SAMPLES).
FLEX_COLUMNS = {"question": (3, 140, None), "answer": (2, 100, None), "deck": (1, 90, 240), "tags": (1, 90, 240),
                "book": (1, 90, 240)}
MIN_SECTION = 40
SAMPLES = {
    "due": ["(2026-12-31)"], "interval": ["11mo", "10.5y"], "stability": ["11mo", "10.5y"], "difficulty": ["100%"],
    "retrievability": ["100%"], "reps": ["9999"], "lapses": ["999"], "type": list(question_model.TYPE_LABELS.values()),
    "added": ["2026-12-31"], "modified": ["2026-12-31"],
    "attempted": ["2026-12-31"],
}
# Anki's flag colors.
FLAGS = {1: ("Red", "#ff6b6b"), 2: ("Orange", "#ffa94d"), 3: ("Green", "#51cf66"), 4: ("Blue", "#4dabf7"),
         5: ("Pink", "#f783ac"), 6: ("Turquoise", "#22b8cf"), 7: ("Purple", "#b197fc")}
SUSPENDED_COLOR, MARKED_COLOR = "#fcc419", "#9775fa"
ROW_SQL = (f"SELECT c.id, c.group_id, c.stage, c.due, c.last_attempt, c.stability, c.difficulty, c.reps, c.lapses, "
           f"c.suspended, c.flag, c.created_at, MAX(c.updated_at, n.updated_at) AS modified, c.data, c.book_id, c.page, "
           f"n.kind, n.sort_text, n.tags, d.name AS deck, b.title AS book FROM {search.FROM} WHERE c.id IN ")
CHUNK = 200


def _date(t: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else ""


def _schedule_text(row, key: str) -> str:
    """A question's interval, stability or difficulty as the table and Question info show it."""
    if key == "interval":
        return practice.format_interval(row["due"] - row["last_attempt"]) if row["stage"] and row["last_attempt"] else ""
    if key == "stability":
        return practice.format_interval(row["stability"] * 86400) if row["stability"] else ""
    return f"{(row['difficulty'] - 1) / 9 * 100:.0f}%" if row["difficulty"] else ""


def _derived(r: dict) -> dict:
    """A table row with its Answer and Type text worked out once, when it is read."""
    try:
        data = json.loads(r["data"])
    except ValueError:
        data = {}
    r["answer"] = question_model.answer_text(data)[:200] if data else ""
    r["type"] = question_model.TYPE_LABELS.get(r["kind"], r["kind"])
    return r


def _walk(items):
    """Tree items and everything under them."""
    stack = list(items)
    while stack:
        item = stack.pop()
        yield item
        stack += [item.child(i) for i in range(item.childCount())]


def _blend(color: str, base: str, amount: float) -> QColor:
    a, b = QColor(color), QColor(base)
    return QColor(round(a.red() * amount + b.red() * (1 - amount)), round(a.green() * amount + b.green() * (1 - amount)),
                  round(a.blue() * amount + b.blue() * (1 - amount)))


class QuestionTableModel(QAbstractTableModel):
    """The questions a search found, in order; rows are read from the database a chunk at a time."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.ids: list[int] = []
        self.columns: list[str] = list(DEFAULT_COLUMNS)
        self._rows: dict[int, dict] = {}
        self._now = time.time()
        self._decay = practice.decay(db)

    def set_ids(self, ids: list[int]) -> None:
        self.beginResetModel()
        self.ids = ids
        self._rows = {}
        self._now = time.time()
        self._decay = practice.decay(self.db)
        self.endResetModel()

    def set_columns(self, columns: list[str]) -> None:
        self.beginResetModel()
        self.columns = columns
        self.endResetModel()

    def invalidate(self, question_ids=None) -> None:
        """Re-read some rows (all if None) next time they're drawn."""
        if question_ids is None:
            self._rows = {}
        else:
            for i in question_ids:
                self._rows.pop(i, None)
        if self.ids:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.ids) - 1, len(self.columns) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.ids)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[self.columns[section]]
        return None

    def row(self, index: int) -> dict | None:
        if not 0 <= index < len(self.ids):
            return None
        question_id = self.ids[index]
        if question_id not in self._rows:
            start = max(index - CHUNK // 4, 0)
            chunk = [i for i in self.ids[start:start + CHUNK] if i not in self._rows] or [question_id]
            for r in self.db.conn.execute(ROW_SQL + f"({','.join('?' * len(chunk))})", chunk):
                self._rows[r["id"]] = _derived(dict(r))
            self._rows.setdefault(question_id, {})
        return self._rows[question_id] or None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        r = self.row(index.row()) if index.isValid() else None
        if r is None:
            return None
        key = self.columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(r, key)
        if role == Qt.ItemDataRole.ToolTipRole and key in ("question", "answer", "deck", "tags", "book"):
            return self._text(r, key)
        if role == Qt.ItemDataRole.BackgroundRole:
            c = theme().colors
            if r["flag"] in FLAGS:
                return _blend(FLAGS[r["flag"]][1], c["surface"], 0.28)
            if r["suspended"]:
                return _blend(SUSPENDED_COLOR, c["surface"], 0.22)
            if " marked " in r["tags"].lower():
                return _blend(MARKED_COLOR, c["surface"], 0.16)
        if role == Qt.ItemDataRole.ForegroundRole and r["suspended"] and key == "due":
            return QColor(theme().colors["muted"])
        return None

    def _text(self, r: dict, key: str) -> str:
        if key == "question":
            return r["sort_text"]
        if key in ("answer", "type", "deck"):
            return r[key]
        if key == "due":
            if r["stage"] == 0:
                text = "New"
            elif r["stage"] in (1, 3) and r["due"] - self._now < 86400:
                text = "now" if r["due"] <= self._now else time.strftime("%H:%M", time.localtime(r["due"]))
            else:
                text = _date(r["due"])
            return f"({text})" if r["suspended"] else text
        if key in ("interval", "stability", "difficulty"):
            return _schedule_text(r, key)
        if key == "retrievability":
            recall = practice.recall(r["stability"], r["last_attempt"], self._now, self._decay)
            return f"{recall * 100:.0f}%" if recall is not None else ""
        if key in ("reps", "lapses"):
            return str(r[key])
        if key == "tags":
            return r["tags"].strip()
        if key == "book":
            return r["book"] or ""
        if key == "added":
            return _date(r["created_at"])
        if key == "modified":
            return _date(r["modified"])
        if key == "attempted":
            return _date(r["last_attempt"])
        return ""


class Sidebar(QTreeWidget):
    """Saved searches, flags, decks, books, tags and question types; each item holds a search."""

    searchChosen = Signal(str, str)  # search, how: "" (replace) | "and" | "or"
    deckMenuRequested = Signal(int, object)  # deck id, global position
    tagMenuRequested = Signal(str, object)  # tag, global position

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setObjectName("browserSidebar")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemClicked.connect(self._clicked)
        self._expanded: set[str] = {"Today", "Decks"}
        self._tag_filter = ""

    def refresh(self) -> None:
        self._remember_expanded()
        self.clear()
        counts = practice.deck_counts(self.db)

        def section(title: str) -> QTreeWidgetItem:
            item = QTreeWidgetItem([title])
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, title)
            self.addTopLevelItem(item)
            return item

        def add(parent, label: str, query: str, key: str, tip: str = "", icon_name: str = "") -> QTreeWidgetItem:
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, query)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, key)
            item.setToolTip(0, tip or query)
            if icon_name:
                item.setIcon(0, icon(icon_name, "muted"))
            parent.addChild(item)
            return item

        today = section("Today")
        for label, q in (("Due", "is:due"), ("New", "is:new"), ("Learning", "is:learn"), ("Added today", "added:1"),
                         ("Attempted today", "rated:1"), ("Missed today", "rated:1:1"), ("Suspended", "is:suspended"),
                         ("All questions", "")):
            add(today, label, q, "Today/" + label)
        flags = section("Flags")
        for n, (name, color) in FLAGS.items():
            item = add(flags, name, f"flag:{n}", f"Flags/{n}")
            item.setForeground(0, QColor(color))
        decks = section("Decks")
        by_name: dict[str, QTreeWidgetItem] = {}
        for d in self.db.decks():
            parent_name, _, leaf = d["name"].rpartition("::")
            parent = by_name.get(parent_name, decks)
            n = counts.get(d["id"], {}).get("total", 0)
            item = add(parent, f"{leaf}  ·  {n}" if n else leaf, f'deck:"{_quote(d["name"])}"', "Decks/" + d["name"],
                       d["name"])
            item.setData(0, Qt.ItemDataRole.UserRole + 2, d["id"])
            by_name[d["name"]] = item
        books = [b for b in self.db.conn.execute(
            "SELECT b.id, b.title, COUNT(c.id) AS n FROM books b JOIN questions c ON c.book_id=b.id GROUP BY b.id ORDER BY b.title")]
        if books:
            sec = section("Books")
            for b in books:
                add(sec, f"{b['title']}  ·  {b['n']}", f'book:"{_quote(b["title"])}"', f"Books/{b['id']}", b["title"])
        tags = self.db.all_tags()
        if tags:
            sec = section("Tags")
            # Section tags (practice.section_tag) in the order of their books, by their first question's page.
            pages = {n["tag"].lower(): n["page"] for ns in practice.sections(self.db).values() for n in ns}
            items: dict[str, QTreeWidgetItem] = {}
            for tag in tags:
                parts = tag.split("::")
                for i in range(len(parts)):
                    path = "::".join(parts[:i + 1])
                    if path.lower() in items:
                        continue
                    parent = items.get("::".join(parts[:i]).lower(), sec)
                    label = parts[i].replace("_", " ") if path.lower() in pages else parts[i]
                    item = add(parent, label, f'tag:"{_quote(path)}"', "Tags/" + path.lower(), path)
                    item.setData(0, Qt.ItemDataRole.UserRole + 3, path)
                    items[path.lower()] = item

            def order(parent: QTreeWidgetItem) -> None:
                kids = parent.takeChildren()
                page = lambda it: pages.get(it.data(0, Qt.ItemDataRole.UserRole + 3).lower())  # noqa: E731
                kids.sort(key=lambda it: (page(it) is None, page(it) or 0, it.text(0).lower()))
                parent.addChildren(kids)
                for k in kids:
                    order(k)
            order(sec)
        types = section("Types")
        for t in question_model.TYPES:
            add(types, question_model.TYPE_LABELS[t], f"type:{t}", f"Types/{t}")
        self._restore_expanded()
        self.filter_tags(self._tag_filter)

    def filter_tags(self, text: str) -> None:
        """Show only the tags containing the text (and the tags above them, expanded so they show)."""
        was, now = self._tag_filter.strip(), text.strip()
        if now and not was:
            self._remember_expanded()  # to put back when the filter is cleared
        self._tag_filter = text
        if was and not now:
            for item in self._items():
                item.setExpanded(False)
            self._restore_expanded()
        needle = now.lower().replace("_", " ")
        sec = next((self.topLevelItem(i) for i in range(self.topLevelItemCount())
                    if self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1) == "Tags"), None)
        if sec is None:
            return

        def show(item: QTreeWidgetItem) -> bool:
            path = item.data(0, Qt.ItemDataRole.UserRole + 3) or ""
            hit = not needle or needle in path.lower().replace("_", " ")
            under = False
            for i in range(item.childCount()):
                under = show(item.child(i)) or under
            item.setHidden(not (hit or under))
            if needle:
                item.setExpanded(under)
            return hit or under
        for i in range(sec.childCount()):
            show(sec.child(i))
        if needle:
            sec.setExpanded(True)

    def _remember_expanded(self) -> None:
        if self._tag_filter.strip():
            return  # filtering expands tags; keep what was open before

        if self.topLevelItemCount() == 0:
            return
        self._expanded = {item.data(0, Qt.ItemDataRole.UserRole + 1) for item in self._items() if item.isExpanded()}

    def _restore_expanded(self) -> None:
        for item in self._items():
            if item.data(0, Qt.ItemDataRole.UserRole + 1) in self._expanded:
                item.setExpanded(True)

    def _items(self):
        return _walk(self.topLevelItem(i) for i in range(self.topLevelItemCount()))

    def _clicked(self, item: QTreeWidgetItem) -> None:
        query = item.data(0, Qt.ItemDataRole.UserRole)
        if query is None:
            item.setExpanded(not item.isExpanded())
            return
        mods = QApplication.keyboardModifiers()
        how = "and" if mods & Qt.KeyboardModifier.ControlModifier else "or" if mods & Qt.KeyboardModifier.ShiftModifier else ""
        self.searchChosen.emit(query, how)

    def _menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        deck_id = item.data(0, Qt.ItemDataRole.UserRole + 2)
        tag = item.data(0, Qt.ItemDataRole.UserRole + 3)
        if deck_id is not None:
            self.deckMenuRequested.emit(deck_id, self.viewport().mapToGlobal(pos))
        elif tag:
            self.tagMenuRequested.emit(tag, self.viewport().mapToGlobal(pos))


def _quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class QuestionView(QWebEngineView):
    """questionview.html: read-only question sides (the browser's preview, Make with Claude's draft). bridge:
    an object the page may call (as `view`), for the draft's checkboxes."""

    def __init__(self, parent=None, bridge=None):
        super().__init__(parent)
        self.setPage(LocalPage(self))
        if bridge is not None:
            self.channel = QWebChannel(self.page())
            self.channel.registerObject("view", bridge)
            self.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(ASSETS / "questionview.html")))
        theme().changed.connect(self._apply_theme)
        self._apply_theme()

    def js(self, function: str, *args) -> None:
        code = f"{function}({', '.join(json.dumps(a) for a in args)})"
        if self._ready:
            self.page().runJavaScript(code)
        else:
            self._pending.append(code)

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        self.page().runJavaScript(f"setMedia({json.dumps(media_url())})")
        for code in self._pending:
            self.page().runJavaScript(code)
        self._pending.clear()

    def _apply_theme(self) -> None:
        self.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self.js("setTheme", theme().colors, theme().dark)


class QuestionBrowser(QMainWindow):
    """A view over every question: a window of its own, or (embedded) the main window's Questions page.
    questionsChanged: something was edited, moved, rescheduled or deleted (practice counts may have
    changed)."""

    questionsChanged = Signal()
    bookRequested = Signal(int, int)  # book id, page (-1 if unknown)
    practiceDeckRequested = Signal(int)  # deck id
    statsRequested = Signal(int)  # deck id (0 = all)
    migrateRequested = Signal()

    def __init__(self, db: Database, parent=None, embedded: bool = False, maker=None):
        super().__init__(parent)
        self.db = db
        self.embedded = embedded
        self.maker = maker  # deck_maker_dialog.DeckMaker, for Make with Claude
        self.setWindowTitle("Browse questions — Study Reader")
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        else:
            self.setWindowFlag(Qt.WindowType.Window)
            self.resize(1440, 880)
        self._question_id: int | None = None  # the question in the editor
        self._shown = 0  # how many times _show_current has run
        self._keep_editor = False  # during search(keep_editor=True)
        self._add_dialog: AddDialog | None = None

        self.search_box = QLineEdit(objectName="browserSearch")
        self.search_box.setPlaceholderText('Search: words, deck:Physics, tag:exam, is:due, -is:suspended, "a phrase", prop:ivl>30 …')
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self.search)
        self._search_timer = QTimer(self, singleShot=True, interval=450)
        self._search_timer.timeout.connect(self.search)
        self.search_box.textEdited.connect(lambda _: self._search_timer.start())
        self.status = QLabel()
        self.status.setProperty("role", "muted")

        self.sidebar = Sidebar(db)
        self.sidebar.searchChosen.connect(self._sidebar_search)
        self.sidebar.deckMenuRequested.connect(self._deck_menu)
        self.sidebar.tagMenuRequested.connect(self._tag_menu)

        self.model = QuestionTableModel(db, self)
        self.table = QTableView(objectName="questionTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(MIN_SECTION)
        self.table.horizontalHeader().sectionResized.connect(self._section_resized)
        self.table.horizontalHeader().sectionMoved.connect(self._section_moved)
        self._fitting = False
        self._widths: dict[str, int] = {}  # columns the user resized, by key
        self._widths_timer = QTimer(self, singleShot=True, interval=500)
        self._widths_timer.timeout.connect(
            lambda: self.db.set_setting(WIDTHS_SETTING, json.dumps(self._widths)))
        self.table.viewport().installEventFilter(self)  # refit the columns when the table's width changes
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_by)
        self.table.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._columns_menu)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.selectionModel().currentRowChanged.connect(self._current_changed)
        self.table.doubleClicked.connect(lambda _: self.editor_focus())

        self.editor = QuestionEditor(db)
        self.editor.edited.connect(lambda: self._save_timer.start())
        self.editor_status = QLabel()
        self.editor_status.setWordWrap(True)
        self.editor_status.setProperty("role", "muted")
        self._save_timer = QTimer(self, singleShot=True, interval=700)
        # Edits that couldn't be saved yet (a question switched to a type whose answer isn't filled
        # in yet), by group id: (its updated_at then, editor draft, why not), shown again when
        # the question (or a sibling) is, rather than the stored version.
        self._drafts: dict[int, tuple[float, dict, str]] = {}
        self._save_timer.timeout.connect(self._save_now)
        self.question_line = QLabel()
        self.question_line.setProperty("role", "muted")
        self.question_line.setWordWrap(True)
        editor_box = QWidget()
        col = QVBoxLayout(editor_box)
        col.setContentsMargins(10, 8, 10, 6)
        col.addWidget(self.question_line)
        col.addWidget(self.editor, 1)
        col.addWidget(self.editor_status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(editor_box)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.preview = QuestionView()
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(scroll)
        right.addWidget(self.preview)
        right.setSizes([480, 360])

        top = QHBoxLayout()
        top.setContentsMargins(10, 8, 10, 4)
        top.addWidget(self.search_box, 1)
        for text, icon_name, tip, slot in (
            ("Add", "message-plus", "Add questions (Ctrl+E)", self.add_questions),
            ("Statistics", "chart", "Statistics", lambda: self.statsRequested.emit(0)),
        ):
            b = QPushButton(text)
            b.setIcon(icon(icon_name))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            top.addWidget(b)

        center = QWidget()
        mid = QVBoxLayout(center)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        mid.addLayout(top)
        mid.addWidget(self.table, 1)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(10, 4, 10, 6)
        status_row.addWidget(self.status, 1)
        mid.addLayout(status_row)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.tag_filter = QLineEdit(objectName="tagFilter", placeholderText="Filter tags…")
        self.tag_filter.setClearButtonEnabled(True)
        self.tag_filter.textChanged.connect(self.sidebar.filter_tags)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(4)
        side_layout.addWidget(self.tag_filter)
        side_layout.addWidget(self.sidebar, 1)
        split.addWidget(side)
        split.addWidget(center)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 760, 430])
        body = QWidget(objectName="browser")
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)
        self.setCentralWidget(body)

        self._load_settings()
        self._build_menus()
        self.refresh()

    # -- settings ------------------------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            cols = [c for c in (OLD_COLUMNS.get(c, c) for c in json.loads(self.db.get_setting(COLUMNS_SETTING, "[]")))
                    if c in COLUMNS]
        except ValueError:
            cols = []
        self.model.set_columns(cols or list(DEFAULT_COLUMNS))
        try:
            widths = json.loads(self.db.get_setting(WIDTHS_SETTING, "{}"))
            self._widths = {OLD_COLUMNS.get(k, k): int(v) for k, v in widths.items()
                            if OLD_COLUMNS.get(k, k) in COLUMNS and int(v) > 0}
        except (ValueError, TypeError, AttributeError):
            self._widths = {}
        sort, _, order = self.db.get_setting(SORT_SETTING, "added:desc").partition(":")
        sort = OLD_COLUMNS.get(sort, sort)
        self.sort = sort if sort in search.SORTS else "added"
        self.descending = order == "desc"
        self._fit_columns()

    def _fit_columns(self) -> None:
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._fit_widths()
        self._show_sort_indicator()

    def _compact_width(self, key: str) -> int:
        """A column wide enough for its bold header with a sort arrow, and for its longest text."""
        font = self.table.horizontalHeader().font()
        font.setBold(True)
        head = QFontMetrics(font).horizontalAdvance(COLUMNS[key]) + 12 + 18  # padding, sort arrow
        cells = QFontMetrics(self.table.font())
        text = max((cells.horizontalAdvance(t) for t in SAMPLES.get(key, ())), default=0) + 12 + 8  # padding, margin
        return max(head, text)

    def _fit_widths(self) -> None:
        """Size the columns to the table: compact ones to their contents, the rest sharing what's left.

        Columns the user resized keep their width; the shared ones still fill the remainder."""
        header, cols = self.table.horizontalHeader(), self.model.columns
        widths: dict[int, int] = {}
        flex: list[int] = []
        for i, key in enumerate(cols):
            if key in self._widths:
                widths[i] = self._widths[key]
            elif key in FLEX_COLUMNS:
                flex.append(i)
            else:
                widths[i] = self._compact_width(key)
        width = self.table.viewport().width()
        space = width - sum(widths.values())
        least = sum(FLEX_COLUMNS[cols[i]][1] for i in flex)
        if flex and space < least:  # too narrow for their minimums: shrink them alike rather than scroll
            for i in flex:
                widths[i] = max(space * FLEX_COLUMNS[cols[i]][1] // least, MIN_SECTION)
            flex = []
        free = list(flex)
        while free:  # share the space by weight; a column held at its least or most width drops out
            total = sum(FLEX_COLUMNS[cols[i]][0] for i in free)
            share = {i: space * FLEX_COLUMNS[cols[i]][0] / total for i in free}
            low = [i for i in free if share[i] < FLEX_COLUMNS[cols[i]][1]]
            high = [i for i in free if (top := FLEX_COLUMNS[cols[i]][2]) and share[i] > top]
            if not low and not high:
                widths.update((i, int(share[i])) for i in free)
                break
            for i in low or high:
                widths[i] = FLEX_COLUMNS[cols[i]][1 if low else 2]
                space -= widths[i]
                free.remove(i)
        if cols and (left := width - sum(widths.values())) > 0:  # rounding, or nothing left to grow
            grow = free[0] if free else header.logicalIndex(len(cols) - 1)
            widths[grow] += left
        self._fitting = True
        try:
            for i, w in widths.items():
                if header.sectionSize(i) != w:
                    header.resizeSection(i, w)
        finally:
            self._fitting = False

    def eventFilter(self, obj, event) -> bool:
        if obj is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_widths()
        return super().eventFilter(obj, event)

    def _section_resized(self, index: int, _old: int, new: int) -> None:
        if self._fitting or not 0 <= index < len(self.model.columns):
            return
        self._widths[self.model.columns[index]] = new
        self._widths_timer.start()
        self._fit_widths()

    def _section_moved(self, _logical: int, _old: int, _new: int) -> None:
        """Keep a dragged column's place as the column order, so it lasts."""
        header = self.table.horizontalHeader()
        cols = [self.model.columns[header.logicalIndex(v)] for v in range(header.count())]
        header.blockSignals(True)
        try:
            for v in range(header.count()):  # undo the header's own reordering; the model now has it
                header.moveSection(header.visualIndex(v), v)
        finally:
            header.blockSignals(False)
        self._set_columns(cols)

    def _show_sort_indicator(self) -> None:
        header = self.table.horizontalHeader()
        if self.sort in self.model.columns:
            header.setSortIndicator(self.model.columns.index(self.sort),
                                    Qt.SortOrder.DescendingOrder if self.descending else Qt.SortOrder.AscendingOrder)
        else:
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

    # -- menus ---------------------------------------------------------------------------
    def _build_menus(self) -> None:
        bar = self.menuBar()
        bar.setObjectName("browserMenu")

        def act(menu: QMenu, text: str, slot, shortcut=None) -> QAction:
            a = menu.addAction(text)
            a.triggered.connect(lambda _=False: slot())
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            return a

        file = bar.addMenu("&File")
        act(file, "Add questions…", self.add_questions, "Ctrl+E")
        act(file, "Migrate from Anki…", self.migrateRequested.emit)
        if not self.embedded:
            file.addSeparator()
            act(file, "Close", self.close, "Ctrl+W")
        edit = bar.addMenu("&Edit")
        act(edit, "Find", lambda: (self.search_box.setFocus(), self.search_box.selectAll()), "Ctrl+F")
        act(edit, "Select all", self.table.selectAll, "Ctrl+Alt+A")
        act(edit, "Invert selection", self._invert_selection, "Ctrl+Alt+S")
        act(edit, "Find and replace…", self.find_replace, "Ctrl+Alt+F")
        questions = bar.addMenu("&Questions")
        self.selection_actions = [
            act(questions, "Change deck…", self.change_deck, "Ctrl+D"),
            act(questions, "Set due date…", self.set_due, "Ctrl+Shift+D"),
            act(questions, "Forget (make new)…", self.forget, "Ctrl+Alt+N"),
            act(questions, "Toggle suspend", self.toggle_suspend, "Ctrl+J"),
        ]
        flag_menu = questions.addMenu("Flag")
        for n, (name, color) in FLAGS.items():
            self.selection_actions.append(act(flag_menu, name, lambda n=n: self.set_flag(n), f"Ctrl+{n}"))
        self.selection_actions.append(act(flag_menu, "No flag", lambda: self.set_flag(0), "Ctrl+0"))
        questions.addSeparator()
        self.selection_actions += [
            act(questions, "Info…", self.question_info, "Ctrl+Shift+I"),
            act(questions, "Open in book", self.open_in_book, "Ctrl+Shift+O"),
            act(questions, "Delete…", self.delete, "Ctrl+Delete"),
        ]
        questions.addSeparator()
        self.selection_actions += [
            act(questions, "Add tags…", self.add_tags, "Ctrl+Shift+A"),
            act(questions, "Remove tags…", self.remove_tags, "Ctrl+Alt+Shift+A"),
            act(questions, "Toggle mark", self.toggle_mark, "Ctrl+K"),
        ]
        claude = bar.addMenu("C&laude")
        act(claude, "Make a deck with Claude…", lambda: self.make_deck(""), "Ctrl+Shift+M")
        self.improve_action = act(claude, "Improve with Claude…", self.improve, "Ctrl+Shift+K")
        self.selection_actions.append(self.improve_action)
        act(claude, "Claude's draft…", self.show_draft)
        claude.addSeparator()
        self.undo_rewrite_action = act(claude, "Undo Claude's rewrite of this question", self.undo_rewrite)
        self.undo_rewrite_action.setEnabled(False)
        for a in claude.actions():
            a.setVisible(self.maker is not None)
        claude.menuAction().setVisible(self.maker is not None)
        go = bar.addMenu("&Go")
        act(go, "Previous question", lambda: self._step(-1), "Ctrl+P")
        act(go, "Next question", lambda: self._step(1), "Ctrl+N")
        act(go, "Sidebar", self.sidebar.setFocus, "Ctrl+Shift+R")
        act(go, "Table", self.table.setFocus, "Ctrl+Shift+T")
        act(go, "Editor", self.editor_focus, "Ctrl+Shift+E")

    def _columns_menu(self, pos) -> None:
        menu = QMenu(self)
        for key, label in COLUMNS.items():
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key in self.model.columns)
            a.triggered.connect(lambda on, k=key: self._toggle_column(k, on))
        menu.addSeparator()
        menu.addAction("Reset columns").triggered.connect(self._reset_columns)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _toggle_column(self, key: str, on: bool) -> None:
        cols = [c for c in self.model.columns if c != key]
        if on:
            cols.append(key)
        if cols:
            self._set_columns(cols)

    def _set_columns(self, cols: list[str]) -> None:
        self.db.set_setting(COLUMNS_SETTING, json.dumps(cols))
        self.save_question()
        selected, current = self.selected_ids(), self._question_id
        self.model.set_columns(cols)
        self._fit_columns()
        self._select(selected, current)

    def _reset_columns(self) -> None:
        self._widths = {}
        self._widths_timer.stop()
        self.db.set_setting(WIDTHS_SETTING, "{}")
        self._set_columns(list(DEFAULT_COLUMNS))

    def _table_menu(self, pos) -> None:
        if not self.selected_ids():
            return
        menu = QMenu(self)
        for a in self.selection_actions[:4]:
            menu.addAction(a)
        flag = menu.addMenu("Flag")
        for a in self.selection_actions[4:12]:
            flag.addAction(a)
        menu.addSeparator()
        for a in self.selection_actions[12:]:
            if a.isVisible():
                menu.addAction(a)
        if self.undo_rewrite_action.isEnabled() and self.undo_rewrite_action.isVisible():
            menu.addAction(self.undo_rewrite_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # -- searching -----------------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read everything: the sidebar and the search."""
        self.sidebar.refresh()
        self.search()

    def search(self, keep: bool = True, keep_editor: bool = False) -> None:
        """Run the search box's query. keep: the selection and current question stay where they can.
        keep_editor: an autosave added or dropped questions; the editor is left as it is (caret,
        undo) while the current question is still one of its group's."""
        self._search_timer.stop()
        self.save_question()
        query = self.search_box.text().strip()
        selected, current = (self.selected_ids(), self._question_id) if keep else ([], None)
        try:
            ids = search.question_ids(self.db, query, self.sort, self.descending)
        except search.SearchError as e:
            self.status.setText(f"<span style='color:{theme().colors['error']}'>{e}</span>")
            return
        self._keep_editor = keep_editor
        try:
            self.model.set_ids(ids)
            self._select(selected, current)
        finally:
            self._keep_editor = False
        self._update_status()

    def set_search(self, query: str) -> None:
        self.search_box.setText(query)
        self.search(keep=False)

    def _sidebar_search(self, query: str, how: str) -> None:
        current = self.search_box.text().strip()
        if how and current and query:
            query = f"({current}) {query}" if how == "and" else f"({current}) OR ({query})"
        self.set_search(query)

    def _sort_by(self, section: int) -> None:
        key = self.model.columns[section]
        if key not in search.SORTS:
            self._show_sort_indicator()
            return
        self.descending = not self.descending if key == self.sort else key in ("added", "modified", "attempted", "reps", "lapses")
        self.sort = key
        self.db.set_setting(SORT_SETTING, f"{key}:{'desc' if self.descending else 'asc'}")
        self._show_sort_indicator()
        self.search()

    def _update_status(self) -> None:
        n, chosen = len(self.model.ids), len(self.table.selectionModel().selectedRows())
        text = f"{n} question{'s' * (n != 1)} shown"
        if chosen:
            text += f", {chosen} selected"
        self.status.setText(text)

    # -- selection -----------------------------------------------------------------------
    def selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.ids[r] for r in rows if r < len(self.model.ids)]

    def _select(self, ids: list[int], current: int | None) -> None:
        sel = self.table.selectionModel()
        shown = self._shown  # moving the current row shows it (_current_changed); else it is shown here
        rows = {question_id: i for i, question_id in enumerate(self.model.ids)}
        selection = QItemSelection()
        last = len(self.model.columns) - 1
        chosen = sorted(rows[question_id] for question_id in ids if question_id in rows)
        start = 0
        for i in range(1, len(chosen) + 1):  # one range per run of consecutive rows
            if i == len(chosen) or chosen[i] != chosen[i - 1] + 1:
                selection.select(self.model.index(chosen[start], 0), self.model.index(chosen[i - 1], last))
                start = i
        sel.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        if current not in rows:  # the question that was current is gone: the first selected one is current
            current = next((question_id for question_id in ids if question_id in rows), None)
        if current in rows:
            sel.setCurrentIndex(self.model.index(rows[current], 0), QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(self.model.index(rows[current], 0))
        elif not ids and self.model.ids:
            self.table.selectRow(0)
        if self._shown == shown:
            self._show_current()

    def _invert_selection(self) -> None:
        chosen = set(self.selected_ids())
        self._select([i for i in self.model.ids if i not in chosen], None)

    def _step(self, delta: int) -> None:
        row = self.table.currentIndex().row()
        self.table.selectRow(min(max(row + delta, 0), len(self.model.ids) - 1))

    def _selection_changed(self, *_) -> None:
        self._update_status()
        has = self.table.selectionModel().hasSelection()
        for a in self.selection_actions:
            a.setEnabled(has)

    def _current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        self.save_question()
        self._show_current()

    def _show_current(self) -> None:
        # The current row: on a click Qt moves it before it updates the selection, so the
        # selection can't be trusted here.
        index = self.table.currentIndex()
        question_id = self.model.ids[index.row()] if index.isValid() and index.row() < len(self.model.ids) else None
        question = self.db.question(question_id) if question_id is not None else None
        self._shown += 1
        self._question_id = question["id"] if question else None
        # After an autosave that added or dropped questions the editor already shows the group:
        # loading it again would move the caret and lose the undo history mid-typing.
        reload = not (self._keep_editor and question is not None and question["group_id"] == self.editor.group_id)
        if reload:
            self.editor.load(question["group_id"] if question else None)
            self.editor_status.clear()
        draft = self._drafts.get(question["group_id"]) if question and reload else None
        if draft is not None:
            group = self.db.question_group(question["group_id"])
            if group is not None and group["updated_at"] == draft[0]:
                self.editor.restore(draft[1])
                self._show_not_saved(draft[2])
            else:  # changed since (or gone): the draft is out of date
                del self._drafts[question["group_id"]]
        self._show_question_line(question)
        self.preview.js("showPreview", json.loads(question["data"]) if question else None)
        self.undo_rewrite_action.setEnabled(question is not None and self.db.question_rewrite(question["id"]) is not None)

    def _show_question_line(self, question) -> None:
        if question is None:
            self.question_line.setText("")
            return
        deck = self.db.deck(question["deck_id"])
        group = self.db.question_group(question["group_id"])
        parts = [deck["name"] if deck else ""]
        if group is not None:
            parts.append(sibling_text(group["kind"], question["ord"], [c["ord"] for c in self.db.group_questions(group["id"])]))
        if question["book_id"]:
            book = self.db.book(question["book_id"])
            if book:
                parts.append(f"from “{book['title']}”" + (f", p. {question['page'] + 1}" if question["page"] is not None else ""))
        self.question_line.setText(" · ".join(p for p in parts if p))

    def editor_focus(self) -> None:
        self.editor.focus_first()

    def show_question(self, question_id: int) -> None:
        """Show one question (e.g. Edit in practice), bringing the window up if it is one."""
        if not self.embedded:
            self.show()
            self.raise_()
            self.activateWindow()
        self.set_search(f"qid:{question_id}")

    # -- editing -------------------------------------------------------------------------
    def save_question(self) -> None:
        """Store pending edits to the question in the editor (and its siblings), if there are any."""
        if self._save_timer.isActive():
            self._save_now()

    def _save_now(self) -> None:
        # Also the save timer's timeout, by which time a single-shot timer is no longer active.
        self._save_timer.stop()
        if self.editor.group_id is None:
            return
        before = {c["ord"] for c in self.db.group_questions(self.editor.group_id)}
        group = self.db.question_group(self.editor.group_id)
        try:
            self.editor.save()
        except question_model.QuestionError as e:
            if group is not None:
                self._drafts[group["id"]] = (group["updated_at"], self.editor.draft(), str(e))
            self._show_not_saved(str(e))
            return
        self._drafts.pop(self.editor.group_id, None)
        questions = self.db.group_questions(self.editor.group_id)
        self.editor_status.setText(f"Saved{sibling_edit_text(self.editor.kind, len(questions))}.")
        self.model.invalidate([c["id"] for c in questions])
        question = self.db.question(self._question_id) if self._question_id is not None else None
        if question is not None:
            self.preview.js("showPreview", json.loads(question["data"]))
        if {c["ord"] for c in questions} != before:  # blanks or sides added or removed: questions come and go
            QTimer.singleShot(0, self._search_after_save)
        self.questionsChanged.emit()

    def _search_after_save(self) -> None:
        """Show the questions an autosave added or dropped, leaving the editor as it is."""
        if self.editor.group_id is None:
            self.search()
            return
        ids = [c["id"] for c in self.db.group_questions(self.editor.group_id)]
        if self._question_id not in ids and ids:  # its blank or side was removed: a sibling stays current
            self._question_id = ids[0]
        self.search(keep_editor=True)

    def _show_not_saved(self, why: str) -> None:
        self.editor_status.setText(f"<span style='color:{theme().colors['error']}'>Not saved: {why}</span>")

    def add_questions(self) -> None:
        if self._add_dialog is None:
            self._add_dialog = AddDialog(self.db, self)
            self._add_dialog.added.connect(self._on_added)
        current = self.db.question(self._question_id) if self._question_id is not None else None
        if current is not None and not self.db.get_setting("add_deck"):
            self._add_dialog.set_deck(self.db.deck(current["deck_id"])["name"])
        self._add_dialog.show()
        self._add_dialog.raise_()
        self._add_dialog.activateWindow()

    def add_questions_to(self, deck_name: str) -> None:
        self.add_questions()
        self._add_dialog.set_deck(deck_name)

    def _on_added(self, group_id: int) -> None:
        self.sidebar.refresh()
        self.search()
        self.questionsChanged.emit()

    # -- actions on the selection --------------------------------------------------------
    def _changed(self, refresh_sidebar: bool = False) -> None:
        self.model.invalidate()
        if refresh_sidebar:
            self.sidebar.refresh()
        self.search()
        self.questionsChanged.emit()

    def change_deck(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Change deck")
        box = DeckBox(self.db)
        current = self.db.question(ids[0])
        box.setCurrentText(self.db.deck(current["deck_id"])["name"] if current else "")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Move {len(ids)} question{'s' * (len(ids) != 1)} to (type a new name to make a deck):"))
        layout.addWidget(box)
        layout.addWidget(buttons)
        if dialog.exec() and box.deck_name():
            self.db.update_questions(ids, deck_id=self.db.ensure_deck(box.deck_name()))
            self._changed(refresh_sidebar=True)

    def _ask_tags(self, title: str, label: str) -> list[str]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        edit = QLineEdit(objectName="tagsInput", placeholderText="tag1 tag2 Parent::child")
        from PySide6.QtWidgets import QCompleter
        completer = QCompleter(self.db.all_tags(), dialog)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        edit.setCompleter(completer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(label))
        layout.addWidget(edit)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(380)
        return question_model.split_tags(edit.text()) if dialog.exec() else []

    def _questions_text(self, ids: list[int]) -> str:
        """'3 questions', plus the questions that share their text (and so their tags) when there are any."""
        groups = self.db.group_ids(ids)
        siblings = self.db.conn.execute(
            f"SELECT COUNT(*) FROM questions WHERE group_id IN ({','.join('?' * len(groups))})", groups).fetchone()[0] - len(ids)
        text = f"{len(ids)} question{'s' * (len(ids) != 1)}"
        return text + (f" (and {siblings} more made from the same text)" if siblings > 0 else "")

    def add_tags(self) -> None:
        ids = self.selected_ids()
        tags = self._ask_tags("Add tags", f"Tags to add to {self._questions_text(ids)}:") if ids else []
        if tags:
            self.db.add_tags(self.db.group_ids(ids), tags)
            self._changed(refresh_sidebar=True)
            self.status.setText(f"Added tags to {self._questions_text(ids)}.")

    def remove_tags(self) -> None:
        ids = self.selected_ids()
        tags = self._ask_tags("Remove tags", f"Tags to remove from {self._questions_text(ids)}:") if ids else []
        if tags:
            self.db.remove_tags(self.db.group_ids(ids), tags)
            self._changed(refresh_sidebar=True)
            self.status.setText(f"Removed tags from {self._questions_text(ids)}.")

    def toggle_mark(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        groups = self.db.group_ids(ids)
        marked = not self.db.any_group(groups, "tags NOT LIKE '% marked %'")
        (self.db.remove_tags if marked else self.db.add_tags)(groups, ["marked"])
        self._changed(refresh_sidebar=True)

    def toggle_suspend(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        suspended = not self.db.any_question(ids, "suspended=0")
        self.db.update_questions(ids, suspended=0 if suspended else 1)
        self._changed()

    def set_flag(self, flag: int) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        if flag and not self.db.any_question(ids, "flag!=?", (flag,)):
            flag = 0  # the same flag again takes it off, as in Anki
        self.db.update_questions(ids, flag=flag)
        self._changed()

    def forget(self) -> None:
        ids = self.selected_ids()
        if ids and QMessageBox.question(
            self, "Forget", f"Make {len(ids)} question{'s' * (len(ids) != 1)} new again? Their past attempts are kept.",
        ) == QMessageBox.StandardButton.Yes:
            practice.forget(self.db, ids)
            self._changed()

    def set_due(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        text, ok = QInputDialog.getText(
            self, "Set due date",
            "Due in how many days? 0 = today, 1 = tomorrow, 3-7 = spread over 3 to 7 days.\n"
            "New questions stay in the new queue.", text=self.db.get_setting("browser_due_days", "0"))
        if not ok:
            return
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*!?\s*", text)
        if not m:
            QMessageBox.warning(self, "Set due date", "Enter a number of days, or a range like 3-7.")
            return
        self.db.set_setting("browser_due_days", text.strip())
        first, last = int(m.group(1)), int(m.group(2)) if m.group(2) else None
        moved = practice.set_due(self.db, ids, first, last)
        self.status.setText(f"Rescheduled {moved} question{'s' * (moved != 1)}.")
        self._changed()

    def delete(self) -> None:
        ids = self.selected_ids()
        if ids and QMessageBox.question(
            self, "Delete questions", f"Delete {len(ids)} question{'s' * (len(ids) != 1)} and their past attempts? "
            "This can't be undone (Suspend keeps a question out of practice instead).",
        ) == QMessageBox.StandardButton.Yes:
            self.save_question()
            self.db.delete_questions(ids)
            self._changed(refresh_sidebar=True)

    def question_info(self) -> None:
        if self._question_id is not None:
            QuestionInfoDialog(self.db, self._question_id, self).exec()

    def open_in_book(self) -> None:
        question = self.db.question(self._question_id) if self._question_id is not None else None
        if question is None or question["book_id"] is None:
            self.status.setText("This question isn't from a book.")
            return
        self.bookRequested.emit(question["book_id"], question["page"] if question["page"] is not None else -1)

    def find_replace(self) -> None:
        dialog = FindReplaceDialog(self.db, bool(self.selected_ids()), self)
        if not dialog.exec():
            return
        groups = self.db.group_ids(self.selected_ids()) if dialog.selected_only.isChecked() else \
            [r[0] for r in self.db.conn.execute("SELECT id FROM question_groups")]
        try:
            changed, failed = find_replace(self.db, groups, dialog.find.text(), dialog.replace.text(),
                                           dialog.field.currentData(), dialog.regex.isChecked(), dialog.case.isChecked())
        except re.error as e:
            QMessageBox.warning(self, "Find and replace", f"That regular expression can't be used: {e}")
            return
        msg = f"Changed {changed} question{'s' * (changed != 1)}."
        if failed:
            msg += f" {failed} couldn't be changed (the result wasn't a valid question)."
        self.status.setText(msg)
        self._changed()

    # -- decks and tags --------------------------------------------------------------------
    def _deck_menu(self, deck_id: int, pos) -> None:
        deck = self.db.deck(deck_id)
        if deck is None:
            return
        menu = QMenu(self)
        menu.addAction(icon("questions"), "Practice this deck").triggered.connect(lambda: self.practiceDeckRequested.emit(deck_id))
        menu.addAction(icon("message-plus"), "Add questions here…").triggered.connect(lambda: self.add_questions_to(deck["name"]))
        menu.addAction(icon("chart"), "Statistics").triggered.connect(lambda: self.statsRequested.emit(deck_id))
        if self.maker is not None:
            menu.addSeparator()
            menu.addAction(icon("sparkles"), "Make questions here with Claude…").triggered.connect(
                lambda: self.make_deck(deck["name"]))
            menu.addAction("Improve this deck's questions with Claude…").triggered.connect(
                lambda: self.improve(search.question_ids(self.db, f'deck:"{_quote(deck["name"])}"')))
        menu.addSeparator()
        menu.addAction("Rename or move…").triggered.connect(lambda: self.rename_deck(deck_id))
        menu.addAction(icon("trash", "muted"), "Delete…").triggered.connect(lambda: self.delete_deck(deck_id))
        menu.exec(pos)

    def rename_deck(self, deck_id: int) -> None:
        deck = self.db.deck(deck_id)
        name, ok = QInputDialog.getText(self, "Rename deck", "Full name (Parent::Child moves it; an existing name merges):",
                                        text=deck["name"])
        if ok and name.strip():
            try:
                self.db.rename_deck(deck_id, name)
            except ValueError as e:
                QMessageBox.warning(self, "Rename deck", str(e))
                return
            self._changed(refresh_sidebar=True)

    def delete_deck(self, deck_id: int) -> None:
        deck = self.db.deck(deck_id)
        n = len(self.db.deck_subtree(deck_id))
        questions = self.db.count_questions(deck_id=deck_id)
        if QMessageBox.question(
            self, "Delete deck", f"Delete “{deck['name']}”{f' and its {n - 1} subdeck(s)' if n > 1 else ''}, with "
            f"{questions} question{'s' * (questions != 1)} and their past attempts? This can't be undone.",
        ) == QMessageBox.StandardButton.Yes:
            self.db.delete_deck(deck_id)
            self._changed(refresh_sidebar=True)

    def _tag_menu(self, tag: str, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Rename…").triggered.connect(lambda: self.rename_tag(tag))
        menu.addAction(icon("trash", "muted"), "Remove from all questions…").triggered.connect(lambda: self.delete_tag(tag))
        menu.exec(pos)

    def rename_tag(self, tag: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename tag", "New name (tags under it are renamed too):", text=tag)
        name = "".join(name.split())
        if ok and name and name != tag:
            self.db.rename_tag(tag, name)
            self._changed(refresh_sidebar=True)

    def delete_tag(self, tag: str) -> None:
        if QMessageBox.question(self, "Remove tag", f"Remove the tag “{tag}” (and tags under it) from every question?") \
                == QMessageBox.StandardButton.Yes:
            self.db.rename_tag(tag, "")
            self._changed(refresh_sidebar=True)

    # -- Make with Claude ------------------------------------------------------------------
    def make_deck(self, parent_deck: str) -> None:
        if self.maker is not None:
            self.maker.make_deck(self, parent_deck)

    def improve(self, question_ids=None) -> None:
        question_ids = self.selected_ids() if question_ids is None else question_ids
        if self.maker is not None and question_ids:
            self.save_question()
            self.maker.improve(question_ids, self)

    def show_draft(self) -> None:
        if self.maker is None:
            return
        if self.maker.draft() is None:
            QMessageBox.information(self, "Claude's draft", "There is no draft from Claude waiting. Make a deck with "
                                    "Claude (Ctrl+Shift+M), or select questions and improve them (Ctrl+Shift+K).")
            return
        self.maker.show_draft(self)

    def undo_rewrite(self) -> None:
        self.save_question()
        if self.maker is not None and self._question_id is not None and self.maker.undo_rewrite(self._question_id):
            self._changed()

    def questions_rewritten(self) -> None:
        """Make with Claude added or rewrote questions: re-read them, and the editor's question."""
        self.model.invalidate()
        self.sidebar.refresh()
        if self.isVisible():
            self.search()

    def closeEvent(self, event) -> None:
        self.save_question()
        super().closeEvent(event)


def sibling_text(kind: str, ord: int, ords: list[int]) -> str:
    """Which of the questions made from the same text a question is ("Reverse side", "Blank 2 of 3"),
    or '' for a question on its own."""
    if len(ords) < 2:
        return ""
    if kind == "both_ways":
        return "Reverse side" if ord == 1 else "Forward side"
    what = {"fill_in": "Blank", "image_labels": "Label"}.get(kind, "Question")
    if sorted(ords) == list(range(len(ords))):
        return f"{what} {ord + 1} of {len(ords)}"
    return f"{what} {ord + 1} (one of {len(ords)})"


def sibling_edit_text(kind: str, count: int) -> str:
    """What else an edit to one of `count` questions made from the same text changes, or ''."""
    if count < 2:
        return ""
    if kind == "both_ways":
        return " (both sides)"
    what = {"fill_in": "fill-in-the-blank", "image_labels": "image labels"}.get(kind, "")
    return f" (this changes its other {what + ' ' if what else ''}question{'s' * (count > 2)} too)"


# -- find and replace ----------------------------------------------------------------------
FIND_FIELDS = [("", "All fields"), ("front", "Front"), ("back", "Back"), ("text", "Text"), ("extra", "Extra"),
               ("header", "Header"), ("question", "Question"), ("answer", "Answer"), ("explanation", "Explanation"),
               ("choices", "Choices")]


def find_replace(db: Database, group_ids, find: str, replace: str, field: str = "", regex: bool = False,
                 case: bool = False) -> tuple[int, int]:
    """Replace text in the fields of these question_groups rows (one field, or all text fields).
    Returns (questions changed, questions that would no longer be valid and were left alone), counting
    every question that shares the text. Raises re.error, before changing anything, for a regular
    expression or replacement that can't be used."""
    if not find:
        return 0, 0
    pattern = re.compile(find if regex else re.escape(find), 0 if case else re.IGNORECASE)
    repl = replace if regex else replace.replace("\\", "\\\\")
    pattern.sub(repl, "")  # a bad group reference is only noticed on a match: one here, before any change
    changed = failed = 0
    for group_id in group_ids:
        group = db.question_group(group_id)
        if group is None:
            continue
        fields = json.loads(group["fields"])
        keys = question_model.TEXT_FIELDS.get(group["kind"]) or ("question", "answer", "explanation", "rubric", "choices", "unit")
        new = dict(fields)
        for key in keys:
            if field and key != field:
                continue
            value = fields.get(key)
            if isinstance(value, str):
                new[key] = pattern.sub(repl, value)
            elif key == "choices" and isinstance(value, list):
                new[key] = [pattern.sub(repl, str(v)) for v in value]
        if new == fields:
            continue
        try:
            if group["kind"] in question_model.QUESTION_TYPES:
                new = quiz.validate_question(new)
            db.update_question_group(group_id, new)
            changed += len(db.group_questions(group_id))
        except (question_model.QuestionError, ValueError, TypeError, KeyError):
            failed += len(db.group_questions(group_id))
    return changed, failed


class FindReplaceDialog(QDialog):
    def __init__(self, db: Database, has_selection: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find and replace")
        self.setMinimumWidth(460)
        self.find = QLineEdit()
        self.replace = QLineEdit()
        self.field = QComboBox()
        for key, label in FIND_FIELDS:
            self.field.addItem(label, key)
        self.selected_only = QCheckBox("Selected questions only (and those sharing their text)")
        self.selected_only.setChecked(has_selection)
        self.selected_only.setEnabled(has_selection)
        self.regex = QCheckBox("Regular expression")
        self.case = QCheckBox("Match case")
        form = QFormLayout()
        form.addRow("Find", self.find)
        form.addRow("Replace with", self.replace)
        form.addRow("In", self.field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        for w in (self.selected_only, self.regex, self.case):
            layout.addWidget(w)
        layout.addWidget(buttons)


# -- question info -------------------------------------------------------------------------
class QuestionInfoDialog(QDialog):
    """A question's schedule, memory state and every attempt."""

    def __init__(self, db: Database, question_id: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Question info")
        self.resize(620, 560)
        question = db.question(question_id)
        group = db.question_group(question["group_id"])
        deck = db.deck(question["deck_id"])
        attempts = db.attempts(question_id)
        now = time.time()
        r = practice.retrievability(db, question, now)
        total_ms = sum(x["duration"] for x in attempts)
        timed = [x["duration"] for x in attempts if x["duration"]]
        rows = [
            ("Question", group["sort_text"][:160] if group else ""),
            ("Deck", deck["name"] if deck else ""),
            ("Type", question_model.TYPE_LABELS.get(group["kind"], "") if group else ""),
            ("Part", sibling_text(group["kind"], question["ord"], [c["ord"] for c in db.group_questions(group["id"])])
             if group else ""),
            ("Created", _date(question["created_at"])),
            ("First attempt", _date(attempts[0]["attempted_at"]) if attempts else ""),
            ("Latest attempt", _date(attempts[-1]["attempted_at"]) if attempts else ""),
            ("Due", "New" if question["stage"] == 0 else _date(question["due"]) + (" (suspended)" if question["suspended"] else "")),
            ("Interval", _schedule_text(question, "interval")),
            ("Stability", _schedule_text(question, "stability")),
            ("Difficulty", _schedule_text(question, "difficulty")),
            ("Retrievability", f"{r * 100:.0f}%" if r is not None else ""),
            ("Attempts", str(question["reps"])),
            ("Lapses", str(question["lapses"])),
            ("Average time", f"{sum(timed) / len(timed) / 1000:.1f} s" if timed else ""),
            ("Total time", f"{total_ms / 60000:.1f} min" if total_ms else ""),
            ("Question ID", str(question["id"]) + (f" (Anki {question['anki_id']})" if question["anki_id"] else "")),
        ]
        form = QFormLayout()
        for label, value in rows:
            if value:
                v = QLabel(value)
                v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                v.setWordWrap(True)
                form.addRow(QLabel(f"<b>{label}</b>"), v)
        table = QTableWidget(len(attempts), 5)
        table.setHorizontalHeaderLabels(["Date", "Rating", "Where", "Graded", "Time"])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # "review" is what the practice source was called before; the database migration renames it.
        where = {"chat": "Quiz in chat", "practice": "Practice", "review": "Practice", "anki": "Anki"}
        for i, x in enumerate(reversed(attempts)):
            for j, value in enumerate((
                time.strftime("%Y-%m-%d %H:%M", time.localtime(x["attempted_at"])), quiz.RATINGS.get(x["rating"], "?"),
                where.get(x["source"], x["source"]), x["verdict"] or "", f"{x['duration'] / 1000:.0f} s" if x["duration"] else "",
            )):
                table.setItem(i, j, QTableWidgetItem(value))
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel(f"<b>Attempts</b> ({len(attempts)})"))
        layout.addWidget(table, 1)
        layout.addWidget(close)
