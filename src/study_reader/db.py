"""SQLite persistence: library, chats, messages and their book snippets, notes."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

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
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
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
    feedback TEXT NOT NULL DEFAULT '', -- Claude's, for free responses; the error for 'error'
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
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def data_dir() -> Path:
    override = os.environ.get("STUDY_READER_DATA")
    base = Path(override) if override else Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    ) / "study-reader"
    base.mkdir(parents=True, exist_ok=True)
    return base


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else str(data_dir() / "study-reader.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

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
        if "source_url" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN source_url TEXT")
        if "finished_at" not in book_cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN finished_at REAL")
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
        calc_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(calculations)")}
        if "latex" not in calc_cols:
            self.conn.execute("ALTER TABLE calculations ADD COLUMN latex TEXT NOT NULL DEFAULT ''")

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
        with self.conn:
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
            self.conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    # -- messages ------------------------------------------------------------
    def add_message(
        self, chat_id: int, role: str, text: str, snippets: list[dict] | None = None,
        session_id: str | None = None, session_uuid: str | None = None,
    ) -> int:
        """snippets: dicts with kind, page, text, image_path, rects."""
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO messages (chat_id, role, text, session_id, session_uuid, created_at) VALUES (?,?,?,?,?,?)",
                (chat_id, role, text, session_id, session_uuid, now),
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
        """Drop a message and everything after it in its chat (their snippets go too)."""
        with self.conn:
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
    def add_calculation(self, book_id: int, expr: str, result: str, value: str, latex: str = "") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO calculations (book_id, expr, latex, result, value, created_at) VALUES (?,?,?,?,?,?)",
                (book_id, expr, latex, result, value, time.time()),
            )
        return cur.lastrowid

    def calculations(self, book_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM calculations WHERE book_id=? ORDER BY id", (book_id,)).fetchall()

    def delete_calculation(self, calc_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM calculations WHERE id=?", (calc_id,))

    def clear_calculations(self, book_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM calculations WHERE book_id=?", (book_id,))

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
