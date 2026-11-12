"""SQLite persistence: library, chats, messages and their book snippets, notes, review cards."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import cardmodel

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    last_page INTEGER NOT NULL DEFAULT 0,
    zoom REAL NOT NULL DEFAULT 0,   -- 0 = fit width
    crop_margins INTEGER NOT NULL DEFAULT 0,
    show_toc INTEGER NOT NULL DEFAULT 1,
    right_panel TEXT NOT NULL DEFAULT 'chat', -- 'chat' | 'calc' | 'notes' | '' (collapsed)
    show_chat_list INTEGER NOT NULL DEFAULT 1,
    base_session_id TEXT,
    context_mode TEXT,              -- NULL (not prepared) | 'full' | 'partial'
    source_url TEXT,                -- web snapshots: the page they were saved from
    finished_at REAL,               -- NULL while still being read
    toc TEXT,                       -- JSON [[level, title, 0-based page], …] once known (srs.learn_toc)
    opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    session_id TEXT,
    model TEXT NOT NULL DEFAULT '', -- '' = the Claude Code default
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,             -- 'user' | 'assistant' | 'error'
    text TEXT NOT NULL,
    session_id TEXT,                -- assistant replies: the Claude session holding the reply,
    session_uuid TEXT,              -- and its last entry, to branch from (regenerate / edit)
    page_from INTEGER,              -- quiz requests on part of the book: its first and last
    page_to INTEGER,                -- 0-based pages
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,             -- 'text' | 'image'
    page INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    rects TEXT NOT NULL DEFAULT '[]' -- JSON [[x0,y0,x1,y1],...] in page points
);
CREATE TABLE IF NOT EXISTS calculations (
    id INTEGER PRIMARY KEY,
    book_id INTEGER REFERENCES books(id) ON DELETE CASCADE, -- NULL: the Review page's, for cards with no book
    expr TEXT NOT NULL,             -- plain expression, as the calculator evaluates it
    latex TEXT NOT NULL DEFAULT '', -- as typed in the math field ('' for rows from before it)
    result TEXT NOT NULL,           -- as displayed
    value TEXT NOT NULL,            -- int, float or "num/den" fraction, to restore `ans` and variables
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- never reused: a grading in flight finds its answer by id
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    quiz INTEGER NOT NULL,          -- which quiz fence of the reply (0-based)
    question INTEGER NOT NULL,      -- 0-based
    response TEXT NOT NULL,         -- JSON: {"choice": i} | {"value": bool} | {"latex": s} | {"text": s}
    status TEXT NOT NULL,           -- 'pending' (Claude is grading) | 'done' | 'error'
    verdict TEXT,                   -- 'correct' | 'partial' | 'incorrect' (NULL if ungraded)
    feedback TEXT NOT NULL DEFAULT '', -- Claude's, for written answers; the error for 'error'
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL,             -- the passage annotated
    rects TEXT NOT NULL DEFAULT '[]', -- JSON [[x0,y0,x1,y1],...] in page points, like snippets
    body TEXT NOT NULL DEFAULT '',  -- the note itself, Markdown with $math$
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,      -- full path, levels joined by '::' (as in Anki); parents have rows too
    created_at REAL NOT NULL
);
-- The fields and tags of one or more cards (a cloze's deletions, a reversed flashcard's sides):
-- never shown as such, a card's type is its row's kind.
CREATE TABLE IF NOT EXISTS card_notes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,             -- the card type, cardmodel.TYPES
    fields TEXT NOT NULL,           -- JSON object (cardmodel.py); for question types the question as quiz.py parses it
    tags TEXT NOT NULL DEFAULT '',  -- ' tag1 tag2 ' (cardmodel.join_tags)
    sort_text TEXT NOT NULL DEFAULT '',   -- plain text of the first field: the browser's Question column
    search_text TEXT NOT NULL DEFAULT '', -- plain text of every field, for search
    anki_id INTEGER,                -- the Anki note it was migrated from
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL REFERENCES card_notes(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL DEFAULT 0, -- which of the note's cards (cardmodel.card_ords)
    deck_id INTEGER NOT NULL REFERENCES decks(id),
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL, -- the book it came from, if any
    -- Quiz cards from a chat: the reply whose quiz holds the question; NULL once that reply is
    -- deleted (only cards already reviewed outlive it, see delete_messages_from).
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    quiz INTEGER,                   -- which quiz fence of the reply (0-based)
    question INTEGER,               -- 0-based
    title TEXT NOT NULL DEFAULT '', -- the quiz's title
    data TEXT NOT NULL,             -- JSON: the question review shows, made from the note (cardmodel.card_data)
    -- JSON: the version of a calculation problem with new numbers Claude wrote for its next review
    -- (variants.py), {"skip": true} if it has nothing to vary, or '' for none (yet).
    variant TEXT NOT NULL DEFAULT '',
    page INTEGER,                   -- 0-based page of the book it draws on, if known
    stage INTEGER NOT NULL DEFAULT 0, -- 0 new | fsrs.State: 1 learning, 2 review, 3 relearning
    fsrs TEXT NOT NULL DEFAULT '',  -- JSON fsrs.Card ('' while new)
    due REAL NOT NULL,              -- next due (new cards: when added)
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0,
    flag INTEGER NOT NULL DEFAULT 0, -- 0 none, 1-7 as Anki's flags (red, orange, green, blue, pink, turquoise, purple)
    stability REAL,                 -- from the fsrs card, for browsing and searching
    difficulty REAL,
    last_review REAL,
    anki_id INTEGER,                -- the Anki card it was migrated from
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (message_id, quiz, question)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL,        -- 1 again | 2 hard | 3 good | 4 easy
    source TEXT NOT NULL,           -- 'chat' (the first answer, in the quiz) | 'review' | 'anki' (migrated)
    response TEXT NOT NULL DEFAULT '', -- JSON, as in quiz_answers ('' if not answered, e.g. a flashcard)
    verdict TEXT,                   -- 'correct' | 'partial' | 'incorrect' (NULL if self-rated)
    reviewed_at REAL NOT NULL,
    duration INTEGER NOT NULL DEFAULT 0, -- ms spent on the card (0 if unknown)
    was_new INTEGER NOT NULL DEFAULT 0 -- the card was new (never reviewed, or forgotten) before this review
);
-- Make with Claude (deck_maker.py): the one draft being worked on (a deck Claude is making, or
-- existing cards it is improving), the requests it was sent in, and the cards it proposed.
CREATE TABLE IF NOT EXISTS deck_drafts (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,             -- 'new' (a deck from a description) | 'improve' (existing cards)
    request TEXT NOT NULL DEFAULT '{}', -- JSON {description, notes, files}: what the learner asked for
    deck TEXT NOT NULL DEFAULT '',  -- full name of the deck the new cards go into
    model TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_batches (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES deck_drafts(id) ON DELETE CASCADE,
    session_id TEXT,                -- Claude's session, resumed by follow-ups
    status TEXT NOT NULL,           -- 'pending' (Claude is working) | 'done' | 'error'
    error TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''   -- what Claude said to the learner in its last reply
);
CREATE TABLE IF NOT EXISTS draft_items (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES deck_drafts(id) ON DELETE CASCADE,
    batch_id INTEGER REFERENCES draft_batches(id) ON DELETE SET NULL,
    number INTEGER NOT NULL,        -- shown as #n and how Claude refers to it; never reused in a draft
    card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE, -- the existing card it rewrites; NULL: a new card
    status TEXT NOT NULL,           -- 'pending' | 'proposed' | 'kept' (no change) | 'error' | 'removed'
    proposal TEXT NOT NULL DEFAULT '', -- JSON [{kind, fields, tags}]: the card, or a split's several
    reason TEXT NOT NULL DEFAULT '',   -- Claude's note on it, or the error
    checked INTEGER NOT NULL DEFAULT 1 -- the learner wants it (unticked ones aren't added or applied)
);
-- Cards Make with Claude rewrote: what they were, so the rewrite can be undone.
CREATE TABLE IF NOT EXISTS card_rewrites (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    original TEXT NOT NULL,         -- JSON {note_id, kind, fields, ord, data, added}: added, the cards a split made
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Phone sync (phone_sync.py): events applied from the phone app, by their ids, and the batch
-- files they came in (the snapshot acknowledges those, so the phone can delete them).
CREATE TABLE IF NOT EXISTS phone_events (id TEXT PRIMARY KEY, device TEXT NOT NULL, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS phone_batches (
    device TEXT NOT NULL, batch TEXT NOT NULL, applied_at REAL NOT NULL, PRIMARY KEY (device, batch)
);
"""
# After the migrations, which may rebuild the tables these index.
INDEXES = """
CREATE INDEX IF NOT EXISTS cards_due ON cards (book_id, suspended, stage, due);
CREATE INDEX IF NOT EXISTS cards_deck ON cards (deck_id, suspended, stage, due);
CREATE INDEX IF NOT EXISTS cards_note ON cards (note_id);
CREATE INDEX IF NOT EXISTS reviews_card ON reviews (card_id);
CREATE INDEX IF NOT EXISTS reviews_time ON reviews (reviewed_at);
CREATE UNIQUE INDEX IF NOT EXISTS card_notes_anki ON card_notes (anki_id) WHERE anki_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS cards_anki ON cards (anki_id) WHERE anki_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_items_draft ON draft_items (draft_id, number);
CREATE INDEX IF NOT EXISTS card_rewrites_card ON card_rewrites (card_id);
"""


