"""Syncing practice questions with the Android companion app (android/) through a shared folder
that Syncthing keeps in step between the computer and the phone. Qt-free.

The desktop stays the source of truth. Each side writes only its own files, so Syncthing never
sees a conflict:

    <folder>/desktop/snapshot.db     questions, decks, schedules, the attempt log and the practice
                                     settings, rewritten whole (atomically) when something changed
    <folder>/desktop/media/          the question media (images), mirrored from media_dir()
    <folder>/phones/<device>/<batch>.json
                                     what the phone did, one immutable file per batch of events

The snapshot's tables are SNAPSHOT_SCHEMA; its meta table holds the snapshot's "format", "schema"
(SNAPSHOT_VERSION: 2 since the tables took their names, questions, question_groups and attempts;
the phone refuses an older one), "id", "made_at" and "day_starts_at_hour".

A batch is {"format": 2, "device", "batch", "events": [...]}; every event has a unique "id",
a "type", a "question" (the question's id, where it applies to one) and "at" (epoch seconds):

    attempt       rating 1-4, duration (ms), response (JSON as practice.html sends it, or null),
                  verdict, new (the question was new on the phone)
    suspend       suspended (0/1)
    flag          flag (0-7)
    bury          the question is put off until tomorrow (scheduled ones only; the phone hides new
                  ones itself)
    xp            amount: XP the phone awarded (gamify.py's rules run on the phone)
    achievement   key (older keys are read through gamify.ACHIEVEMENT_ALIASES)
    goal          day (ISO): the daily goal was met that day
    freeze_spent  day: a streak freeze covered that day
    freeze_earned day

Batches in format 1, from phones not yet updated, are read too: their events say "card" for
"question" and "review" for "attempt".

Attempts are replayed through FSRS at the time they happened. An attempt older than the
question's last one (it was also practiced here meanwhile) rebuilds the question's schedule from
its whole attempt log in time order. A batch is applied whole or not at all. Applied events are
remembered by id (phone_events), so a batch read twice, or an event sent again in a later batch,
changes nothing. They are kept for good: the phone sends an event again when it couldn't record
that its file was written, and nothing tells the desktop when that can no longer happen. The
snapshot lists the batches it already reflects (its acks table); the phone then deletes those
files and replays only its newer events on top of the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from fsrs import Card, Rating, State

from . import gamify, practice, variants
from .db import Database, media_dir

FORMAT = 2  # of the batches the phone writes
FORMATS = (1, 2)  # read: 1 is from before events said "question" and "attempt"
SNAPSHOT_FORMAT = 1
SNAPSHOT_VERSION = 2  # meta "schema": the snapshot's tables (2: questions, question_groups, attempts)
FOLDER_SETTING = "phone_sync_folder"  # the shared folder ('' while phone sync is off)
FINGERPRINT_SETTING = "phone_sync_fingerprint"  # what the last snapshot was made from
LAST_SYNC_SETTING = "phone_sync_at"
SNAPSHOT = "desktop/snapshot.db"
MEDIA = "desktop/media"
PHONES = "phones"
# Syncthing never syncs its own temporary files, so the snapshot is written under such a name first.
TEMP_PREFIX = ".syncthing."
# The practice settings the phone follows.
SETTINGS = (practice.NEW_PER_DAY_SETTING, practice.RETENTION_SETTING, practice.PARAMETERS_SETTING,
            gamify.ENABLED_SETTING, gamify.GOAL_SETTING, gamify.STATE_SETTING)
QUESTION_COLUMNS = ("id", "group_id", "ord", "deck_id", "book_id", "message_id", "quiz", "position", "title", "data",
                    "variant", "page", "stage", "fsrs", "due", "reps", "lapses", "suspended", "flag", "stability",
                    "difficulty", "last_attempt", "created_at", "updated_at")
# Format 1 event names: old -> new.
OLD_EVENT_TYPES = {"review": "attempt"}

SNAPSHOT_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, root TEXT NOT NULL, deck_id INTEGER);
CREATE TABLE section_starts (book_id INTEGER NOT NULL, tag TEXT NOT NULL, page INTEGER NOT NULL);
CREATE TABLE question_groups (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, tags TEXT NOT NULL);
CREATE TABLE questions (
    id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, ord INTEGER NOT NULL, deck_id INTEGER NOT NULL,
    book_id INTEGER, message_id INTEGER, quiz INTEGER, position INTEGER, title TEXT NOT NULL, data TEXT NOT NULL,
    variant TEXT NOT NULL, page INTEGER, stage INTEGER NOT NULL, fsrs TEXT NOT NULL, due REAL NOT NULL,
    reps INTEGER NOT NULL, lapses INTEGER NOT NULL, suspended INTEGER NOT NULL, flag INTEGER NOT NULL,
    stability REAL, difficulty REAL, last_attempt REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, rating INTEGER NOT NULL, source TEXT NOT NULL,
    attempted_at REAL NOT NULL, duration INTEGER NOT NULL, was_new INTEGER NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE acks (device TEXT NOT NULL, batch TEXT NOT NULL, PRIMARY KEY (device, batch));
"""
SNAPSHOT_INDEXES = """
CREATE INDEX questions_due ON questions (book_id, suspended, stage, due);
CREATE INDEX questions_deck ON questions (deck_id, suspended, stage, due);
CREATE INDEX attempts_time ON attempts (attempted_at);
CREATE INDEX attempts_question ON attempts (question_id);
"""


