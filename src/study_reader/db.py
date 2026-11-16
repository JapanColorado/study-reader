"""SQLite persistence: library, chats, messages and their book snippets, notes, practice questions."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import question_model

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
    toc TEXT,                       -- JSON [[level, title, 0-based page], …] once known (practice.learn_toc)
    opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    book_id INTEGER REFERENCES books(id) ON DELETE CASCADE, -- NULL: the Practice page's, about a question with no book
    question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL, -- started beside this question on the Practice page
    title TEXT NOT NULL,
    session_id TEXT,
    model TEXT NOT NULL DEFAULT '', -- '' = the Claude Code default
    throwaway INTEGER NOT NULL DEFAULT 0, -- 1: not kept (deleted when left, and at startup)
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
    stopped INTEGER NOT NULL DEFAULT 0, -- assistant replies the user stopped partway (text is what arrived)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,             -- 'text' | 'image'
    page INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    rects TEXT NOT NULL DEFAULT '[]' -- JSON [[x0,y0,x1,y1],...] in page points; see passage_pages
);
CREATE TABLE IF NOT EXISTS calculations (
    id INTEGER PRIMARY KEY,
    book_id INTEGER REFERENCES books(id) ON DELETE CASCADE, -- NULL: the Practice page's, for questions with no book
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
    feedback TEXT NOT NULL DEFAULT '', -- Claude's, for short answers; the error for 'error'
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
-- The fields and tags of one or more questions (a fill-in-the-blank's blanks, a both-ways
-- self-check's sides): never shown as such, a question's type is its row's kind.
CREATE TABLE IF NOT EXISTS question_groups (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,             -- the question type, question_model.TYPES
    fields TEXT NOT NULL,           -- JSON object (question_model.py); for question types the question as quiz.py parses it
    tags TEXT NOT NULL DEFAULT '',  -- ' tag1 tag2 ' (question_model.join_tags)
    sort_text TEXT NOT NULL DEFAULT '',   -- plain text of the first field: the browser's Question column
    search_text TEXT NOT NULL DEFAULT '', -- plain text of every field, for search
    anki_id INTEGER,                -- the Anki note it was migrated from
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES question_groups(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL DEFAULT 0, -- which of the group's questions (question_model.question_ords)
    deck_id INTEGER NOT NULL REFERENCES decks(id),
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL, -- the book it came from, if any
    -- Questions from a chat's quiz: the reply whose quiz holds it; NULL once that reply is
    -- deleted (only questions already attempted outlive it, see delete_messages_from).
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    quiz INTEGER,                   -- which quiz fence of the reply (0-based)
    position INTEGER,               -- its place in the quiz (0-based)
    title TEXT NOT NULL DEFAULT '', -- the quiz's title
    data TEXT NOT NULL,             -- JSON: what practice shows, made from the group (question_model.question_data)
    -- JSON: the version of a calculation problem with new numbers Claude wrote for its next attempt
    -- (variants.py), {"skip": true} if it has nothing to vary, or '' for none (yet).
    variant TEXT NOT NULL DEFAULT '',
    page INTEGER,                   -- 0-based page of the book it draws on, if known
    stage INTEGER NOT NULL DEFAULT 0, -- 0 new | fsrs.State: 1 learning, 2 learned (State.Review), 3 relearning
    fsrs TEXT NOT NULL DEFAULT '',  -- JSON fsrs.Card ('' while new)
    due REAL NOT NULL,              -- next due (new questions: when added)
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0,
    flag INTEGER NOT NULL DEFAULT 0, -- 0 none, 1-7 as Anki's flags (red, orange, green, blue, pink, turquoise, purple)
    stability REAL,                 -- from the fsrs.Card, for browsing and searching
    difficulty REAL,
    last_attempt REAL,
    anki_id INTEGER,                -- the Anki card it was migrated from
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (message_id, quiz, position)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL,        -- 1 missed | 2 shaky | 3 got it | 4 easy
    source TEXT NOT NULL,           -- 'chat' (the first answer, in the quiz) | 'practice' | 'anki' (migrated)
    response TEXT NOT NULL DEFAULT '', -- JSON, as in quiz_answers ('' if not answered, e.g. a self-check)
    verdict TEXT,                   -- 'correct' | 'partial' | 'incorrect' (NULL if self-rated)
    attempted_at REAL NOT NULL,
    duration INTEGER NOT NULL DEFAULT 0, -- ms spent on the question (0 if unknown)
    was_new INTEGER NOT NULL DEFAULT 0 -- the question was new (never attempted, or forgotten) before this attempt
);
-- Make with Claude (deck_maker.py): the one draft being worked on (a deck Claude is making, or
-- existing questions it is improving), the requests it was sent in, and the questions it proposed.
CREATE TABLE IF NOT EXISTS deck_drafts (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,             -- 'new' (a deck from a description) | 'improve' (existing questions)
    request TEXT NOT NULL DEFAULT '{}', -- JSON {description, notes, files}: what the learner asked for
    deck TEXT NOT NULL DEFAULT '',  -- full name of the deck the new questions go into
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
    question_id INTEGER REFERENCES questions(id) ON DELETE CASCADE, -- the existing question it rewrites; NULL: a new one
    status TEXT NOT NULL,           -- 'pending' | 'proposed' | 'kept' (no change) | 'error' | 'removed'
    proposal TEXT NOT NULL DEFAULT '', -- JSON [{kind, fields, tags}]: the question, or a split's several
    reason TEXT NOT NULL DEFAULT '',   -- Claude's note on it, or the error
    checked INTEGER NOT NULL DEFAULT 1 -- the learner wants it (unticked ones aren't added or applied)
);
-- Questions Make with Claude rewrote: what they were, so the rewrite can be undone.
CREATE TABLE IF NOT EXISTS question_rewrites (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    original TEXT NOT NULL,         -- JSON {group_id, kind, fields, ord, data, added}: added, the questions a split made
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
CREATE INDEX IF NOT EXISTS questions_due ON questions (book_id, suspended, stage, due);
CREATE INDEX IF NOT EXISTS questions_deck ON questions (deck_id, suspended, stage, due);
CREATE INDEX IF NOT EXISTS questions_group ON questions (group_id);
CREATE INDEX IF NOT EXISTS attempts_question ON attempts (question_id);
CREATE INDEX IF NOT EXISTS attempts_time ON attempts (attempted_at);
CREATE UNIQUE INDEX IF NOT EXISTS question_groups_anki ON question_groups (anki_id) WHERE anki_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS questions_anki ON questions (anki_id) WHERE anki_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_items_draft ON draft_items (draft_id, number);
CREATE INDEX IF NOT EXISTS question_rewrites_question ON question_rewrites (question_id);
CREATE INDEX IF NOT EXISTS chats_book ON chats (book_id, throwaway);
CREATE INDEX IF NOT EXISTS chats_question ON chats (question_id);
"""

