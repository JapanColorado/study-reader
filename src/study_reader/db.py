"""SQLite persistence: library, chats, messages and their book snippets."""

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
    base_session_id TEXT,
    context_mode TEXT,              -- NULL (not prepared) | 'full' | 'partial'
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
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "model" not in cols:
            # Existing chats keep the model that was selected globally before chats had their own.
            self.conn.execute("ALTER TABLE chats ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE chats SET model=COALESCE((SELECT value FROM settings WHERE key='model'), '')"
            )

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

    def book(self, book_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()

    def recent_books(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM books ORDER BY opened_at DESC").fetchall()

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
    def add_message(self, chat_id: int, role: str, text: str, snippets: list[dict] | None = None) -> int:
        """snippets: dicts with kind, page, text, image_path, rects."""
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?,?,?,?)",
                (chat_id, role, text, now),
            )
            for s in snippets or []:
                self.conn.execute(
                    "INSERT INTO snippets (message_id, kind, page, text, image_path, rects) VALUES (?,?,?,?,?,?)",
                    (cur.lastrowid, s["kind"], s["page"], s.get("text", ""), s.get("image_path", ""),
                     json.dumps(s.get("rects", []))),
                )
            self.conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
        return cur.lastrowid

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