DEFAULT_DECK = "Default"
# The deck with id ? and every deck under it.
SUBTREE = ("SELECT d2.id FROM decks d1 JOIN decks d2 ON d2.id=d1.id OR substr(d2.name, 1, length(d1.name) + 2)=d1.name||'::' "
           "WHERE d1.id=?")


def deck_name(name: str) -> str:
    """A deck's full name tidied: levels stripped, empty ones dropped."""
    return "::".join(p.strip() for p in str(name).split("::") if p.strip())


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _chunks(items: list, size: int = 900):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fsrs_columns(fsrs_json: str) -> tuple[float | None, float | None, float | None]:
    """(stability, difficulty, last review time) of a stored fsrs.Card."""
    if not fsrs_json:
        return None, None, None
    from datetime import datetime
    d = json.loads(fsrs_json)
    last = d.get("last_review")
    return d.get("stability"), d.get("difficulty"), datetime.fromisoformat(last).timestamp() if last else None


def data_dir() -> Path:
    override = os.environ.get("STUDY_READER_DATA")
    base = Path(override) if override else Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    ) / "study-reader"
    base.mkdir(parents=True, exist_ok=True)
    return base


def media_dir() -> Path:
    """Images and other files card text refers to by name (e.g. migrated from Anki)."""
    path = data_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_url() -> str:
    """media_dir() as the base URL pages resolve card images against."""
    return media_dir().as_uri() + "/"


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else str(data_dir() / "study-reader.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Write-ahead logging: commits are cheap (a migration from Anki makes thousands) and a
        # crash can't corrupt the file; NORMAL sync is safe with it.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self.conn.executescript(INDEXES)

    def _migrate(self) -> None:
        book_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(books)")}
        if "crop_margins" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN crop_margins INTEGER NOT NULL DEFAULT 0")
        if "show_toc" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN show_toc INTEGER NOT NULL DEFAULT 1")
        if "right_panel" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN right_panel TEXT NOT NULL DEFAULT 'chat'")
        if "show_chat_list" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN show_chat_list INTEGER NOT NULL DEFAULT 1")
        # The book's Review panel is gone (the Review page took its place): chats instead, and the
        # calculator that sat under it alone.
        self.conn.execute("UPDATE books SET right_panel='chat' WHERE right_panel='review'")
        self.conn.execute("UPDATE books SET right_panel='calc' WHERE right_panel='calc+review'")
        if "source_url" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN source_url TEXT")
        if "finished_at" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN finished_at REAL")
        if "toc" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN toc TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "model" not in cols:
            # Existing chats keep the model that was selected globally before chats had their own.
            self.conn.execute("ALTER TABLE chats ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE chats SET model=COALESCE((SELECT value FROM settings WHERE key='model'), '')"
            )
        msg_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        for col in ("session_id", "session_uuid"):
            if col not in msg_cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        for col in ("page_from", "page_to"):
            if col not in msg_cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        calc_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(calculations)")}
        if "latex" not in calc_cols:
            self.conn.execute("ALTER TABLE calculations ADD COLUMN latex TEXT NOT NULL DEFAULT ''")
        if any(r["name"] == "book_id" and r["notnull"] for r in self.conn.execute("PRAGMA table_info(calculations)")):
            # book_id became optional (the Review page's history for cards with no book): rebuild
            # the table. Nothing refers to it, so foreign keys can stay on.
            self.conn.execute("DROP TABLE IF EXISTS calculations_new")  # left by an earlier attempt that failed
            new_schema = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS calculations ("):]
            new_schema = new_schema[:new_schema.index(";") + 1]
            with self.conn:
                self.conn.execute(new_schema.replace("calculations (", "calculations_new (", 1))
                self.conn.execute("INSERT INTO calculations_new (id, book_id, expr, latex, result, value, created_at) "
                                  "SELECT id, book_id, expr, latex, result, value, created_at FROM calculations")
                self.conn.execute("DROP TABLE calculations")
                self.conn.execute("ALTER TABLE calculations_new RENAME TO calculations")
        if "deck_id" not in book_cols:
            # The deck a book's new quiz cards go into (made when its first card is).
            self.conn.execute("ALTER TABLE books ADD COLUMN deck_id INTEGER REFERENCES decks(id) ON DELETE SET NULL")
        review_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(reviews)")}
        if "duration" not in review_cols:
            self.conn.execute("ALTER TABLE reviews ADD COLUMN duration INTEGER NOT NULL DEFAULT 0")
        if "was_new" not in review_cols:
            self.conn.execute("ALTER TABLE reviews ADD COLUMN was_new INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE reviews SET was_new=1 WHERE NOT EXISTS "
                              "(SELECT 1 FROM reviews p WHERE p.card_id=reviews.card_id AND p.id<reviews.id)")
        card_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(cards)")}
        if "note_id" not in card_cols:
            self._migrate_cards_to_notes()
        elif "variant" not in card_cols:
            self.conn.execute("ALTER TABLE cards ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
        # Cards made from a highlight and Claude Conversion are gone (their cards stay as they are).
        self.conn.execute("DROP INDEX IF EXISTS cards_passage")
        if "passage_id" in {r["name"] for r in self.conn.execute("PRAGMA table_info(cards)")}:
            self.conn.execute("ALTER TABLE cards DROP COLUMN passage_id")
        self.conn.execute("DROP TABLE IF EXISTS card_passages")
        self.conn.execute("DROP TABLE IF EXISTS conversions")
        self.conn.execute("DELETE FROM settings WHERE key='convert_instructions'")
        self._migrate_card_types()
        # Section tags were once made from contents titles padded with NULs, which the review
        # panel's section list couldn't hand back intact.
        padded = [r["id"] for r in self.conn.execute(
            "SELECT id FROM card_notes WHERE instr(CAST(tags AS BLOB), x'00') > 0")]
        if padded:
            self._retag(padded, lambda tags: [t for t in map(cardmodel.clean_tag, tags) if t])
        # A }} inside a cloze's math ({{c1::\(\sqrt{LC}}\)}}) once ended the deletion; remake
        # the cached sides of cards that were cut short.
        for r in self.conn.execute(r"SELECT c.id, c.ord, c.data, n.fields FROM cards c JOIN card_notes n ON n.id=c.note_id "
                                   r"WHERE n.kind='cloze' AND n.fields LIKE '%}}%\\)%'").fetchall():
            try:
                data = cardmodel.dumps(cardmodel.card_data("cloze", json.loads(r["fields"]), r["ord"]))
            except cardmodel.NoteError:
                continue
            if data != r["data"]:
                self.conn.execute("UPDATE cards SET data=? WHERE id=?", (data, r["id"]))

    def _migrate_card_types(self) -> None:
        """Card types were once kinds of notes: basic, reverse and quiz (every answered question,
        whatever its type; before that flashcards too), and written answers were "free"
        responses. Rows and cards keep their ids, schedules and times."""
        self.conn.execute("UPDATE card_notes SET kind='flashcard' WHERE kind='basic'")
        self.conn.execute("UPDATE card_notes SET kind='reversed' WHERE kind='reverse'")
        for r in self.conn.execute("SELECT id, fields FROM card_notes WHERE kind='quiz'").fetchall():
            q = json.loads(r["fields"])
            if q.get("type") == "free":
                q["type"] = "written"
            if q.get("type") not in (*cardmodel.QUESTION_TYPES, "flashcard"):
                q["type"] = "written"
            kind, fields = cardmodel.card_fields(q)
            self.conn.execute(
                "UPDATE card_notes SET kind=?, fields=?, sort_text=?, search_text=? WHERE id=?",
                (kind, cardmodel.dumps(fields), cardmodel.sort_text(kind, fields), cardmodel.search_text(kind, fields),
                 r["id"]),
            )
            for c in self.conn.execute("SELECT id, ord, data FROM cards WHERE note_id=?", (r["id"],)).fetchall():
                data = cardmodel.dumps(cardmodel.card_data(kind, fields, c["ord"]))
                if data != c["data"]:
                    self.conn.execute("UPDATE cards SET data=? WHERE id=?", (data, c["id"]))
        for r in self.conn.execute("SELECT id, data, variant FROM cards WHERE variant LIKE '%\"free\"%' "
                                   "OR data LIKE '%\"free\"%'").fetchall():
            change = {}
            for col in ("data", "variant"):
                try:
                    q = json.loads(r[col]) if r[col] else None
                except ValueError:
                    continue
                if isinstance(q, dict) and q.get("type") == "free":
                    change[col] = cardmodel.dumps({**q, "type": "written"})
            if change:
                self.conn.execute(f"UPDATE cards SET {', '.join(f'{k}=?' for k in change)} WHERE id=?",
                                  (*change.values(), r["id"]))

    def _migrate_cards_to_notes(self) -> None:
        """Cards from before decks and notes: each becomes a 'quiz' note in its book's deck. The
        table is rebuilt (book_id and the quiz position become optional), so foreign keys are
        off meanwhile, or dropping the old table would take the review log with it."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("DROP TABLE IF EXISTS cards_new")  # left by an earlier attempt that failed
            with self.conn:
                new_schema = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS cards ("):]
                new_schema = new_schema[:new_schema.index(");") + 2].replace(
                    "CREATE TABLE IF NOT EXISTS cards (", "CREATE TABLE cards_new (")
                self.conn.execute(new_schema)
                for c in self.conn.execute("SELECT * FROM cards ORDER BY id").fetchall():
                    data = json.loads(c["data"])
                    deck_id = self.book_deck(c["book_id"])
                    note_id = self._insert_note("quiz", data, "", c["created_at"])  # _migrate_card_types sorts it
                    stability, difficulty, last_review = _fsrs_columns(c["fsrs"])
                    self.conn.execute(
                        "INSERT INTO cards_new (id, note_id, ord, deck_id, book_id, message_id, quiz, question, title, "
                        "data, page, stage, fsrs, due, reps, lapses, suspended, stability, difficulty, last_review, "
                        "created_at, updated_at) VALUES (?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (c["id"], note_id, deck_id, c["book_id"], c["message_id"], c["quiz"], c["question"],
                         c["title"], c["data"], c["page"], c["stage"], c["fsrs"], c["due"], c["reps"], c["lapses"],
                         c["suspended"], stability, difficulty, last_review, c["created_at"], c["created_at"]),
                    )
                self.conn.execute("DROP TABLE cards")
                self.conn.execute("ALTER TABLE cards_new RENAME TO cards")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    # -- books ---------------------------------------------------------------
    def upsert_book(self, hash: str, path: str, title: str, page_count: int) -> sqlite3.Row:
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO books (hash, path, title, page_count, opened_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(hash) DO UPDATE SET path=excluded.path, page_count=excluded.page_count, "
                "opened_at=excluded.opened_at",
                (hash, path, title, page_count, now),
            )
        return self.conn.execute("SELECT * FROM books WHERE hash=?", (hash,)).fetchone()

    def replace_book_file(self, book_id: int, hash: str, path: str, title: str, page_count: int) -> None:
        """Point a book at a new version of its file (a fresh web snapshot), keeping its chats,
        notes and highlights. Its base session holds the old text, so it is dropped."""
        self.update_book(
            book_id, hash=hash, path=path, title=title, page_count=page_count,
            base_session_id=None, context_mode=None, opened_at=time.time(),
        )

    def book(self, book_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()

    def book_by_hash(self, hash: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM books WHERE hash=?", (hash,)).fetchone()

    def books_from_url(self, url: str) -> list[sqlite3.Row]:
        """Snapshots of a web page, newest first."""
        return self.conn.execute(
            "SELECT * FROM books WHERE source_url=? ORDER BY id DESC", (url,)
        ).fetchall()

    def recent_books(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM books ORDER BY opened_at DESC").fetchall()

    def set_finished(self, book_id: int, finished: bool) -> None:
        self.update_book(book_id, finished_at=time.time() if finished else None)

    def update_book(self, book_id: int, **fields) -> None:
        self._update("books", book_id, fields)

    def delete_book(self, book_id: int) -> None:
        """Remove a book with its chats. Its quiz cards that were never reviewed go too; reviewed
        ones stay in their decks, no longer linked to the book."""
        with self.conn:
            self._drop_new_cards("SELECT m.id FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.book_id=?", (book_id,))
            self.conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    # -- chats ---------------------------------------------------------------
    def create_chat(self, book_id: int, title: str, model: str = "") -> int:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO chats (book_id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                (book_id, title, model, now, now),
            )
        return cur.lastrowid

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()

    def chats(self, book_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM chats WHERE book_id=? ORDER BY updated_at DESC", (book_id,)
        ).fetchall()

    def update_chat(self, chat_id: int, **fields) -> None:
        self._update("chats", chat_id, fields)

    def delete_chat(self, chat_id: int) -> None:
        with self.conn:
            self._drop_new_cards("SELECT id FROM messages WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    # -- messages ------------------------------------------------------------
    def add_message(
        self, chat_id: int, role: str, text: str, snippets: list[dict] | None = None,
        session_id: str | None = None, session_uuid: str | None = None, pages: tuple[int, int] | None = None,
    ) -> int:
        """snippets: dicts with kind, page, text, image_path, rects. pages: the 0-based first and
        last pages a quiz request covers."""
        now = time.time()
        first, last = pages or (None, None)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO messages (chat_id, role, text, session_id, session_uuid, page_from, page_to, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, role, text, session_id, session_uuid, first, last, now),
            )
            for s in snippets or []:
                self.conn.execute(
                    "INSERT INTO snippets (message_id, kind, page, text, image_path, rects) VALUES (?,?,?,?,?,?)",
                    (cur.lastrowid, s["kind"], s["page"], s.get("text", ""), s.get("image_path", ""),
                     json.dumps(s.get("rects", []))),
                )
            self.conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
        return cur.lastrowid

    def delete_messages_from(self, chat_id: int, message_id: int) -> None:
        """Drop a message and everything after it in its chat (their snippets go too, and their
        quizzes' cards that were never reviewed; reviewed ones stay in the deck)."""
        with self.conn:
            self._drop_new_cards("SELECT id FROM messages WHERE chat_id=? AND id>=?", (chat_id, message_id))
            self.conn.execute("DELETE FROM messages WHERE chat_id=? AND id>=?", (chat_id, message_id))

    def messages(self, chat_id: int) -> list[dict]:
        out = []
        for m in self.conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)):
            d = dict(m)
            d["snippets"] = [
                {**dict(s), "rects": json.loads(s["rects"])}
                for s in self.conn.execute("SELECT * FROM snippets WHERE message_id=? ORDER BY id", (m["id"],))
            ]
            out.append(d)
        return out

    def message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def snippet(self, snippet_id: int) -> dict | None:
        s = self.conn.execute("SELECT * FROM snippets WHERE id=?", (snippet_id,)).fetchone()
        return {**dict(s), "rects": json.loads(s["rects"])} if s else None

    def book_snippets(self, book_id: int) -> list[dict]:
        """Every snippet asked about in any of the book's chats, with its chat id."""
        rows = self.conn.execute(
            "SELECT s.*, m.chat_id FROM snippets s JOIN messages m ON m.id=s.message_id "
            "JOIN chats c ON c.id=m.chat_id WHERE c.book_id=? ORDER BY s.id",
            (book_id,),
        )
        return [{**dict(s), "rects": json.loads(s["rects"])} for s in rows]

    # -- quizzes -------------------------------------------------------------
    def save_quiz_answer(
        self, message_id: int, quiz: int, question: int, response: str, status: str,
        verdict: str | None = None, feedback: str = "",
    ) -> int:
        """Replaces any earlier answer to the question, under a new id (so a grading still
        running for the old one can't land on it)."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM quiz_answers WHERE message_id=? AND quiz=? AND question=?", (message_id, quiz, question)
            )
            cur = self.conn.execute(
                "INSERT INTO quiz_answers (message_id, quiz, question, response, status, verdict, feedback, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (message_id, quiz, question, response, status, verdict, feedback, time.time()),
            )
        return cur.lastrowid

    def quiz_answer(self, answer_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM quiz_answers WHERE id=?", (answer_id,)).fetchone()

    def update_quiz_answer(self, answer_id: int, **fields) -> None:
        self._update("quiz_answers", answer_id, fields)

    def quiz_answers(self, chat_id: int) -> dict[int, list[sqlite3.Row]]:
        """The chat's quiz answers by message id."""
        out: dict[int, list[sqlite3.Row]] = {}
        for r in self.conn.execute(
            "SELECT a.* FROM quiz_answers a JOIN messages m ON m.id=a.message_id WHERE m.chat_id=? "
            "ORDER BY a.quiz, a.question", (chat_id,),
        ):
            out.setdefault(r["message_id"], []).append(r)
        return out

    def clear_quiz_answers(self, message_id: int, quiz: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM quiz_answers WHERE message_id=? AND quiz=?", (message_id, quiz))

    # -- notes ---------------------------------------------------------------
    def add_note(self, book_id: int, page: int, text: str, rects: list, body: str = "") -> int:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notes (book_id, page, text, rects, body, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (book_id, page, text, json.dumps(rects), body, now, now),
            )
        return cur.lastrowid

    def note(self, note_id: int) -> dict | None:
        n = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return {**dict(n), "rects": json.loads(n["rects"])} if n else None

    def notes(self, book_id: int) -> list[dict]:
        """The book's notes in reading order."""
        rows = self.conn.execute("SELECT * FROM notes WHERE book_id=? ORDER BY page, id", (book_id,))
        out = [{**dict(n), "rects": json.loads(n["rects"])} for n in rows]
        out.sort(key=lambda n: (n["page"], min((r[1] for r in n["rects"]), default=0.0), n["id"]))
        return out

    def update_note(self, note_id: int, body: str) -> None:
        self._update("notes", note_id, {"body": body, "updated_at": time.time()})

    def delete_note(self, note_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

    # -- calculator -----------------------------------------------------------
    # A book_id of None is the Review page's shared history, for cards with no book.
    def add_calculation(self, book_id: int | None, expr: str, result: str, value: str, latex: str = "") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO calculations (book_id, expr, latex, result, value, created_at) VALUES (?,?,?,?,?,?)",
                (book_id, expr, latex, result, value, time.time()),
            )
        return cur.lastrowid

    def calculations(self, book_id: int | None) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM calculations WHERE book_id IS ? ORDER BY id", (book_id,)).fetchall()

    def delete_calculation(self, calc_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM calculations WHERE id=?", (calc_id,))

    def clear_calculations(self, book_id: int | None) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM calculations WHERE book_id IS ?", (book_id,))

    # -- decks -----------------------------------------------------------------
    def decks(self) -> list[sqlite3.Row]:
        """Every deck, parents before their children."""
        return self.conn.execute("SELECT * FROM decks ORDER BY name COLLATE NOCASE").fetchall()

    def deck(self, deck_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()

    def deck_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM decks WHERE name=?", (deck_name(name),)).fetchone()

    def ensure_deck(self, name: str) -> int:
        """The deck with this full name ('A::B'), made along with its parents if need be."""
        with self.conn:
            return self._ensure_deck(name)

    def _ensure_deck(self, name: str) -> int:
        name = deck_name(name)
        parts = name.split("::")
        deck_id = 0
        for i in range(len(parts)):
            path = "::".join(parts[:i + 1])
            row = self.conn.execute("SELECT id FROM decks WHERE name=?", (path,)).fetchone()
            if row:
                deck_id = row["id"]
            else:
                deck_id = self.conn.execute(
                    "INSERT INTO decks (name, created_at) VALUES (?,?)", (path, time.time())
                ).lastrowid
        return deck_id

    def deck_subtree(self, deck_id: int) -> list[int]:
        """The deck and all decks under it."""
        return [r[0] for r in self.conn.execute(SUBTREE, (deck_id,))]

    def book_deck(self, book_id: int | None) -> int:
        """The deck a book's new quiz cards go into, made (named after the book) if it has none."""
        book = self.book(book_id) if book_id is not None else None
        if book is None:
            return self._ensure_deck(DEFAULT_DECK)
        if book["deck_id"] and self.deck(book["deck_id"]):
            return book["deck_id"]
        base = deck_name(book["title"].replace("::", ":")) or "Book"
        name, n = base, 1
        while (row := self.deck_by_name(name)) and self.conn.execute(
            "SELECT 1 FROM books WHERE deck_id=? AND id!=?", (row["id"], book_id)
        ).fetchone():
            n += 1
            name = f"{base} ({n})"
        deck_id = self._ensure_deck(name)
        self.conn.execute("UPDATE books SET deck_id=? WHERE id=?", (deck_id, book_id))
        return deck_id

    def rename_deck(self, deck_id: int, name: str) -> int:
        """Rename (or move, by changing the path) a deck along with the decks under it. Renaming
        onto an existing deck merges into it. Returns the deck's id afterwards."""
        old = self.deck(deck_id)
        name = deck_name(name)
        if old is None or name == old["name"]:
            return deck_id
        if name.startswith(old["name"] + "::"):
            raise ValueError("A deck can't be moved into itself.")
        with self.conn:
            for row in sorted(self._subtree_rows(deck_id), key=lambda r: len(r["name"])):
                target = name + row["name"][len(old["name"]):]
                existing = self.conn.execute("SELECT id FROM decks WHERE name=?", (target,)).fetchone()
                if existing:
                    self.conn.execute("UPDATE cards SET deck_id=? WHERE deck_id=?", (existing["id"], row["id"]))
                    self.conn.execute("UPDATE books SET deck_id=? WHERE deck_id=?", (existing["id"], row["id"]))
                    self.conn.execute("DELETE FROM decks WHERE id=?", (row["id"],))
                else:
                    if "::" in target:
                        self._ensure_deck(target.rsplit("::", 1)[0])
                    self.conn.execute("UPDATE decks SET name=? WHERE id=?", (target, row["id"]))
        return self.deck_by_name(name)["id"]

    def _subtree_rows(self, deck_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM decks WHERE id IN ({SUBTREE})", (deck_id,)).fetchall()

    def delete_deck(self, deck_id: int) -> int:
        """Delete a deck, the decks under it, and all their cards. Returns how many cards went."""
        ids = self.deck_subtree(deck_id)
        with self.conn:
            marks = ",".join("?" * len(ids))
            notes = [r[0] for r in self.conn.execute(f"SELECT DISTINCT note_id FROM cards WHERE deck_id IN ({marks})", ids)]
            n = self.conn.execute(f"DELETE FROM cards WHERE deck_id IN ({marks})", ids).rowcount
            self.conn.execute(f"DELETE FROM decks WHERE id IN ({marks})", ids)
            self._drop_orphan_notes(notes)
        return n

    # -- card notes ------------------------------------------------------------
    def _insert_note(self, kind: str, fields: dict, tags: str, now: float, anki_id: int | None = None,
                     created_at: float | None = None) -> int:
        return self.conn.execute(
            "INSERT INTO card_notes (kind, fields, tags, sort_text, search_text, anki_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, cardmodel.dumps(fields), tags, cardmodel.sort_text(kind, fields),
             cardmodel.search_text(kind, fields), anki_id, created_at or now, now),
        ).lastrowid

    def add_card_note(
        self, kind: str, fields: dict, deck_id: int, tags=(), book_id: int | None = None, page: int | None = None,
        now: float | None = None, anki_id: int | None = None, created_at: float | None = None, title: str = "",
    ) -> int:
        """A note and its cards (new); returns the note's id. Raises cardmodel.NoteError if it
        makes no card."""
        cardmodel.check(kind, fields)
        now = time.time() if now is None else now
        with self.conn:
            note_id = self._insert_note(kind, fields, cardmodel.join_tags(tags), now, anki_id, created_at)
            for ord in cardmodel.card_ords(kind, fields):
                self._insert_card(note_id, ord, deck_id, kind, fields, now, book_id=book_id, page=page,
                                  created_at=created_at, title=title)
        return note_id

    def _insert_card(self, note_id: int, ord: int, deck_id: int, kind: str, fields: dict, now: float,
                     created_at: float | None = None, **cols) -> int:
        cols = {k: v for k, v in cols.items() if v is not None}
        names = ["note_id", "ord", "deck_id", "data", "due", "created_at", "updated_at", *cols]
        values = [note_id, ord, deck_id, cardmodel.dumps(cardmodel.card_data(kind, fields, ord)),
                  created_at or now, created_at or now, now, *cols.values()]
        return self.conn.execute(
            f"INSERT INTO cards ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values
        ).lastrowid

    def ensure_card(self, note_id: int, ord: int, deck_id: int, now: float | None = None) -> int:
        """The note's card `ord`, made (new, in this deck) if the note doesn't have it."""
        row = self.conn.execute("SELECT id FROM cards WHERE note_id=? AND ord=?", (note_id, ord)).fetchone()
        if row:
            return row["id"]
        note = self.card_note(note_id)
        with self.conn:
            return self._insert_card(note_id, ord, deck_id, note["kind"], json.loads(note["fields"]),
                                     time.time() if now is None else now)

    def card_note(self, note_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM card_notes WHERE id=?", (note_id,)).fetchone()

    def note_by_anki_id(self, anki_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM card_notes WHERE anki_id=?", (anki_id,)).fetchone()

    def note_cards(self, note_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM cards WHERE note_id=? ORDER BY ord", (note_id,)).fetchall()

    def update_card_note(self, note_id: int, fields: dict | None = None, tags=None, kind: str | None = None,
                         now: float | None = None) -> None:
        """Change a note's fields (and kind) and/or tags. Its cards are remade from the fields. A
        cloze number (or reverse side) the edit introduces gets a new card; one the note made
        before but no longer has a card for (deleted, left behind in Anki) isn't
        brought back. A card whose deletion the edit removes goes if it was never reviewed;
        reviewed ones stay, showing what they did, so an edit (or a pause while retyping) can't
        lose review history."""
        note = self.card_note(note_id)
        if note is None:
            return
        now = time.time() if now is None else now
        kind = kind or note["kind"]
        with self.conn:
            if tags is not None:
                self.conn.execute("UPDATE card_notes SET tags=?, updated_at=? WHERE id=?",
                                  (cardmodel.join_tags(tags), now, note_id))
            if fields is None:
                return
            cardmodel.check(kind, fields)
            before = set(cardmodel.card_ords(note["kind"], json.loads(note["fields"])))
            self.conn.execute(
                "UPDATE card_notes SET kind=?, fields=?, sort_text=?, search_text=?, updated_at=? WHERE id=?",
                (kind, cardmodel.dumps(fields), cardmodel.sort_text(kind, fields),
                 cardmodel.search_text(kind, fields), now, note_id),
            )
            cards = {c["ord"]: c for c in self.note_cards(note_id)}
            ords = cardmodel.card_ords(kind, fields)
            first = next(iter(cards.values()), None)
            for ord in ords:
                data = cardmodel.dumps(cardmodel.card_data(kind, fields, ord))
                if ord in cards:  # a version with new numbers of what it was before is out of date
                    self.conn.execute("UPDATE cards SET variant=CASE WHEN data=? THEN variant ELSE '' END, data=?, "
                                      "updated_at=? WHERE id=?", (data, data, now, cards[ord]["id"]))
                elif ord not in before:
                    self._insert_card(
                        note_id, ord, first["deck_id"] if first else self._ensure_deck(DEFAULT_DECK), kind, fields,
                        now, book_id=first["book_id"] if first else None, page=first["page"] if first else None,
                    )
            gone = [c["id"] for o, c in cards.items() if o not in ords and not self.conn.execute(
                "SELECT 1 FROM reviews WHERE card_id=? LIMIT 1", (c["id"],)).fetchone()]
            if gone:
                self.conn.execute(f"DELETE FROM cards WHERE id IN ({','.join('?' * len(gone))})", gone)

    def move_card_to_note(self, card_id: int, kind: str, fields: dict, now: float | None = None) -> int:
        """Give one card new content in a card_notes row of its own, as the first card the fields
        make (the card keeps its deck, schedule and history, and its tags); the old row goes if no
        other card is left on it. Returns the new row's id."""
        cardmodel.check(kind, fields)
        card = self.card(card_id)
        old = self.card_note(card["note_id"])
        now = time.time() if now is None else now
        with self.conn:
            note_id = self._insert_note(kind, fields, old["tags"] if old else "", now,
                                        created_at=old["created_at"] if old else None)
            self.move_card(card_id, note_id, cardmodel.card_ords(kind, fields)[0], now)
        return note_id

    def move_card(self, card_id: int, note_id: int, ord: int, now: float | None = None) -> None:
        """Put a card on another card_notes row as its card `ord` (keeping its schedule and
        history); its old row goes if no other card is left on it."""
        card, note = self.card(card_id), self.card_note(note_id)
        data = cardmodel.dumps(cardmodel.card_data(note["kind"], json.loads(note["fields"]), ord))
        with self.conn:
            self.conn.execute("UPDATE cards SET note_id=?, ord=?, data=?, variant='', updated_at=? WHERE id=?",
                              (note_id, ord, data, time.time() if now is None else now, card_id))
            self._drop_orphan_notes([card["note_id"]])

    def _drop_orphan_notes(self, note_ids) -> None:
        """Delete those of these notes that no card is left on."""
        for chunk in _chunks(list(note_ids)):
            self.conn.execute(f"DELETE FROM card_notes WHERE id IN ({','.join('?' * len(chunk))}) "
                              "AND NOT EXISTS (SELECT 1 FROM cards WHERE cards.note_id=card_notes.id)", chunk)

    # -- tags ------------------------------------------------------------------
    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for (t,) in self.conn.execute("SELECT DISTINCT tags FROM card_notes WHERE tags!=''"):
            tags.update(t.split())
        return sorted(tags, key=str.lower)

    def add_tags(self, note_ids, tags) -> None:
        self._retag(note_ids, lambda old: old + [t for t in tags if t not in old])

    def remove_tags(self, note_ids, tags) -> None:
        drop = {t.lower() for t in tags}
        self._retag(note_ids, lambda old: [t for t in old if t.lower() not in drop])

    def rename_tag(self, old: str, new: str) -> None:
        """Rename a tag and the tags under it (old::child)."""
        rows = self.conn.execute("SELECT id, tags FROM card_notes WHERE tags LIKE ? ESCAPE '\\'",
                                 (f"% {_like(old)}%",)).fetchall()

        def renamed(t: str) -> str:
            if t.lower() == old.lower():
                return new
            if t.lower().startswith(old.lower() + "::"):
                return new + t[len(old):] if new else ""
            return t
        self._retag([r["id"] for r in rows], lambda tags: [renamed(t) for t in tags if renamed(t)])

    def _retag(self, note_ids, change) -> None:
        now = time.time()
        with self.conn:
            for chunk in _chunks(list(note_ids)):
                for r in self.conn.execute(
                    f"SELECT id, tags FROM card_notes WHERE id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall():
                    tags = change(r["tags"].split())
                    joined = cardmodel.join_tags(tags)
                    if joined != r["tags"]:
                        self.conn.execute("UPDATE card_notes SET tags=?, updated_at=? WHERE id=?", (joined, now, r["id"]))

    # -- review cards ----------------------------------------------------------
    def _drop_new_cards(self, messages_sql: str, args: tuple) -> None:
        where = f"message_id IN ({messages_sql}) AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.card_id=cards.id)"
        notes = [r[0] for r in self.conn.execute(f"SELECT DISTINCT note_id FROM cards WHERE {where}", args)]
        self.conn.execute(f"DELETE FROM cards WHERE {where}", args)
        self._drop_orphan_notes(notes)

    def add_card(
        self, book_id: int, message_id: int | None, quiz: int, question: int, title: str, data: str,
        page: int | None, now: float, tags=(),
    ) -> int | None:
        """A new card for a quiz question, in the book's deck; None if the question has one already."""
        if message_id is not None and self.card_for(message_id, quiz, question) is not None:
            return None
        kind, fields = cardmodel.card_fields(json.loads(data))
        with self.conn:
            deck_id = self.book_deck(book_id)
            note_id = self._insert_note(kind, fields, cardmodel.join_tags(tags), now)
            return self._insert_card(note_id, 0, deck_id, kind, fields, now, book_id=book_id, message_id=message_id,
                                     quiz=quiz, question=question, title=title, page=page)

    def relink_card(self, card_id: int, message_id: int, quiz: int, question: int, title: str,
                    page: int | None, now: float) -> None:
        """Hand a quiz question's card to the question that revises it, in another reply."""
        with self.conn:
            self.conn.execute(
                "UPDATE cards SET message_id=?, quiz=?, question=?, title=?, page=COALESCE(?, page), updated_at=? "
                "WHERE id=?", (message_id, quiz, question, title, page, now, card_id),
            )

    def drop_new_quiz_cards(self, message_id: int, quiz: int) -> None:
        """Delete a quiz's cards that were never reviewed (it was revised without them)."""
        where = "message_id=? AND quiz=? AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.card_id=cards.id)"
        with self.conn:
            notes = [r[0] for r in self.conn.execute(f"SELECT DISTINCT note_id FROM cards WHERE {where}", (message_id, quiz))]
            self.conn.execute(f"DELETE FROM cards WHERE {where}", (message_id, quiz))
            self._drop_orphan_notes(notes)

    def card(self, card_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()

    def card_for(self, message_id: int, quiz: int, question: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cards WHERE message_id=? AND quiz=? AND question=?", (message_id, quiz, question)
        ).fetchone()

    def card_by_anki_id(self, anki_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cards WHERE anki_id=?", (anki_id,)).fetchone()

    def quiz_cards(self, message_id: int, quiz: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cards WHERE message_id=? AND quiz=? ORDER BY question", (message_id, quiz)
        ).fetchall()

    def _scope(self, book_id: int | None, deck_id: int | None, tag: str | None = None) -> tuple[str, tuple]:
        sql, args = "", ()
        if book_id is not None:
            sql, args = "book_id=? AND ", (book_id,)
        if deck_id is not None:
            sql, args = sql + f"deck_id IN ({SUBTREE}) AND ", args + (deck_id,)
        if tag is not None:  # the tag or a tag under it (tag::child)
            sql += ("note_id IN (SELECT id FROM card_notes WHERE tags LIKE ? ESCAPE '\\' "
                    "OR tags LIKE ? ESCAPE '\\') AND ")
            args += (f"% {_like(tag)} %", f"% {_like(tag)}::%")
        return sql, args

    def cards(self, book_id: int | None = None, where: str = "1", args: tuple = (), order: str = "id",
              limit: int = -1, deck_id: int | None = None, tag: str | None = None) -> list[sqlite3.Row]:
        """Cards of a book, deck or tag (or of every one), filtered by a SQL condition."""
        scope, scope_args = self._scope(book_id, deck_id, tag)
        return self.conn.execute(
            f"SELECT * FROM cards WHERE {scope}({where}) ORDER BY {order} LIMIT ?", scope_args + args + (limit,),
        ).fetchall()

    def count_cards(self, book_id: int | None = None, where: str = "1", args: tuple = (),
                    deck_id: int | None = None, tag: str | None = None) -> int:
        scope, scope_args = self._scope(book_id, deck_id, tag)
        return self.conn.execute(f"SELECT COUNT(*) FROM cards WHERE {scope}({where})", scope_args + args).fetchone()[0]

    def record_review(
        self, card_id: int, rating: int, source: str, response: str, verdict: str | None, at: float,
        duration: int = 0, was_new: bool = False, **card_fields,
    ) -> None:
        """Log a review and store the card's new schedule (together, in one transaction)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO reviews (card_id, rating, source, response, verdict, reviewed_at, duration, was_new) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (card_id, rating, source, response, verdict, at, duration, int(was_new)),
            )
            if card_fields:
                self.conn.execute(f"UPDATE cards SET {', '.join(f'{k}=?' for k in card_fields)} WHERE id=?",
                                  (*card_fields.values(), card_id))

    def reviews(self, card_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reviews WHERE card_id=? ORDER BY reviewed_at, id", (card_id,)).fetchall()

    def new_cards_reviewed_since(self, since: float) -> int:
        """New cards (never reviewed, or forgotten) introduced in review (not the chat) since `since`."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE source='review' AND was_new=1 AND reviewed_at>=?", (since,),
        ).fetchone()[0]

    def update_card(self, card_id: int, **fields) -> None:
        self._update("cards", card_id, fields)

    def update_cards(self, card_ids, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            for chunk in _chunks(list(card_ids)):
                self.conn.execute(f"UPDATE cards SET {cols} WHERE id IN ({','.join('?' * len(chunk))})",
                                  (*fields.values(), *chunk))

    def set_quiz_suspended(self, message_id: int, quiz: int, suspended: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE cards SET suspended=? WHERE message_id=? AND quiz=?", (int(suspended), message_id, quiz)
            )

    def delete_card(self, card_id: int) -> None:
        self.delete_cards([card_id])

    def delete_cards(self, card_ids) -> None:
        card_ids = list(card_ids)
        with self.conn:
            notes = self.note_ids(card_ids)
            for chunk in _chunks(card_ids):
                self.conn.execute(f"DELETE FROM cards WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            self._drop_orphan_notes(notes)

    def any_card(self, card_ids, where: str, args=()) -> bool:
        """Whether any of these cards meets a condition on the cards table."""
        return any(self.conn.execute(
            f"SELECT 1 FROM cards WHERE id IN ({','.join('?' * len(chunk))}) AND {where} LIMIT 1", (*chunk, *args)
        ).fetchone() for chunk in _chunks(list(card_ids)))

    def any_note(self, note_ids, where: str, args=()) -> bool:
        """Whether any of these notes meets a condition on the card_notes table."""
        return any(self.conn.execute(
            f"SELECT 1 FROM card_notes WHERE id IN ({','.join('?' * len(chunk))}) AND {where} LIMIT 1", (*chunk, *args)
        ).fetchone() for chunk in _chunks(list(note_ids)))

    def note_ids(self, card_ids) -> list[int]:
        """The card_notes ids of these cards."""
        out: list[int] = []
        for chunk in _chunks(list(card_ids)):
            out += [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT note_id FROM cards WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
        return list(dict.fromkeys(out))

    # -- Make with Claude --------------------------------------------------------
    def draft(self) -> sqlite3.Row | None:
        """The draft being worked on, if any (there is at most one)."""
        return self.conn.execute("SELECT * FROM deck_drafts ORDER BY id DESC LIMIT 1").fetchone()

    def add_draft(self, mode: str, request: dict, deck: str, model: str) -> int:
        """A new draft, in place of any other."""
        with self.conn:
            self.conn.execute("DELETE FROM deck_drafts")
            return self.conn.execute(
                "INSERT INTO deck_drafts (mode, request, deck, model, created_at) VALUES (?,?,?,?,?)",
                (mode, json.dumps(request, ensure_ascii=False), deck, model, time.time()),
            ).lastrowid

    def update_draft(self, draft_id: int, **fields) -> None:
        self._update("deck_drafts", draft_id, fields)

    def delete_draft(self, draft_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM deck_drafts WHERE id=?", (draft_id,))

    def add_draft_batch(self, draft_id: int) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO draft_batches (draft_id, status) VALUES (?, 'pending')",
                                     (draft_id,)).lastrowid

    def draft_batch(self, batch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM draft_batches WHERE id=?", (batch_id,)).fetchone()

    def draft_batches(self, draft_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM draft_batches WHERE draft_id=? ORDER BY id", (draft_id,)).fetchall()

    def update_draft_batch(self, batch_id: int, **fields) -> None:
        self._update("draft_batches", batch_id, fields)

    def add_draft_item(self, draft_id: int, batch_id: int | None, card_id: int | None = None, status: str = "pending",
                       proposal: str = "", reason: str = "") -> int:
        """A card in the draft, numbered after the draft's others."""
        with self.conn:
            number = self.conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM draft_items WHERE draft_id=?",
                                       (draft_id,)).fetchone()[0]
            return self.conn.execute(
                "INSERT INTO draft_items (draft_id, batch_id, number, card_id, status, proposal, reason) "
                "VALUES (?,?,?,?,?,?,?)", (draft_id, batch_id, number, card_id, status, proposal, reason),
            ).lastrowid

    def draft_item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM draft_items WHERE id=?", (item_id,)).fetchone()

    def draft_items(self, draft_id: int, batch_id: int | None = None) -> list[sqlite3.Row]:
        """The draft's cards (or one batch's), in order, without those Claude removed."""
        if batch_id is None:
            return self.conn.execute("SELECT * FROM draft_items WHERE draft_id=? AND status!='removed' ORDER BY number",
                                     (draft_id,)).fetchall()
        return self.conn.execute("SELECT * FROM draft_items WHERE draft_id=? AND batch_id=? AND status!='removed' "
                                 "ORDER BY number", (draft_id, batch_id)).fetchall()

    def update_draft_item(self, item_id: int, **fields) -> None:
        self._update("draft_items", item_id, fields)

    def set_draft_items_checked(self, draft_id: int, checked: bool) -> None:
        with self.conn:
            self.conn.execute("UPDATE draft_items SET checked=? WHERE draft_id=?", (int(checked), draft_id))

    def add_card_rewrite(self, card_id: int, original: dict) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO card_rewrites (card_id, original, created_at) VALUES (?,?,?)",
                                     (card_id, json.dumps(original, ensure_ascii=False), time.time())).lastrowid

    def card_rewrite(self, card_id: int) -> sqlite3.Row | None:
        """The card's last rewrite by Claude, if any (to undo it)."""
        return self.conn.execute("SELECT * FROM card_rewrites WHERE card_id=? ORDER BY id DESC LIMIT 1",
                                 (card_id,)).fetchone()

    def delete_card_rewrite(self, rewrite_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM card_rewrites WHERE id=?", (rewrite_id,))

    # -- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _update(self, table: str, row_id: int, fields: dict) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