@dataclass
class Result:
    applied: int = 0  # events applied
    batches: int = 0  # batch files read for the first time
    exported: bool = False  # a new snapshot was written
    attempted: list[int] = field(default_factory=list)  # questions the phone attempted
    errors: list[str] = field(default_factory=list)  # batches that couldn't be read (yet)


def folder(db: Database) -> Path | None:
    raw = db.get_setting(FOLDER_SETTING)
    return Path(raw) if raw else None


@dataclass
class Export:
    """A snapshot to write: what it needs from the settings and the folder, read on the database's
    own thread (plan_export), so that write_snapshot can copy the rest from any connection."""
    root: Path
    now: float
    fingerprint: str  # of what it is made from, stored once it is written (finish_export)
    settings: list[tuple[str, str]]
    variants: bool  # new versions of questions are on (variants.ENABLED_SETTING)
    acks: list[tuple[str, str]]


def sync(db: Database, root: Path, now: float | None = None, force: bool = False, write=None) -> Result:
    """Apply what the phones sent, then write a new snapshot if anything changed (or force).
    write: called with the snapshot's plan (an Export) instead of writing it here, to write it on
    another thread (write_snapshot_apart, then finish_export back here)."""
    now = time.time() if now is None else now
    result = import_batches(db, root)
    plan = plan_export(db, root, now, force=force or bool(result.batches))
    if plan is not None:
        if write is None:
            write_snapshot(db.conn, plan)
            finish_export(db, plan)
        else:
            write(plan)
        result.exported = True
    db.set_setting(LAST_SYNC_SETTING, str(now))
    return result


# -- from the phone ------------------------------------------------------------------------
def batch_files(root: Path) -> list[tuple[str, str, Path]]:
    """Every phone batch in the folder: (device, batch, path), oldest first within a device."""
    out = []
    phones = root / PHONES
    if not phones.is_dir():
        return out
    for device in sorted(p for p in phones.iterdir() if p.is_dir()):
        for f in sorted(device.glob("*.json")):
            if not f.name.startswith(TEMP_PREFIX):
                out.append((device.name, f.stem, f))
    return out


