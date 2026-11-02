import json
import sqlite3

from study_reader import gamify, phone_sync, srs
from study_reader.db import media_dir

from test_quiz import QUIZ
from test_srs import NOW, setup

DAY = 86400


def make(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    db, book_id, chat, reply = setup(tmp_path)
    ids = srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    root = tmp_path / "sync"
    root.mkdir()
    return db, ids, root


def write_batch(root, events, device="pixel", batch="0001"):
    d = root / "phones" / device
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{batch}.json"
    path.write_text(json.dumps({"format": 1, "device": device, "batch": batch, "events": events}))
    return path


def review(card, at, rating=3, eid=None, new=False):
    return {"id": eid or f"r{card}-{at}", "type": "review", "card": card, "at": at, "rating": rating,
            "duration": 4000, "response": None, "verdict": None, "new": new}


def snapshot(root):
    conn = sqlite3.connect(root / phone_sync.SNAPSHOT)
    conn.row_factory = sqlite3.Row
    return conn


def test_snapshot_holds_cards_settings_and_media(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    srs.review(db, ids[0], 3, now=NOW)
    db.set_setting(srs.NEW_PER_DAY_SETTING, "7")
    (media_dir() / "fig.png").write_bytes(b"png")
    result = phone_sync.sync(db, root, now=NOW)
    assert result.exported
    snap = snapshot(root)
    assert [r["id"] for r in snap.execute("SELECT id FROM cards ORDER BY id")] == ids
    assert snap.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    settings = dict(snap.execute("SELECT key, value FROM settings").fetchall())
    assert settings[srs.NEW_PER_DAY_SETTING] == "7" and gamify.STATE_SETTING in settings
    assert dict(snap.execute("SELECT key, value FROM meta").fetchall())["format"] == "1"
    assert snap.execute("SELECT root FROM books").fetchone()[0] == "A"
    assert (root / phone_sync.MEDIA / "fig.png").read_bytes() == b"png"
    # Nothing changed: no new snapshot; a change makes one.
    assert not phone_sync.sync(db, root, now=NOW).exported
    db.update_card(ids[1], suspended=1)
    assert phone_sync.sync(db, root, now=NOW).exported
    (media_dir() / "fig.png").unlink()
    phone_sync.sync(db, root, now=NOW)
    assert not (root / phone_sync.MEDIA / "fig.png").exists()


def test_phone_reviews_are_applied_once_and_acknowledged(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    path = write_batch(root, [review(ids[0], NOW + 60, new=True), review(ids[0], NOW + 3 * DAY, eid="b")])
    result = phone_sync.sync(db, root, now=NOW + 3 * DAY)
    assert (result.applied, result.batches, result.reviewed) == (2, 1, [ids[0], ids[0]])
    card = db.card(ids[0])
    assert card["reps"] == 2 and card["last_review"] == NOW + 3 * DAY
    logs = db.reviews(ids[0])
    assert [r["source"] for r in logs] == ["review", "review"] and logs[0]["was_new"] == 1
    assert db.new_cards_reviewed_since(NOW) == 1  # phone introductions count against the daily cap
    assert [tuple(r) for r in snapshot(root).execute("SELECT device, batch FROM acks")] == [("pixel", "0001")]
    # Read again (the phone hasn't deleted it yet): nothing happens twice.
    assert phone_sync.sync(db, root).applied == 0 and db.card(ids[0])["reps"] == 2
    # The same events in a second file are skipped by id.
    write_batch(root, [review(ids[0], NOW + 60, new=True)], batch="0002")
    assert phone_sync.import_batches(db, root).applied == 0
    # Once the phone deletes the files, the snapshot stops acknowledging them.
    path.unlink()
    (root / "phones" / "pixel" / "0002.json").unlink()
    phone_sync.sync(db, root)
    assert snapshot(root).execute("SELECT COUNT(*) FROM acks").fetchone()[0] == 0


def test_half_synced_batch_waits(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    path = write_batch(root, [review(ids[0], NOW)])
    path.write_text('{"format": 1, "events": [')
    result = phone_sync.sync(db, root, now=NOW)
    assert result.errors and result.applied == 0 and db.card(ids[0])["reps"] == 0
    write_batch(root, [review(ids[0], NOW)])
    assert phone_sync.sync(db, root, now=NOW).applied == 1


def test_older_phone_review_replays_the_log(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    srs.review(db, ids[0], 3, now=NOW)
    srs.review(db, ids[0], 3, now=NOW + 5 * DAY)  # here, after the phone's review below
    write_batch(root, [review(ids[0], NOW + 2 * DAY, rating=1)])
    phone_sync.sync(db, root, now=NOW + 5 * DAY)
    card = db.card(ids[0])
    assert card["reps"] == 3 and card["last_review"] == NOW + 5 * DAY
    assert [r["rating"] for r in sorted(db.reviews(ids[0]), key=lambda r: r["reviewed_at"])] == [3, 1, 3]
    # The same as reviewing in that order here.
    (tmp_path / "other").mkdir()
    db2, ids2, _ = make(tmp_path / "other", monkeypatch)
    for at, rating in ((NOW, 3), (NOW + 2 * DAY, 1), (NOW + 5 * DAY, 3)):
        srs.review(db2, ids2[0], rating, now=at)
    other = db2.card(ids2[0])
    assert (card["stage"], card["stability"], card["difficulty"]) == (other["stage"], other["stability"], other["difficulty"])


def test_suspend_flag_and_bury(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    srs.review(db, ids[1], 1, now=NOW)  # learning, due in minutes
    write_batch(root, [
        {"id": "s", "type": "suspend", "card": ids[0], "at": NOW, "suspended": 1},
        {"id": "f", "type": "flag", "card": ids[2], "at": NOW, "flag": 3},
        {"id": "b", "type": "bury", "card": ids[1], "at": NOW + 60},
        {"id": "n", "type": "bury", "card": ids[3], "at": NOW + 60},  # new: left alone
        {"id": "g", "type": "suspend", "card": 99999, "at": NOW},  # gone: ignored
    ])
    phone_sync.sync(db, root, now=NOW)
    assert db.card(ids[0])["suspended"] == 1 and db.card(ids[2])["flag"] == 3
    buried = db.card(ids[1])
    assert buried["due"] == srs.next_day(NOW + 60) and json.loads(buried["fsrs"])["due"].startswith("20")
    assert db.card(ids[3])["stage"] == 0


def test_rewards_merge(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    before = gamify.load(db, NOW)
    write_batch(root, [
        {"id": "x", "type": "xp", "at": NOW, "amount": 15},
        {"id": "a", "type": "achievement", "at": NOW, "key": "first"},
        {"id": "z", "type": "achievement", "at": NOW, "key": "no_such_thing"},
        {"id": "g", "type": "goal", "at": NOW, "day": "2027-01-15"},
        {"id": "g2", "type": "goal", "at": NOW, "day": "2027-01-15"},
        {"id": "f", "type": "freeze_spent", "at": NOW, "day": "2027-01-14"},
        {"id": "e", "type": "freeze_earned", "at": NOW, "day": "2027-01-15"},
    ])
    phone_sync.sync(db, root, now=NOW)
    state = gamify.load(db, NOW)
    assert state["xp"] == before["xp"] + 15 and "first" in state["achievements"]
    assert "no_such_thing" not in state["achievements"]
    assert (state["goals"], state["goal_day"]) == (before["goals"] + 1, "2027-01-15")
    assert state["frozen"] == ["2027-01-14"]
    assert state["freezes"] == before["freezes"] - 1 + 1 and state["freeze_earned"] == "2027-01-15"
