"""Syncing review cards with the Android companion app (android/) through a shared folder that
Syncthing keeps in step between the computer and the phone. Qt-free.

The desktop stays the source of truth. Each side writes only its own files, so Syncthing never
sees a conflict:

    <folder>/desktop/snapshot.db     cards, decks, schedules, the review log and the review settings,
                                     rewritten whole (atomically) when something changed
    <folder>/desktop/media/          the card media (images), mirrored from media_dir()
    <folder>/phones/<device>/<batch>.json
                                     what the phone did, one immutable file per batch of events

A batch is {"format": 1, "device", "batch", "events": [...]}; every event has a unique "id",
a "type", a "card" (where it applies to one) and "at" (epoch seconds):

    review        rating 1-4, duration (ms), response (JSON as review.html sends it, or null),
                  verdict, new (the card was new on the phone)
    suspend       suspended (0/1)
    flag          flag (0-7)
    bury          the card is put off until tomorrow (scheduled cards only; the phone hides new ones itself)
    xp            amount: XP the phone awarded (gamify.py's rules run on the phone)
    achievement   key
    goal          day (ISO): the daily goal was met that day
    freeze_spent  day: a streak freeze covered that day
    freeze_earned day

Reviews are replayed through FSRS at the time they happened. A review older than the card's last
review (the card was also reviewed here meanwhile) rebuilds the card's schedule from its whole
review log in time order. Applied events are remembered by id (phone_events), so a batch read
twice changes nothing. The snapshot lists the batches it already reflects (its acks table); the
phone then deletes those files and replays only its newer events on top of the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fsrs import Card, Rating, State

from . import gamify, srs, variants
from .db import Database, media_dir

FORMAT = 1
FOLDER_SETTING = "phone_sync_folder"  # the shared folder ('' while phone sync is off)
FINGERPRINT_SETTING = "phone_sync_fingerprint"  # what the last snapshot was made from
LAST_SYNC_SETTING = "phone_sync_at"
SNAPSHOT = "desktop/snapshot.db"
MEDIA = "desktop/media"
PHONES = "phones"
# Syncthing never syncs its own temporary files, so the snapshot is written under such a name first.
TEMP_PREFIX = ".syncthing."
# The review settings the phone follows.
SETTINGS = (srs.NEW_PER_DAY_SETTING, srs.RETENTION_SETTING, srs.PARAMETERS_SETTING, gamify.ENABLED_SETTING,
            gamify.GOAL_SETTING, gamify.STATE_SETTING)
CARD_COLUMNS = ("id", "note_id", "ord", "deck_id", "book_id", "message_id", "quiz", "question", "title", "data",
                "variant", "page", "stage", "fsrs", "due", "reps", "lapses", "suspended", "flag", "stability",
                "difficulty", "last_review", "created_at", "updated_at")

SNAPSHOT_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, root TEXT NOT NULL, deck_id INTEGER);
CREATE TABLE section_starts (book_id INTEGER NOT NULL, tag TEXT NOT NULL, page INTEGER NOT NULL);
CREATE TABLE card_notes (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, tags TEXT NOT NULL);
CREATE TABLE cards (
    id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL, ord INTEGER NOT NULL, deck_id INTEGER NOT NULL,
    book_id INTEGER, message_id INTEGER, quiz INTEGER, question INTEGER, title TEXT NOT NULL, data TEXT NOT NULL,
    variant TEXT NOT NULL, page INTEGER, stage INTEGER NOT NULL, fsrs TEXT NOT NULL, due REAL NOT NULL,
    reps INTEGER NOT NULL, lapses INTEGER NOT NULL, suspended INTEGER NOT NULL, flag INTEGER NOT NULL,
    stability REAL, difficulty REAL, last_review REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL, rating INTEGER NOT NULL, source TEXT NOT NULL,
    reviewed_at REAL NOT NULL, duration INTEGER NOT NULL, was_new INTEGER NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE acks (device TEXT NOT NULL, batch TEXT NOT NULL, PRIMARY KEY (device, batch));
"""
SNAPSHOT_INDEXES = """
CREATE INDEX cards_due ON cards (book_id, suspended, stage, due);
CREATE INDEX cards_deck ON cards (deck_id, suspended, stage, due);
CREATE INDEX reviews_time ON reviews (reviewed_at);
CREATE INDEX reviews_card ON reviews (card_id);
"""


@dataclass
class Result:
    applied: int = 0  # events applied
    batches: int = 0  # batch files read for the first time
    exported: bool = False  # a new snapshot was written
    reviewed: list[int] = field(default_factory=list)  # cards the phone reviewed
    errors: list[str] = field(default_factory=list)  # batches that couldn't be read (yet)