# Older databases named things as Anki does (cards, notes, reviews): renamed before SCHEMA runs, so
# it never makes empty tables beside the old ones. Renaming a table updates the foreign keys that
# point at it.
TABLE_RENAMES = (("card_notes", "question_groups"), ("cards", "questions"), ("reviews", "attempts"),
                 ("card_rewrites", "question_rewrites"))
COLUMN_RENAMES = (
    ("questions", "note_id", "group_id"), ("questions", "question", "position"),
    ("questions", "last_review", "last_attempt"),
    ("attempts", "card_id", "question_id"), ("attempts", "reviewed_at", "attempted_at"),
    ("question_rewrites", "card_id", "question_id"), ("chats", "card_id", "question_id"),
    ("draft_items", "card_id", "question_id"),
)
# Their indexes: dropped, so INDEXES makes them under the new names.
OLD_INDEXES = ("cards_due", "cards_deck", "cards_note", "reviews_card", "reviews_time", "card_notes_anki", "cards_anki",
               "card_rewrites_card", "chats_card")
SETTING_RENAMES = (
    ("srs_new_per_day", "practice_new_per_day"), ("srs_desired_retention", "practice_desired_retention"),
    ("srs_parameters", "practice_parameters"), ("srs_backfilled", "practice_backfilled"),
    ("review_panel", "practice_panel"), ("review_calc", "practice_calc"), ("review_chat_list", "practice_chat_list"),
)
BACKUP_SUFFIX = ".before-rename.db"  # the copy kept of a database from before the renames


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
    """(stability, difficulty, last attempt time) of a stored fsrs.Card."""
    if not fsrs_json:
        return None, None, None
    from datetime import datetime
    d = json.loads(fsrs_json)
    last = d.get("last_review")
    return d.get("stability"), d.get("difficulty"), datetime.fromisoformat(last).timestamp() if last else None



def _renamed_type(text: str) -> str | None:
    """Stored question JSON (data or a variant) with its "type" renamed, or None if it needs no change."""
    try:
        q = json.loads(text) if text else None
    except ValueError:
        return None
    if isinstance(q, dict) and q.get("type") in question_model.KIND_ALIASES:
        return question_model.dumps({**q, "type": question_model.kind_name(q["type"])})
    return None


def _renamed_spec(spec):
    """A {kind, fields, ...} question spec with its older type names renamed."""
    if not isinstance(spec, dict):
        return spec
    out = dict(spec)
    if "kind" in out:
        out["kind"] = question_model.kind_name(out["kind"])
    if isinstance(out.get("fields"), dict) and "type" in out["fields"]:
        out["fields"] = {**out["fields"], "type": question_model.kind_name(out["fields"]["type"])}
    return out


def passage_pages(page: int, rects: list) -> dict[int, list[list[float]]]:
    """A snippet's or note's rects by page, in page order. A passage selected across pages
    starts on `page`; its rects on later pages carry their page as a fifth number
    ([x0, y0, x1, y1, page]), and the rest (all of them, for one on a single page) are on `page`."""
    out: dict[int, list[list[float]]] = {}
    for r in rects:
        out.setdefault(int(r[4]) if len(r) > 4 else page, []).append(list(r[:4]))
    return dict(sorted(out.items()))


def last_page(page: int, rects: list) -> int:
    """The page a snippet or note ends on."""
    return max([page, *(int(r[4]) for r in rects if len(r) > 4)])


def _passage(row) -> dict:
    """A snippet or note row, its rects decoded, with the page it ends on as `last_page`."""
    d = {**dict(row), "rects": json.loads(row["rects"])}
    d["last_page"] = last_page(d["page"], d["rects"])
    return d


def data_dir() -> Path:
    override = os.environ.get("STUDY_READER_DATA")
    base = Path(override) if override else Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    ) / "study-reader"
    base.mkdir(parents=True, exist_ok=True)
    return base