def import_batches(db: Database, root: Path) -> Result:
    result = Result()
    done = {(r["device"], r["batch"]) for r in db.conn.execute("SELECT device, batch FROM phone_batches")}
    for device, batch, path in batch_files(root):
        if (device, batch) in done:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a batch")
            if data.get("format") not in FORMATS or not isinstance(data.get("events"), list):
                raise ValueError(f"unknown format {data.get('format')!r}")
        except (OSError, ValueError) as e:  # half-synced, most likely: read it next time
            result.errors.append(f"{device}/{path.name}: {e}")
            continue
        events = sorted((_current(e) for e in data["events"] if isinstance(e, dict) and e.get("id")),
                        key=lambda e: (_at(e), str(e["id"])))
        applied, attempted, errors = 0, [], []
        # A batch is applied whole or not at all (and then read again next time): its events, the
        # record of them and of the batch are one transaction.
        with db.transaction():
            seen = _applied_events(db, [str(e["id"]) for e in events])
            for event in events:
                if str(event["id"]) in seen:
                    continue
                seen.add(str(event["id"]))
                try:
                    with db.conn:  # a savepoint: an event that fails leaves nothing behind
                        if apply_event(db, event) and event.get("type") == "attempt":
                            attempted.append(int(event["question"]))
                except (KeyError, TypeError, ValueError) as e:
                    errors.append(f"{device}/{path.name}: event {event['id']}: {e}")
                db.conn.execute("INSERT OR IGNORE INTO phone_events (id, device, applied_at) VALUES (?,?,?)",
                                (str(event["id"]), device, time.time()))
                applied += 1
            db.conn.execute("INSERT OR IGNORE INTO phone_batches (device, batch, applied_at) VALUES (?,?,?)",
                            (device, batch, time.time()))
        result.applied += applied
        result.attempted += attempted
        result.errors += errors
        result.batches += 1
    return result


def _applied_events(db: Database, ids: list[str]) -> set[str]:
    """Which of these event ids were applied already (from any batch)."""
    seen: set[str] = set()
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        seen.update(r[0] for r in db.conn.execute(
            f"SELECT id FROM phone_events WHERE id IN ({','.join('?' * len(chunk))})", chunk))
    return seen