def folder(db: Database) -> Path | None:
    raw = db.get_setting(FOLDER_SETTING)
    return Path(raw) if raw else None


def sync(db: Database, root: Path, now: float | None = None, force: bool = False) -> Result:
    """Apply what the phones sent, then write a new snapshot if anything changed (or force)."""
    now = time.time() if now is None else now
    result = import_batches(db, root)
    fingerprint = _fingerprint(db, root)
    if force or result.batches or fingerprint != db.get_setting(FINGERPRINT_SETTING) \
            or not (root / SNAPSHOT).exists():
        export_snapshot(db, root, now)
        db.set_setting(FINGERPRINT_SETTING, _fingerprint(db, root))
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
            if data.get("format") != FORMAT or not isinstance(data.get("events"), list):
                raise ValueError(f"unknown format {data.get('format')!r}")
        except (OSError, ValueError) as e:  # half-synced, most likely: read it next time
            result.errors.append(f"{device}/{path.name}: {e}")
            continue
        events = sorted((e for e in data["events"] if isinstance(e, dict) and e.get("id")),
                        key=lambda e: (float(e.get("at") or 0), str(e["id"])))
        with db.conn:
            seen = {r[0] for r in db.conn.execute("SELECT id FROM phone_events WHERE device=?", (device,))}
            for event in events:
                if event["id"] in seen:
                    continue
                try:
                    if apply_event(db, event) and event.get("type") == "review":
                        result.reviewed.append(int(event["card"]))
                except (KeyError, TypeError, ValueError) as e:
                    result.errors.append(f"{device}/{path.name}: event {event['id']}: {e}")
                db.conn.execute("INSERT OR IGNORE INTO phone_events (id, device, applied_at) VALUES (?,?,?)",
                                (event["id"], device, time.time()))
                result.applied += 1
            db.conn.execute("INSERT OR IGNORE INTO phone_batches (device, batch, applied_at) VALUES (?,?,?)",
                            (device, batch, time.time()))
        result.batches += 1
    return result


def apply_event(db: Database, e: dict) -> bool:
    """Apply one event from a phone. False if it no longer applies (its card is gone, say)."""
    kind = e.get("type")
    at = float(e["at"])
    if kind in ("xp", "achievement", "goal", "freeze_spent", "freeze_earned"):
        _apply_reward(db, kind, e, at)
        return True
    card = db.card(int(e["card"]))
    if card is None:
        return False
    if kind == "review":
        apply_review(db, card, int(e["rating"]), at, e.get("response") or None, e.get("verdict"),
                     int(e.get("duration") or 0), bool(e.get("new")))
    elif kind == "suspend":
        db.update_card(card["id"], suspended=1 if e.get("suspended", 1) else 0, updated_at=at)
    elif kind == "flag":
        db.update_card(card["id"], flag=max(0, min(7, int(e.get("flag") or 0))), updated_at=at)
    elif kind == "bury":
        bury(db, card, at)
    else:
        return False
    return True


def apply_review(db: Database, card, rating: int, at: float, response: dict | None, verdict: str | None,
                 duration: int, new: bool) -> None:
    if rating not in (1, 2, 3, 4):
        raise ValueError(f"rating {rating}")
    if card["last_review"] is None or at >= card["last_review"]:
        srs.review(db, card["id"], rating, now=at, response=response, verdict=verdict, duration=duration)
        return
    # Reviewed here too, after the phone's review: put the phone's in the log, then replay the log.
    db.record_review(card["id"], rating, "review", json.dumps(response) if response else "", verdict, at,
                     duration, was_new=new)
    if not rebuild(db, card["id"]):
        # Nothing to replay from (history from Anki, say): schedule it as if reviewed with the last one.
        fsrs_card, _ = srs.scheduler(db).review_card(srs._fsrs_card(card, at), Rating(rating), srs.dt(card["last_review"]))
        db.update_card(card["id"], **srs.schedule_columns(fsrs_card), reps=card["reps"] + 1)


def rebuild(db: Database, card_id: int) -> bool:
    """Recompute a card's schedule by replaying its reviews in time order, from its last
    introduction (a review of it while new). False if its log can't be replayed."""
    reviews = db.conn.execute(
        "SELECT rating, reviewed_at, was_new, source FROM reviews WHERE card_id=? ORDER BY reviewed_at, id", (card_id,),
    ).fetchall()
    starts = [i for i, r in enumerate(reviews) if r["was_new"]]
    if not starts:
        return False
    reviews = reviews[starts[-1]:]
    if any(r["source"] == "anki" for r in reviews):
        return False
    sched = srs.scheduler(db)
    fsrs_card = Card(due=srs.dt(reviews[0]["reviewed_at"]))
    lapses = 0
    for r in reviews:
        lapses += r["rating"] == 1 and fsrs_card.state == State.Review
        fsrs_card, _ = sched.review_card(fsrs_card, Rating(r["rating"]), srs.dt(r["reviewed_at"]))
    card = db.card(card_id)
    db.update_card(card_id, **srs.schedule_columns(fsrs_card), reps=card["reps"] + 1, lapses=max(card["lapses"], lapses))
    return True