def media_dir() -> Path:
    """Images and other files question text refers to by name (e.g. migrated from Anki)."""
    path = data_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_url() -> str:
    """media_dir() as the base URL pages resolve question images against."""
    return media_dir().as_uri() + "/"


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else str(data_dir() / "study-reader.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._rename_old_schema()
        # Write-ahead logging: commits are cheap (a migration from Anki makes thousands) and a
        # crash can't corrupt the file; NORMAL sync is safe with it.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self.conn.executescript(INDEXES)

    # -- migrations ------------------------------------------------------------
    def _tables(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _rename_old_schema(self) -> None:
        """Tables, columns and indexes named as Anki names them (cards, card_notes, reviews, ...)
        get their names now (TABLE_RENAMES, COLUMN_RENAMES, OLD_INDEXES), before SCHEMA runs. The
        first time, the database is copied to BACKUP_SUFFIX beside it first."""
        tables = self._tables()
        steps: list[str] = []
        for old, new in TABLE_RENAMES:
            if old not in tables:
                continue
            if new in tables:
                # Made empty beside the old one (by a build that didn't rename): the old one wins.
                if self.conn.execute(f"SELECT 1 FROM {new} LIMIT 1").fetchone():
                    continue
                steps.append(f"DROP TABLE {new}")
            steps.append(f"ALTER TABLE {old} RENAME TO {new}")
            tables = (tables - {old}) | {new}
        for table, old, new in COLUMN_RENAMES:
            if table in tables:
                # A table renamed above still has its old name until the steps run.
                source = next((o for o, n in TABLE_RENAMES if n == table and f"ALTER TABLE {o} RENAME TO {n}" in steps),
                              table)
                cols = self._columns(source)
                if old in cols and new not in cols:
                    steps.append(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        indexes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        steps += [f"DROP INDEX {name}" for name in OLD_INDEXES if name in indexes]
        if not steps:
            return
        self._backup()
        self.conn.execute("BEGIN")
        try:
            for step in steps:
                self.conn.execute(step)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _backup(self) -> None:
        """Copy the database (with sqlite's backup API, so a WAL file's contents come along) to
        <name>.before-rename.db beside it, unless that copy exists already."""
        if self.path == ":memory:" or not Path(self.path).is_file():
            return
        target = Path(self.path).with_name(Path(self.path).stem + BACKUP_SUFFIX)
        if target.exists():
            return
        tmp = target.with_name(target.name + ".tmp")
        tmp.unlink(missing_ok=True)
        copy = sqlite3.connect(tmp)
        try:
            self.conn.backup(copy)
        finally:
            copy.close()
        os.replace(tmp, target)

    def _migrate(self) -> None:
        book_cols = self._columns("books")
        if "crop_margins" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN crop_margins INTEGER NOT NULL DEFAULT 0")
        if "show_toc" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN show_toc INTEGER NOT NULL DEFAULT 1")
        if "right_panel" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN right_panel TEXT NOT NULL DEFAULT 'chat'")
        if "show_chat_list" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN show_chat_list INTEGER NOT NULL DEFAULT 1")
        # The book's own practice panel ('review', from before the Practice page took its place) is
        # gone: chats instead, and the calculator that sat under it alone.
        self.conn.execute("UPDATE books SET right_panel='chat' WHERE right_panel='review'")
        self.conn.execute("UPDATE books SET right_panel='calc' WHERE right_panel='calc+review'")
        if "source_url" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN source_url TEXT")
        if "finished_at" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN finished_at REAL")
        if "toc" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN toc TEXT")
        cols = self._columns("chats")
        if "model" not in cols:
            # Existing chats keep the model that was selected globally before chats had their own.
            self.conn.execute("ALTER TABLE chats ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE chats SET model=COALESCE((SELECT value FROM settings WHERE key='model'), '')"
            )
        msg_cols = self._columns("messages")
        for col in ("session_id", "session_uuid"):
            if col not in msg_cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        for col in ("page_from", "page_to"):
            if col not in msg_cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        if "stopped" not in msg_cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0")
        if "question_id" not in cols:
            self._migrate_chats()
        calc_cols = self._columns("calculations")
        if "latex" not in calc_cols:
            self.conn.execute("ALTER TABLE calculations ADD COLUMN latex TEXT NOT NULL DEFAULT ''")
        if any(r["name"] == "book_id" and r["notnull"] for r in self.conn.execute("PRAGMA table_info(calculations)")):
            # book_id became optional (the Practice page's history for questions with no book): rebuild
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
            # The deck a book's new quiz questions go into (made when its first question is).
            self.conn.execute("ALTER TABLE books ADD COLUMN deck_id INTEGER REFERENCES decks(id) ON DELETE SET NULL")
        attempt_cols = self._columns("attempts")
        if "duration" not in attempt_cols:
            self.conn.execute("ALTER TABLE attempts ADD COLUMN duration INTEGER NOT NULL DEFAULT 0")
        if "was_new" not in attempt_cols:
            self.conn.execute("ALTER TABLE attempts ADD COLUMN was_new INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE attempts SET was_new=1 WHERE NOT EXISTS "
                              "(SELECT 1 FROM attempts p WHERE p.question_id=attempts.question_id AND p.id<attempts.id)")
        question_cols = self._columns("questions")
        if "group_id" not in question_cols:
            self._migrate_questions_to_groups()
        elif "variant" not in question_cols:
            self.conn.execute("ALTER TABLE questions ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
        # Questions made from a highlight and Claude Conversion are gone (their questions stay as they are).
        self.conn.execute("DROP INDEX IF EXISTS cards_passage")
        if "passage_id" in self._columns("questions"):
            self.conn.execute("ALTER TABLE questions DROP COLUMN passage_id")
        self.conn.execute("DROP TABLE IF EXISTS card_passages")
        self.conn.execute("DROP TABLE IF EXISTS conversions")
        self.conn.execute("DELETE FROM settings WHERE key='convert_instructions'")
        self._migrate_question_types()
        self._migrate_terms()
        # Section tags were once made from contents titles padded with NULs, which the practice
        # page's section list couldn't hand back intact.
        padded = [r["id"] for r in self.conn.execute(
            "SELECT id FROM question_groups WHERE instr(CAST(tags AS BLOB), x'00') > 0")]
        if padded:
            self._retag(padded, lambda tags: [t for t in map(question_model.clean_tag, tags) if t])
        # A }} inside a blank's math ({{c1::\(\sqrt{LC}}\)}}) once ended the blank; remake
        # the cached sides of questions that were cut short.
        for r in self.conn.execute(r"SELECT q.id, q.ord, q.data, g.fields FROM questions q "
                                   r"JOIN question_groups g ON g.id=q.group_id "
                                   r"WHERE g.kind='fill_in' AND g.fields LIKE '%}}%\\)%'").fetchall():
            try:
                data = question_model.dumps(question_model.question_data("fill_in", json.loads(r["fields"]), r["ord"]))
            except question_model.QuestionError:
                continue
            if data != r["data"]:
                self.conn.execute("UPDATE questions SET data=? WHERE id=?", (data, r["id"]))

    def _migrate_question_types(self) -> None:
        """Types were once Anki-style kinds of notes: basic, reverse and quiz (every answered
        question, whatever its type; before that self-checks too), and short answers were "free"
        responses. Then they had Anki's names (flashcard, reversed, cloze, occlusion) and numeric
        and written answers. Each becomes its type (question_model.KIND_ALIASES), in its group's
        kind and fields and in its questions' data and variants. Rows keep their ids, schedules and
        times."""
        changed: list[int] = []
        for r in self.conn.execute("SELECT id, fields FROM question_groups WHERE kind='quiz'").fetchall():
            q = json.loads(r["fields"])
            kind = question_model.kind_name(q.get("type"))
            if kind not in (*question_model.QUESTION_TYPES, "self_check"):
                kind = "short_answer"
            kind, fields = question_model.question_fields({**q, "type": kind})
            self.conn.execute(
                "UPDATE question_groups SET kind=?, fields=?, sort_text=?, search_text=? WHERE id=?",
                (kind, question_model.dumps(fields), question_model.sort_text(kind, fields),
                 question_model.search_text(kind, fields), r["id"]),
            )
            for c in self.conn.execute("SELECT id, ord, data FROM questions WHERE group_id=?", (r["id"],)).fetchall():
                data = question_model.dumps(question_model.question_data(kind, fields, c["ord"]))
                if data != c["data"]:
                    self.conn.execute("UPDATE questions SET data=? WHERE id=?", (data, c["id"]))
            changed.append(r["id"])
        old = tuple(question_model.KIND_ALIASES)
        for r in self.conn.execute(f"SELECT id, kind, fields FROM question_groups WHERE kind IN ({','.join('?' * len(old))})",
                                   old).fetchall():
            kind = question_model.kind_name(r["kind"])
            fields = r["fields"]
            if kind in question_model.QUESTION_TYPES:
                q = json.loads(fields)
                if isinstance(q, dict) and q.get("type") != kind:
                    fields = question_model.dumps({**q, "type": kind})
            self.conn.execute("UPDATE question_groups SET kind=?, fields=? WHERE id=?", (kind, fields, r["id"]))
            changed.append(r["id"])
        for chunk in _chunks(changed):
            for r in self.conn.execute(f"SELECT id, data, variant FROM questions WHERE group_id IN "
                                       f"({','.join('?' * len(chunk))})", chunk).fetchall():
                change = {col: v for col in ("data", "variant") if (v := _renamed_type(r[col])) is not None}
                if change:
                    self.conn.execute(f"UPDATE questions SET {', '.join(f'{k}=?' for k in change)} WHERE id=?",
                                      (*change.values(), r["id"]))

    def _migrate_terms(self) -> None:
        """What older versions stored under older names, besides the schema (_rename_old_schema)
        and the types (_migrate_question_types): settings keys, the Add dialog's type, attempts
        made in practice (once 'review'), and the question specs Make with Claude keeps."""
        for old, new in SETTING_RENAMES:
            self.conn.execute("UPDATE OR IGNORE settings SET key=? WHERE key=?", (new, old))
            self.conn.execute("DELETE FROM settings WHERE key=?", (old,))
        for old, new in question_model.KIND_ALIASES.items():
            self.conn.execute("UPDATE settings SET value=? WHERE key='add_type' AND value=?", (new, old))
        self.conn.execute("UPDATE attempts SET source='practice' WHERE source='review'")
        likes = " OR ".join("{col} LIKE ?" for _ in question_model.KIND_ALIASES)
        patterns = tuple(f'%"{k}"%' for k in question_model.KIND_ALIASES)
        for r in self.conn.execute(f"SELECT id, proposal FROM draft_items WHERE {likes.format(col='proposal')}",
                                   patterns).fetchall():
            try:
                specs = json.loads(r["proposal"])
            except ValueError:
                continue
            if isinstance(specs, list):
                specs = [_renamed_spec(s) for s in specs]
                self.conn.execute("UPDATE draft_items SET proposal=? WHERE id=?",
                                  (json.dumps(specs, ensure_ascii=False), r["id"]))
        for r in self.conn.execute(f"SELECT id, original FROM question_rewrites WHERE original LIKE '%\"note_id\"%' "
                                   f"OR {likes.format(col='original')}", patterns).fetchall():
            try:
                original = json.loads(r["original"])
            except ValueError:
                continue
            if not isinstance(original, dict):
                continue
            if "note_id" in original:
                original = {("group_id" if k == "note_id" else k): v for k, v in original.items()}
            original = _renamed_spec(original)
            if isinstance(original.get("data"), dict):
                original["data"] = {**original["data"], "type": question_model.kind_name(original["data"].get("type"))}
            self.conn.execute("UPDATE question_rewrites SET original=? WHERE id=?",
                              (json.dumps(original, ensure_ascii=False), r["id"]))

    def _migrate_chats(self) -> None:
        """Chats from before the Practice page had its own: book_id becomes optional (questions
        with no book), and chats gain question_id and throwaway. The table is rebuilt with foreign
        keys off, or dropping the old one would take every message with it."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("DROP TABLE IF EXISTS chats_new")  # left by an earlier attempt that failed
            new_schema = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS chats ("):]
            new_schema = new_schema[:new_schema.index(");") + 2].replace(
                "CREATE TABLE IF NOT EXISTS chats (", "CREATE TABLE chats_new (")
            with self.conn:
                self.conn.execute(new_schema)
                self.conn.execute("INSERT INTO chats_new (id, book_id, title, session_id, model, created_at, updated_at) "
                                  "SELECT id, book_id, title, session_id, model, created_at, updated_at FROM chats")
                self.conn.execute("DROP TABLE chats")
                self.conn.execute("ALTER TABLE chats_new RENAME TO chats")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_questions_to_groups(self) -> None:
        """Questions from before decks and groups: each becomes a 'quiz' group in its book's deck
        (_migrate_question_types gives it its type). The table is rebuilt (book_id and the quiz
        position become optional), so foreign keys are off meanwhile, or dropping the old table
        would take the attempts with it."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("DROP TABLE IF EXISTS questions_new")  # left by an earlier attempt that failed
            with self.conn:
                new_schema = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS questions ("):]
                new_schema = new_schema[:new_schema.index(");") + 2].replace(
                    "CREATE TABLE IF NOT EXISTS questions (", "CREATE TABLE questions_new (")
                self.conn.execute(new_schema)
                for c in self.conn.execute("SELECT * FROM questions ORDER BY id").fetchall():
                    data = json.loads(c["data"])
                    deck_id = self.book_deck(c["book_id"])
                    group_id = self._insert_group("quiz", data, "", c["created_at"])
                    stability, difficulty, last_attempt = _fsrs_columns(c["fsrs"])
                    self.conn.execute(
                        "INSERT INTO questions_new (id, group_id, ord, deck_id, book_id, message_id, quiz, position, "
                        "title, data, page, stage, fsrs, due, reps, lapses, suspended, stability, difficulty, "
                        "last_attempt, created_at, updated_at) VALUES (?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (c["id"], group_id, deck_id, c["book_id"], c["message_id"], c["quiz"], c["position"],
                         c["title"], c["data"], c["page"], c["stage"], c["fsrs"], c["due"], c["reps"], c["lapses"],
                         c["suspended"], stability, difficulty, last_attempt, c["created_at"], c["created_at"]),
                    )
                self.conn.execute("DROP TABLE questions")
                self.conn.execute("ALTER TABLE questions_new RENAME TO questions")
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
        """Remove a book with its chats. Its quiz questions that were never attempted go too;
        attempted ones stay in their decks, no longer linked to the book."""
        with self.conn:
            self._drop_new_questions("SELECT m.id FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.book_id=?", (book_id,))
            self.conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    # -- chats ---------------------------------------------------------------
    def create_chat(self, book_id: int | None, title: str, model: str = "", question_id: int | None = None,
                    throwaway: bool = False) -> int:
        """book_id None: a chat about a question with no book. question_id: the question it was
        started beside on the Practice page. throwaway: not kept (left out of chats(), deleted when left)."""
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO chats (book_id, question_id, title, model, throwaway, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (book_id, question_id, title, model, int(throwaway), now, now),
            )
        return cur.lastrowid

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()

    def chats(self, book_id: int | None) -> list[sqlite3.Row]:
        """A book's chats (None: those about questions with no book), latest first, throwaway ones left out."""
        return self.conn.execute(
            "SELECT * FROM chats WHERE book_id IS ? AND throwaway=0 ORDER BY updated_at DESC", (book_id,)
        ).fetchall()

    def question_chat(self, question_id: int) -> sqlite3.Row | None:
        """The latest (kept) chat started beside a question on the Practice page."""
        return self.conn.execute(
            "SELECT * FROM chats WHERE question_id=? AND throwaway=0 ORDER BY updated_at DESC LIMIT 1", (question_id,)
        ).fetchone()

    def throwaway_chats(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chats WHERE throwaway=1").fetchall()

    def chat_sessions(self, chat_id: int) -> list[str]:
        """Every Claude session a chat's replies were in (branches fork new ones), and its own."""
        chat = self.chat(chat_id)
        ids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT session_id FROM messages WHERE chat_id=? AND session_id IS NOT NULL", (chat_id,))]
        if chat is not None and chat["session_id"]:
            ids.append(chat["session_id"])
        return list(dict.fromkeys(ids))

    def update_chat(self, chat_id: int, **fields) -> None:
        self._update("chats", chat_id, fields)

    def delete_chat(self, chat_id: int) -> None:
        with self.conn:
            self._drop_new_questions("SELECT id FROM messages WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    # -- messages ------------------------------------------------------------
    def add_message(
        self, chat_id: int, role: str, text: str, snippets: list[dict] | None = None,
        session_id: str | None = None, session_uuid: str | None = None, pages: tuple[int, int] | None = None,
        stopped: bool = False,
    ) -> int:
        """snippets: dicts with kind, page, text, image_path, rects. pages: the 0-based first and
        last pages a quiz request covers. stopped: a reply the user stopped partway."""
        now = time.time()
        first, last = pages or (None, None)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO messages (chat_id, role, text, session_id, session_uuid, page_from, page_to, stopped, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (chat_id, role, text, session_id, session_uuid, first, last, int(stopped), now),
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
        quizzes' questions that were never attempted; attempted ones stay in the deck)."""
        with self.conn:
            self._drop_new_questions("SELECT id FROM messages WHERE chat_id=? AND id>=?", (chat_id, message_id))
            self.conn.execute("DELETE FROM messages WHERE chat_id=? AND id>=?", (chat_id, message_id))

    def messages(self, chat_id: int) -> list[dict]:
        out = []
        for m in self.conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)):
            d = dict(m)
            d["snippets"] = [
                _passage(s)
                for s in self.conn.execute("SELECT * FROM snippets WHERE message_id=? ORDER BY id", (m["id"],))
            ]
            out.append(d)
        return out

    def message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def snippet(self, snippet_id: int) -> dict | None:
        s = self.conn.execute("SELECT * FROM snippets WHERE id=?", (snippet_id,)).fetchone()
        return _passage(s) if s else None

    def book_snippets(self, book_id: int) -> list[dict]:
        """Every snippet asked about in any of the book's chats, with its chat id."""
        rows = self.conn.execute(
            "SELECT s.*, m.chat_id FROM snippets s JOIN messages m ON m.id=s.message_id "
            "JOIN chats c ON c.id=m.chat_id WHERE c.book_id=? ORDER BY s.id",
            (book_id,),
        )
        return [_passage(s) for s in rows]

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
        return _passage(n) if n else None

    def notes(self, book_id: int) -> list[dict]:
        """The book's notes in reading order."""
        rows = self.conn.execute("SELECT * FROM notes WHERE book_id=? ORDER BY page, id", (book_id,))
        out = [_passage(n) for n in rows]
        out.sort(key=lambda n: (n["page"], min((r[1] for r in passage_pages(n["page"], n["rects"]).get(n["page"], [])),
                                               default=0.0), n["id"]))
        return out

    def update_note(self, note_id: int, body: str) -> None:
        self._update("notes", note_id, {"body": body, "updated_at": time.time()})

    def delete_note(self, note_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

    # -- calculator -----------------------------------------------------------
    # A book_id of None is the Practice page's shared history, for questions with no book.
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
        """The deck a book's new quiz questions go into, made (named after the book) if it has none."""
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
                    self.conn.execute("UPDATE questions SET deck_id=? WHERE deck_id=?", (existing["id"], row["id"]))
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
        """Delete a deck, the decks under it, and all their questions. Returns how many questions went."""
        ids = self.deck_subtree(deck_id)
        with self.conn:
            marks = ",".join("?" * len(ids))
            groups = [r[0] for r in self.conn.execute(f"SELECT DISTINCT group_id FROM questions WHERE deck_id IN ({marks})", ids)]
            n = self.conn.execute(f"DELETE FROM questions WHERE deck_id IN ({marks})", ids).rowcount
            self.conn.execute(f"DELETE FROM decks WHERE id IN ({marks})", ids)
            self._drop_orphan_groups(groups)
        return n

    # -- question groups ------------------------------------------------------------
    def _insert_group(self, kind: str, fields: dict, tags: str, now: float, anki_id: int | None = None,
                     created_at: float | None = None) -> int:
        return self.conn.execute(
            "INSERT INTO question_groups (kind, fields, tags, sort_text, search_text, anki_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, question_model.dumps(fields), tags, question_model.sort_text(kind, fields),
             question_model.search_text(kind, fields), anki_id, created_at or now, now),
        ).lastrowid

    def add_question_group(
        self, kind: str, fields: dict, deck_id: int, tags=(), book_id: int | None = None, page: int | None = None,
        now: float | None = None, anki_id: int | None = None, created_at: float | None = None, title: str = "",
    ) -> int:
        """A group and its questions (new); returns the group's id. Raises question_model.QuestionError if it
        makes no question."""
        question_model.check(kind, fields)
        now = time.time() if now is None else now
        with self.conn:
            group_id = self._insert_group(kind, fields, question_model.join_tags(tags), now, anki_id, created_at)
            for ord in question_model.question_ords(kind, fields):
                self._insert_question(group_id, ord, deck_id, kind, fields, now, book_id=book_id, page=page,
                                  created_at=created_at, title=title)
        return group_id

    def _insert_question(self, group_id: int, ord: int, deck_id: int, kind: str, fields: dict, now: float,
                     created_at: float | None = None, **cols) -> int:
        cols = {k: v for k, v in cols.items() if v is not None}
        names = ["group_id", "ord", "deck_id", "data", "due", "created_at", "updated_at", *cols]
        values = [group_id, ord, deck_id, question_model.dumps(question_model.question_data(kind, fields, ord)),
                  created_at or now, created_at or now, now, *cols.values()]
        return self.conn.execute(
            f"INSERT INTO questions ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values
        ).lastrowid

    def ensure_question(self, group_id: int, ord: int, deck_id: int, now: float | None = None) -> int:
        """The group's question `ord`, made (new, in this deck) if the group doesn't have it."""
        row = self.conn.execute("SELECT id FROM questions WHERE group_id=? AND ord=?", (group_id, ord)).fetchone()
        if row:
            return row["id"]
        group = self.question_group(group_id)
        with self.conn:
            return self._insert_question(group_id, ord, deck_id, group["kind"], json.loads(group["fields"]),
                                     time.time() if now is None else now)

    def question_group(self, group_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM question_groups WHERE id=?", (group_id,)).fetchone()

    def group_by_anki_id(self, anki_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM question_groups WHERE anki_id=?", (anki_id,)).fetchone()

    def group_questions(self, group_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM questions WHERE group_id=? ORDER BY ord", (group_id,)).fetchall()

    def update_question_group(self, group_id: int, fields: dict | None = None, tags=None, kind: str | None = None,
                         now: float | None = None) -> None:
        """Change a group's fields (and kind) and/or tags. Its questions are remade from the fields.
        A blank's number (or reverse side) the edit introduces gets a new question; one the group
        made before but no longer has a question for (deleted, left behind in Anki) isn't brought
        back. A question whose blank the edit removes goes if it was never attempted; attempted
        ones stay, showing what they did, so an edit (or a pause while retyping) can't lose
        practice history."""
        group = self.question_group(group_id)
        if group is None:
            return
        now = time.time() if now is None else now
        kind = kind or group["kind"]
        with self.conn:
            if tags is not None:
                self.conn.execute("UPDATE question_groups SET tags=?, updated_at=? WHERE id=?",
                                  (question_model.join_tags(tags), now, group_id))
            if fields is None:
                return
            question_model.check(kind, fields)
            before = set(question_model.question_ords(group["kind"], json.loads(group["fields"])))
            self.conn.execute(
                "UPDATE question_groups SET kind=?, fields=?, sort_text=?, search_text=?, updated_at=? WHERE id=?",
                (kind, question_model.dumps(fields), question_model.sort_text(kind, fields),
                 question_model.search_text(kind, fields), now, group_id),
            )
            questions = {q["ord"]: q for q in self.group_questions(group_id)}
            ords = question_model.question_ords(kind, fields)
            first = next(iter(questions.values()), None)
            for ord in ords:
                data = question_model.dumps(question_model.question_data(kind, fields, ord))
                if ord in questions:  # a version with new numbers of what it was before is out of date
                    self.conn.execute("UPDATE questions SET variant=CASE WHEN data=? THEN variant ELSE '' END, data=?, "
                                      "updated_at=? WHERE id=?", (data, data, now, questions[ord]["id"]))
                elif ord not in before:
                    self._insert_question(
                        group_id, ord, first["deck_id"] if first else self._ensure_deck(DEFAULT_DECK), kind, fields,
                        now, book_id=first["book_id"] if first else None, page=first["page"] if first else None,
                    )
            gone = [q["id"] for o, q in questions.items() if o not in ords and not self.conn.execute(
                "SELECT 1 FROM attempts WHERE question_id=? LIMIT 1", (q["id"],)).fetchone()]
            if gone:
                self.conn.execute(f"DELETE FROM questions WHERE id IN ({','.join('?' * len(gone))})", gone)

    def move_question_to_group(self, question_id: int, kind: str, fields: dict, now: float | None = None) -> int:
        """Give one question new content in a question_groups row of its own, as the first question the fields
        make (the question keeps its deck, schedule and history, and its tags); the old row goes if no
        other question is left on it. Returns the new row's id."""
        question_model.check(kind, fields)
        question = self.question(question_id)
        old = self.question_group(question["group_id"])
        now = time.time() if now is None else now
        with self.conn:
            group_id = self._insert_group(kind, fields, old["tags"] if old else "", now,
                                        created_at=old["created_at"] if old else None)
            self.move_question(question_id, group_id, question_model.question_ords(kind, fields)[0], now)
        return group_id

    def move_question(self, question_id: int, group_id: int, ord: int, now: float | None = None) -> None:
        """Put a question on another question_groups row as its question `ord` (keeping its schedule and
        history); its old row goes if no other question is left on it."""
        question, group = self.question(question_id), self.question_group(group_id)
        data = question_model.dumps(question_model.question_data(group["kind"], json.loads(group["fields"]), ord))
        with self.conn:
            self.conn.execute("UPDATE questions SET group_id=?, ord=?, data=?, variant='', updated_at=? WHERE id=?",
                              (group_id, ord, data, time.time() if now is None else now, question_id))
            self._drop_orphan_groups([question["group_id"]])

    def _drop_orphan_groups(self, group_ids) -> None:
        """Delete those of these groups that no question is left on."""
        for chunk in _chunks(list(group_ids)):
            self.conn.execute(f"DELETE FROM question_groups WHERE id IN ({','.join('?' * len(chunk))}) "
                              "AND NOT EXISTS (SELECT 1 FROM questions WHERE questions.group_id=question_groups.id)", chunk)

    # -- tags ------------------------------------------------------------------
    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for (t,) in self.conn.execute("SELECT DISTINCT tags FROM question_groups WHERE tags!=''"):
            tags.update(t.split())
        return sorted(tags, key=str.lower)

    def add_tags(self, group_ids, tags) -> None:
        self._retag(group_ids, lambda old: old + [t for t in tags if t not in old])

    def remove_tags(self, group_ids, tags) -> None:
        drop = {t.lower() for t in tags}
        self._retag(group_ids, lambda old: [t for t in old if t.lower() not in drop])

    def rename_tag(self, old: str, new: str) -> None:
        """Rename a tag and the tags under it (old::child)."""
        rows = self.conn.execute("SELECT id, tags FROM question_groups WHERE tags LIKE ? ESCAPE '\\'",
                                 (f"% {_like(old)}%",)).fetchall()

        def renamed(t: str) -> str:
            if t.lower() == old.lower():
                return new
            if t.lower().startswith(old.lower() + "::"):
                return new + t[len(old):] if new else ""
            return t
        self._retag([r["id"] for r in rows], lambda tags: [renamed(t) for t in tags if renamed(t)])

    def _retag(self, group_ids, change) -> None:
        now = time.time()
        with self.conn:
            for chunk in _chunks(list(group_ids)):
                for r in self.conn.execute(
                    f"SELECT id, tags FROM question_groups WHERE id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall():
                    tags = change(r["tags"].split())
                    joined = question_model.join_tags(tags)
                    if joined != r["tags"]:
                        self.conn.execute("UPDATE question_groups SET tags=?, updated_at=? WHERE id=?", (joined, now, r["id"]))

    # -- questions --------------------------------------------------------------
    def _drop_new_questions(self, messages_sql: str, args: tuple) -> None:
        where = f"message_id IN ({messages_sql}) AND NOT EXISTS (SELECT 1 FROM attempts r WHERE r.question_id=questions.id)"
        groups = [r[0] for r in self.conn.execute(f"SELECT DISTINCT group_id FROM questions WHERE {where}", args)]
        self.conn.execute(f"DELETE FROM questions WHERE {where}", args)
        self._drop_orphan_groups(groups)

    def add_question(
        self, book_id: int, message_id: int | None, quiz: int, position: int, title: str, data: str,
        page: int | None, now: float, tags=(), deck_id: int | None = None,
    ) -> int | None:
        """A new question for a quiz's question at `position`, in deck_id or else the book's deck;
        None if it has one already."""
        if message_id is not None and self.question_for(message_id, quiz, position) is not None:
            return None
        kind, fields = question_model.question_fields(json.loads(data))
        with self.conn:
            if deck_id is None or self.deck(deck_id) is None:
                deck_id = self.book_deck(book_id)
            group_id = self._insert_group(kind, fields, question_model.join_tags(tags), now)
            return self._insert_question(group_id, 0, deck_id, kind, fields, now, book_id=book_id, message_id=message_id,
                                     quiz=quiz, position=position, title=title, page=page)

    def relink_question(self, question_id: int, message_id: int, quiz: int, position: int, title: str,
                    page: int | None, now: float) -> None:
        """Hand a question over to the quiz question that revises it, in another reply."""
        with self.conn:
            self.conn.execute(
                "UPDATE questions SET message_id=?, quiz=?, position=?, title=?, page=COALESCE(?, page), updated_at=? "
                "WHERE id=?", (message_id, quiz, position, title, page, now, question_id),
            )

    def drop_new_quiz_questions(self, message_id: int, quiz: int) -> None:
        """Delete a quiz's questions that were never attempted (it was revised without them)."""
        where = "message_id=? AND quiz=? AND NOT EXISTS (SELECT 1 FROM attempts r WHERE r.question_id=questions.id)"
        with self.conn:
            groups = [r[0] for r in self.conn.execute(f"SELECT DISTINCT group_id FROM questions WHERE {where}", (message_id, quiz))]
            self.conn.execute(f"DELETE FROM questions WHERE {where}", (message_id, quiz))
            self._drop_orphan_groups(groups)

    def question(self, question_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()

    def question_for(self, message_id: int, quiz: int, position: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM questions WHERE message_id=? AND quiz=? AND position=?", (message_id, quiz, position)
        ).fetchone()

    def question_by_anki_id(self, anki_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM questions WHERE anki_id=?", (anki_id,)).fetchone()

    def quiz_questions(self, message_id: int, quiz: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM questions WHERE message_id=? AND quiz=? ORDER BY position", (message_id, quiz)
        ).fetchall()

    def _scope(self, book_id: int | None, deck_id: int | None, tag: str | None = None,
               picks=()) -> tuple[str, tuple]:
        """SQL condition (ending in AND) and its arguments for a book, deck (with its subdecks) or tag,
        and within any of picks (several of them, the Practice page's multiselect: {"deck": id} or
        {"book": id, "section": tag or None}); none for every question."""
        sql, args = "", ()
        if picks:
            parts = []
            for p in picks:
                part, part_args = (self._scope(None, p["deck"]) if p.get("deck") is not None
                                   else self._scope(p["book"], None, p.get("section")))
                parts.append(f"({part.removesuffix(' AND ')})")
                args += part_args
            sql = f"({' OR '.join(parts)}) AND "
        if book_id is not None:
            sql, args = "book_id=? AND ", (book_id,)
        if deck_id is not None:
            sql, args = sql + f"deck_id IN ({SUBTREE}) AND ", args + (deck_id,)
        if tag is not None:  # the tag or a tag under it (tag::child)
            sql += ("group_id IN (SELECT id FROM question_groups WHERE tags LIKE ? ESCAPE '\\' "
                    "OR tags LIKE ? ESCAPE '\\') AND ")
            args += (f"% {_like(tag)} %", f"% {_like(tag)}::%")
        return sql, args

    def questions(self, book_id: int | None = None, where: str = "1", args: tuple = (), order: str = "id",
              limit: int = -1, deck_id: int | None = None, tag: str | None = None, picks=()) -> list[sqlite3.Row]:
        """Questions of a book, deck or tag (or of every one; or of any of picks), filtered by a SQL condition."""
        scope, scope_args = self._scope(book_id, deck_id, tag, picks)
        return self.conn.execute(
            f"SELECT * FROM questions WHERE {scope}({where}) ORDER BY {order} LIMIT ?", scope_args + args + (limit,),
        ).fetchall()

    def count_questions(self, book_id: int | None = None, where: str = "1", args: tuple = (),
                    deck_id: int | None = None, tag: str | None = None, picks=()) -> int:
        scope, scope_args = self._scope(book_id, deck_id, tag, picks)
        return self.conn.execute(f"SELECT COUNT(*) FROM questions WHERE {scope}({where})", scope_args + args).fetchone()[0]

    def record_attempt(
        self, question_id: int, rating: int, source: str, response: str, verdict: str | None, at: float,
        duration: int = 0, was_new: bool = False, **question_fields,
    ) -> None:
        """Log an attempt and store the question's new schedule (together, in one transaction)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO attempts (question_id, rating, source, response, verdict, attempted_at, duration, was_new) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (question_id, rating, source, response, verdict, at, duration, int(was_new)),
            )
            if question_fields:
                self.conn.execute(f"UPDATE questions SET {', '.join(f'{k}=?' for k in question_fields)} WHERE id=?",
                                  (*question_fields.values(), question_id))

    def attempts(self, question_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM attempts WHERE question_id=? ORDER BY attempted_at, id", (question_id,)).fetchall()

    def new_questions_attempted_since(self, since: float) -> int:
        """New questions (never attempted, or forgotten) introduced in practice (not the chat) since `since`."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE source='practice' AND was_new=1 AND attempted_at>=?", (since,),
        ).fetchone()[0]

    def update_question(self, question_id: int, **fields) -> None:
        self._update("questions", question_id, fields)

    def update_questions(self, question_ids, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            for chunk in _chunks(list(question_ids)):
                self.conn.execute(f"UPDATE questions SET {cols} WHERE id IN ({','.join('?' * len(chunk))})",
                                  (*fields.values(), *chunk))

    def set_quiz_suspended(self, message_id: int, quiz: int, suspended: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE questions SET suspended=? WHERE message_id=? AND quiz=?", (int(suspended), message_id, quiz)
            )

    def delete_question(self, question_id: int) -> None:
        self.delete_questions([question_id])

    def delete_questions(self, question_ids) -> None:
        question_ids = list(question_ids)
        with self.conn:
            groups = self.group_ids(question_ids)
            for chunk in _chunks(question_ids):
                self.conn.execute(f"DELETE FROM questions WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            self._drop_orphan_groups(groups)

    def any_question(self, question_ids, where: str, args=()) -> bool:
        """Whether any of these questions meets a condition on the questions table."""
        return any(self.conn.execute(
            f"SELECT 1 FROM questions WHERE id IN ({','.join('?' * len(chunk))}) AND {where} LIMIT 1", (*chunk, *args)
        ).fetchone() for chunk in _chunks(list(question_ids)))

    def any_group(self, group_ids, where: str, args=()) -> bool:
        """Whether any of these groups meets a condition on the question_groups table."""
        return any(self.conn.execute(
            f"SELECT 1 FROM question_groups WHERE id IN ({','.join('?' * len(chunk))}) AND {where} LIMIT 1", (*chunk, *args)
        ).fetchone() for chunk in _chunks(list(group_ids)))

    def group_ids(self, question_ids) -> list[int]:
        """The question_groups ids of these questions."""
        out: list[int] = []
        for chunk in _chunks(list(question_ids)):
            out += [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT group_id FROM questions WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
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

    def add_draft_item(self, draft_id: int, batch_id: int | None, question_id: int | None = None, status: str = "pending",
                       proposal: str = "", reason: str = "") -> int:
        """A question in the draft, numbered after the draft's others."""
        with self.conn:
            number = self.conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM draft_items WHERE draft_id=?",
                                       (draft_id,)).fetchone()[0]
            return self.conn.execute(
                "INSERT INTO draft_items (draft_id, batch_id, number, question_id, status, proposal, reason) "
                "VALUES (?,?,?,?,?,?,?)", (draft_id, batch_id, number, question_id, status, proposal, reason),
            ).lastrowid

    def draft_item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM draft_items WHERE id=?", (item_id,)).fetchone()

    def draft_items(self, draft_id: int, batch_id: int | None = None) -> list[sqlite3.Row]:
        """The draft's questions (or one batch's), in order, without those Claude removed."""
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

    def add_question_rewrite(self, question_id: int, original: dict) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO question_rewrites (question_id, original, created_at) VALUES (?,?,?)",
                                     (question_id, json.dumps(original, ensure_ascii=False), time.time())).lastrowid

    def question_rewrite(self, question_id: int) -> sqlite3.Row | None:
        """The question's last rewrite by Claude, if any (to undo it)."""
        return self.conn.execute("SELECT * FROM question_rewrites WHERE question_id=? ORDER BY id DESC LIMIT 1",
                                 (question_id,)).fetchone()

    def delete_question_rewrite(self, rewrite_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM question_rewrites WHERE id=?", (rewrite_id,))

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