def _at(e: dict) -> float:
    """An event's time, for putting a batch in order (0 for one without a usable time, which
    apply_event then turns down by itself)."""
    try:
        return float(e.get("at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _current(e: dict) -> dict:
    """A format 1 event in format 2's names ("question" for "card", "attempt" for "review")."""
    if "card" in e and "question" not in e:
        e = {**{k: v for k, v in e.items() if k != "card"}, "question": e["card"]}
    if e.get("type") in OLD_EVENT_TYPES:
        e = {**e, "type": OLD_EVENT_TYPES[e["type"]]}
    return e


def apply_event(db: Database, e: dict) -> bool:
    """Apply one event from a phone (in format 2's names; _current makes them). False if it no
    longer applies (its question is gone, say)."""
    kind = e.get("type")
    at = float(e["at"])
    if kind in ("xp", "achievement", "goal", "freeze_spent", "freeze_earned"):
        _apply_reward(db, kind, e, at)
        return True
    question = db.question(int(e["question"]))
    if question is None:
        return False
    if kind == "attempt":
        apply_attempt(db, question, int(e["rating"]), at, e.get("response") or None, e.get("verdict"),
                      int(e.get("duration") or 0), bool(e.get("new")))
    elif kind == "suspend":
        db.update_question(question["id"], suspended=1 if e.get("suspended", 1) else 0, updated_at=at)
    elif kind == "flag":
        db.update_question(question["id"], flag=max(0, min(7, int(e.get("flag") or 0))), updated_at=at)
    elif kind == "bury":
        bury(db, question, at)
    else:
        return False
    return True


def apply_attempt(db: Database, question, rating: int, at: float, response: dict | None, verdict: str | None,
                  duration: int, new: bool) -> None:
    if rating not in (1, 2, 3, 4):
        raise ValueError(f"rating {rating}")
    if question["last_attempt"] is None or at >= question["last_attempt"]:
        practice.attempt(db, question["id"], rating, now=at, response=response, verdict=verdict, duration=duration)
        return
    # Practiced here too, after the phone's attempt: put the phone's in the log, then replay the log.
    db.record_attempt(question["id"], rating, "practice", json.dumps(response) if response else "", verdict, at,
                      duration, was_new=_introduced(db, question["id"], at, new))
    if not rebuild(db, question["id"]):
        # Nothing to replay from (history from Anki, say): schedule it as if attempted with the last one.
        fsrs_card, _ = practice.scheduler(db).review_card(practice._fsrs_card(question, at), Rating(rating),
                                                          practice.dt(question["last_attempt"]))
        db.update_question(question["id"], **practice.schedule_columns(fsrs_card), reps=question["reps"] + 1)


def _introduced(db: Database, question_id: int, at: float, new: bool) -> bool:
    """Whether a phone attempt at `at`, older than the question's last attempt here, introduced the
    question (attempts.was_new: its first attempt, or its first after a Forget), so that an
    introduction is marked once: the first attempt in the log always is one, and an attempt the
    phone made while the question was new in its copy (forgotten here before) is one if the next
    attempt here was. That next attempt then loses its mark."""
    earlier = db.conn.execute("SELECT 1 FROM attempts WHERE question_id=? AND attempted_at<=? LIMIT 1",
                              (question_id, at)).fetchone()
    after = db.conn.execute("SELECT id, was_new FROM attempts WHERE question_id=? AND attempted_at>? "
                            "ORDER BY attempted_at, id LIMIT 1", (question_id, at)).fetchone()
    if earlier is not None and not (new and after is not None and after["was_new"]):
        return False
    if after is not None and after["was_new"]:
        with db.conn:
            db.conn.execute("UPDATE attempts SET was_new=0 WHERE id=?", (after["id"],))
    return True


def rebuild(db: Database, question_id: int) -> bool:
    """Recompute a question's schedule by replaying its attempts in time order, from its last
    introduction (an attempt at it while new). False if its log can't be replayed."""
    attempts = db.conn.execute(
        "SELECT rating, attempted_at, was_new, source FROM attempts WHERE question_id=? ORDER BY attempted_at, id",
        (question_id,),
    ).fetchall()
    starts = [i for i, r in enumerate(attempts) if r["was_new"]]
    if not starts:
        return False
    attempts = attempts[starts[-1]:]
    if any(r["source"] == "anki" for r in attempts):
        return False
    sched = practice.scheduler(db)
    fsrs_card = Card(due=practice.dt(attempts[0]["attempted_at"]))
    lapses = 0
    for r in attempts:
        lapses += r["rating"] == 1 and fsrs_card.state == State.Review
        fsrs_card, _ = sched.review_card(fsrs_card, Rating(r["rating"]), practice.dt(r["attempted_at"]))
    question = db.question(question_id)
    db.update_question(question_id, **practice.schedule_columns(fsrs_card), reps=question["reps"] + 1,
                       lapses=max(question["lapses"], lapses))
    return True


def bury(db: Database, question, at: float) -> None:
    """Put a scheduled question off until tomorrow (the practice day after `at`). New questions
    stay as they are: the phone keeps those out of its own queue for the day."""
    if question["stage"] == 0 or question["suspended"]:
        return
    tomorrow = practice.next_day(at)
    if question["due"] >= tomorrow:
        return
    fsrs_card = Card.from_json(question["fsrs"]) if question["fsrs"] else Card(due=practice.dt(tomorrow))
    fsrs_card.due = practice.dt(tomorrow)
    db.update_question(question["id"], **practice.schedule_columns(fsrs_card), updated_at=at)


def _apply_reward(db: Database, kind: str, e: dict, at: float) -> None:
    state = gamify.load(db, at)
    if kind == "xp":
        state["xp"] += max(int(e.get("amount") or 0), 0)
    elif kind == "achievement":
        key = str(e["key"])
        key = gamify.achievement_key(key)
        if key in {a[0] for a in gamify.ACHIEVEMENTS}:
            state["achievements"].setdefault(key, at)
    elif kind == "goal":
        day = str(e["day"])
        if day > state["goal_day"]:
            state["goals"] += 1
            state["goal_day"] = day
    elif kind == "freeze_spent":
        day = str(e["day"])
        if day not in state["frozen"]:
            state["frozen"].append(day)
            state["freezes"] = max(state["freezes"] - 1, 0)
    elif kind == "freeze_earned":
        day = str(e["day"])
        if state["freeze_earned"] != day and state["freezes"] < gamify.MAX_FREEZES:
            state["freezes"] += 1
            state["freeze_earned"] = day
    gamify._save(db, state)


# -- to the phone --------------------------------------------------------------------------
def _fingerprint(db: Database, root: Path) -> str:
    """Changes whenever anything the snapshot holds does (cheaply: aggregates, not the rows), and
    with the snapshot's format and schema, so a desktop that changes them rewrites it once."""
    c = db.conn
    parts = [
        [SNAPSHOT_FORMAT, SNAPSHOT_VERSION],
        c.execute("SELECT COUNT(*), TOTAL(id*(stage+1)), TOTAL(due), TOTAL(suspended*id), TOTAL(flag*id), "
                  "TOTAL(length(data)+length(variant)), MAX(updated_at), TOTAL(deck_id*id), TOTAL(reps), "
                  "TOTAL(COALESCE(book_id, 0)*id) FROM questions").fetchone(),
        c.execute("SELECT COUNT(*), MAX(id), TOTAL(attempted_at) FROM attempts").fetchone(),
        c.execute("SELECT COUNT(*), MAX(updated_at), TOTAL(length(tags)*id) FROM question_groups").fetchone(),
        c.execute("SELECT group_concat(id||':'||name, '|') FROM decks").fetchone(),
        c.execute("SELECT group_concat(id||':'||title||':'||COALESCE(length(toc), 0)||':'||COALESCE(deck_id, ''), '|') "
                  "FROM books").fetchone(),
        [db.get_setting(k) for k in (*SETTINGS, variants.ENABLED_SETTING)],
        sorted(_acks(db, root)),
        _media_listing(),
    ]
    blob = json.dumps([list(p) if not isinstance(p, list) else p for p in parts], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _acks(db: Database, root: Path) -> list[tuple[str, str]]:
    """The applied batches whose files are still in the folder (the phone deletes the rest)."""
    present = {(d, b) for d, b, _ in batch_files(root)}
    return [(r["device"], r["batch"]) for r in db.conn.execute("SELECT device, batch FROM phone_batches")
            if (r["device"], r["batch"]) in present]


def _media_listing() -> list:
    d = media_dir()
    return sorted((f.name, f.stat().st_size, int(f.stat().st_mtime)) for f in d.iterdir() if f.is_file())


def plan_export(db: Database, root: Path, now: float | None = None, force: bool = False) -> Export | None:
    """The snapshot to write, if one is due: something it holds changed since the last one, there
    is none in the folder, or force."""
    now = time.time() if now is None else now
    gamify.load(db, now)  # made from the log the first time, so the phone starts from the same state
    fingerprint = _fingerprint(db, root)
    if not force and fingerprint == db.get_setting(FINGERPRINT_SETTING) and (root / SNAPSHOT).exists():
        return None
    return Export(root, now, fingerprint, [(k, db.get_setting(k)) for k in SETTINGS if db.get_setting(k) != ""],
                  variants.enabled(db), _acks(db, root))


def finish_export(db: Database, plan: Export) -> None:
    """Remember what the written snapshot was made from. (What changed while it was being
    written shows as a change next time.)"""
    db.set_setting(FINGERPRINT_SETTING, plan.fingerprint)


def export_snapshot(db: Database, root: Path, now: float | None = None) -> Path:
    """Write the snapshot for the phone (and mirror the media), whether or not anything changed."""
    return write_snapshot(db.conn, plan_export(db, root, now, force=True))


def write_snapshot_apart(db_path: str, plan: Export) -> Path:
    """write_snapshot for a thread of its own: it reads the database through a read-only
    connection of its own, in one read transaction, so the tables it copies agree with each other
    while the app goes on writing (the database is in WAL mode)."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        return write_snapshot(conn, plan)
    finally:
        conn.close()


def write_snapshot(conn: sqlite3.Connection, plan: Export) -> Path:
    """Write the snapshot plan_export planned, from the database connection given (and mirror
    the media)."""
    out = plan.root / SNAPSHOT
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{TEMP_PREFIX}{out.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with closing(sqlite3.connect(tmp)) as snap:
            _fill_snapshot(snap, conn, plan)
        os.replace(tmp, out)
    except BaseException:  # never leave a half-written copy behind
        for leftover in (tmp, tmp.with_name(tmp.name + "-journal")):
            leftover.unlink(missing_ok=True)
        raise
    mirror_media(plan.root)
    return out


def _fill_snapshot(snap: sqlite3.Connection, conn: sqlite3.Connection, plan: Export) -> None:
    snap.executescript(SNAPSHOT_SCHEMA)
    meta = {"format": SNAPSHOT_FORMAT, "schema": SNAPSHOT_VERSION, "id": uuid.uuid4().hex, "made_at": plan.now,
            "day_starts_at_hour": practice.DAY_STARTS_AT_HOUR}
    snap.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    snap.executemany("INSERT INTO decks VALUES (?,?)", conn.execute("SELECT id, name FROM decks"))
    books = conn.execute("SELECT id, title, toc, deck_id FROM books").fetchall()
    snap.executemany("INSERT INTO books VALUES (?,?,?,?)",
                     [(b["id"], b["title"], practice.section_root(b), b["deck_id"]) for b in books])
    snap.executemany("INSERT INTO section_starts VALUES (?,?,?)", [
        (b["id"], tag, page) for b in books
        for tag, page in practice._section_starts(practice.section_root(b), b["toc"]).items()
    ])
    snap.executemany("INSERT INTO question_groups VALUES (?,?,?)",
                     conn.execute("SELECT id, kind, tags FROM question_groups"))
    cols = ", ".join(QUESTION_COLUMNS)
    # With new versions turned off (variants.ENABLED_SETTING) the phone shows the questions as written.
    shown = cols.replace("variant", "variant" if plan.variants else "'' AS variant")
    snap.executemany(f"INSERT INTO questions ({cols}) VALUES ({', '.join('?' * len(QUESTION_COLUMNS))})",
                     conn.execute(f"SELECT {shown} FROM questions"))
    snap.executemany("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", conn.execute(
        "SELECT id, question_id, rating, source, attempted_at, duration, was_new FROM attempts"))
    snap.executemany("INSERT INTO settings VALUES (?,?)", plan.settings)
    snap.executemany("INSERT INTO acks VALUES (?,?)", plan.acks)
    snap.executescript(SNAPSHOT_INDEXES)
    snap.commit()


def mirror_media(root: Path) -> int:
    """Copy media files that are new or changed into the folder, and drop ones that are gone.
    Returns how many were copied."""
    src, dst = media_dir(), root / MEDIA
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    names = set()
    for f in src.iterdir():
        if not f.is_file():
            continue
        names.add(f.name)
        target = dst / f.name
        st = f.stat()
        if target.exists():
            tt = target.stat()
            if tt.st_size == st.st_size and int(tt.st_mtime) == int(st.st_mtime):
                continue
        tmp = dst / f"{TEMP_PREFIX}{f.name}.tmp"
        shutil.copy2(f, tmp)
        os.replace(tmp, target)
        copied += 1
    for f in dst.iterdir():
        if f.is_file() and f.name not in names and not f.name.startswith(TEMP_PREFIX):
            f.unlink()
    return copied


def status(db: Database) -> dict:
    """For the settings dialog: the folder, when it last synced, and what's waiting in it."""
    root = folder(db)
    at = db.get_setting(LAST_SYNC_SETTING)
    devices = sorted({d for d, _, _ in batch_files(root)}) if root and root.is_dir() else []
    return {"folder": str(root) if root else "", "last": float(at) if at else None, "devices": devices,
            "phones": db.conn.execute("SELECT COUNT(DISTINCT device) FROM phone_events").fetchone()[0]}