def bury(db: Database, card, at: float) -> None:
    """Put a scheduled card off until tomorrow (the review day after `at`). New cards stay as
    they are: the phone keeps those out of its own queue for the day."""
    if card["stage"] == 0 or card["suspended"]:
        return
    tomorrow = srs.next_day(at)
    if card["due"] >= tomorrow:
        return
    fsrs_card = Card.from_json(card["fsrs"]) if card["fsrs"] else Card(due=srs.dt(tomorrow))
    fsrs_card.due = srs.dt(tomorrow)
    db.update_card(card["id"], **srs.schedule_columns(fsrs_card), updated_at=at)


def _apply_reward(db: Database, kind: str, e: dict, at: float) -> None:
    state = gamify.load(db, at)
    if kind == "xp":
        state["xp"] += max(int(e.get("amount") or 0), 0)
    elif kind == "achievement":
        key = str(e["key"])
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
    """Changes whenever anything the snapshot holds does (cheaply: aggregates, not the rows)."""
    c = db.conn
    parts = [
        c.execute("SELECT COUNT(*), TOTAL(id*(stage+1)), TOTAL(due), TOTAL(suspended*id), TOTAL(flag*id), "
                  "TOTAL(length(data)+length(variant)), MAX(updated_at), TOTAL(deck_id*id), TOTAL(reps), "
                  "TOTAL(COALESCE(book_id, 0)*id) FROM cards").fetchone(),
        c.execute("SELECT COUNT(*), MAX(id), TOTAL(reviewed_at) FROM reviews").fetchone(),
        c.execute("SELECT COUNT(*), MAX(updated_at), TOTAL(length(tags)*id) FROM card_notes").fetchone(),
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


def export_snapshot(db: Database, root: Path, now: float | None = None) -> Path:
    """Write the snapshot for the phone (and mirror the media)."""
    now = time.time() if now is None else now
    out = root / SNAPSHOT
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{TEMP_PREFIX}{out.name}.{uuid.uuid4().hex[:8]}.tmp")
    snap = sqlite3.connect(tmp)
    try:
        snap.executescript(SNAPSHOT_SCHEMA)
        meta = {"format": FORMAT, "id": uuid.uuid4().hex, "made_at": now, "day_starts_at_hour": srs.DAY_STARTS_AT_HOUR}
        snap.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
        snap.executemany("INSERT INTO decks VALUES (?,?)", db.conn.execute("SELECT id, name FROM decks"))
        books = db.conn.execute("SELECT id, title, toc, deck_id FROM books").fetchall()
        snap.executemany("INSERT INTO books VALUES (?,?,?,?)",
                         [(b["id"], b["title"], srs.section_root(b), b["deck_id"]) for b in books])
        snap.executemany("INSERT INTO section_starts VALUES (?,?,?)", [
            (b["id"], tag, page) for b in books
            for tag, page in srs._section_starts(srs.section_root(b), b["toc"]).items()
        ])
        snap.executemany("INSERT INTO card_notes VALUES (?,?,?)", db.conn.execute("SELECT id, kind, tags FROM card_notes"))
        cols = ", ".join(CARD_COLUMNS)
        # With new versions turned off (variants.ENABLED_SETTING) the phone shows the cards' own questions.
        shown = cols.replace("variant", "variant" if variants.enabled(db) else "'' AS variant")
        snap.executemany(f"INSERT INTO cards ({cols}) VALUES ({', '.join('?' * len(CARD_COLUMNS))})",
                         db.conn.execute(f"SELECT {shown} FROM cards"))
        snap.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)", db.conn.execute(
            "SELECT id, card_id, rating, source, reviewed_at, duration, was_new FROM reviews"))
        gamify.load(db, now)  # made from the log the first time, so the phone starts from the same state
        snap.executemany("INSERT INTO settings VALUES (?,?)",
                         [(k, db.get_setting(k)) for k in SETTINGS if db.get_setting(k) != ""])
        snap.executemany("INSERT INTO acks VALUES (?,?)", _acks(db, root))
        snap.executescript(SNAPSHOT_INDEXES)
        snap.commit()
    finally:
        snap.close()
    os.replace(tmp, out)
    mirror_media(root)
    return out


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
