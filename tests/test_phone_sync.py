import json
import sqlite3

from study_reader import gamify, phone_sync, practice, question_model
from study_reader.db import media_dir

from test_quiz import QUIZ
from test_practice import NOW, setup

DAY = 86400


def make(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    db, book_id, chat, reply = setup(tmp_path)
    ids = practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    root = tmp_path / "sync"
    root.mkdir()
    return db, ids, root


def write_batch(root, events, device="pixel", batch="0001", format=phone_sync.FORMAT):
    d = root / "phones" / device
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{batch}.json"
    path.write_text(json.dumps({"format": format, "device": device, "batch": batch, "events": events}))
    return path


def attempt(question, at, rating=3, eid=None, new=False):
    return {"id": eid or f"r{question}-{at}", "type": "attempt", "question": question, "at": at, "rating": rating,
            "duration": 4000, "response": None, "verdict": None, "new": new}


def snapshot(root):
    conn = sqlite3.connect(root / phone_sync.SNAPSHOT)
    conn.row_factory = sqlite3.Row
    return conn


def test_snapshot_holds_questions_settings_and_media(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    practice.attempt(db, ids[0], 3, now=NOW)
    db.set_setting(practice.NEW_PER_DAY_SETTING, "7")
    (media_dir() / "fig.png").write_bytes(b"png")
    result = phone_sync.sync(db, root, now=NOW)
    assert result.exported
    snap = snapshot(root)
    assert [r["id"] for r in snap.execute("SELECT id FROM questions ORDER BY id")] == ids
    assert snap.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert snap.execute("SELECT source FROM attempts").fetchone()[0] == "practice"
    question = snap.execute("SELECT group_id, position, last_attempt FROM questions WHERE id=?", (ids[0],)).fetchone()
    assert question["group_id"] and question["position"] == 0 and question["last_attempt"] == NOW
    assert {r["kind"] for r in snap.execute("SELECT kind FROM question_groups")} <= set(question_model.TYPES)
    indexes = {r[0] for r in snap.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}
    assert indexes == {"questions_due", "questions_deck", "attempts_time", "attempts_question"}
    settings = dict(snap.execute("SELECT key, value FROM settings").fetchall())
    assert settings[practice.NEW_PER_DAY_SETTING] == "7" and gamify.STATE_SETTING in settings
    meta = dict(snap.execute("SELECT key, value FROM meta").fetchall())
    assert (meta["format"], meta["schema"]) == ("1", "2")
    assert snap.execute("SELECT root FROM books").fetchone()[0] == "A"
    assert (root / phone_sync.MEDIA / "fig.png").read_bytes() == b"png"
    # Nothing changed: no new snapshot; a change makes one.
    assert not phone_sync.sync(db, root, now=NOW).exported
    db.update_question(ids[1], suspended=1)
    assert phone_sync.sync(db, root, now=NOW).exported
    # A desktop whose snapshot has a newer schema rewrites it, though nothing else changed.
    monkeypatch.setattr(phone_sync, "SNAPSHOT_VERSION", phone_sync.SNAPSHOT_VERSION + 1)
    assert phone_sync.sync(db, root, now=NOW).exported
    assert not phone_sync.sync(db, root, now=NOW).exported
    (media_dir() / "fig.png").unlink()
    phone_sync.sync(db, root, now=NOW)
    assert not (root / phone_sync.MEDIA / "fig.png").exists()


def test_phone_attempts_are_applied_once_and_acknowledged(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    path = write_batch(root, [attempt(ids[0], NOW + 60, new=True), attempt(ids[0], NOW + 3 * DAY, eid="b")])
    result = phone_sync.sync(db, root, now=NOW + 3 * DAY)
    assert (result.applied, result.batches, result.attempted) == (2, 1, [ids[0], ids[0]])
    question = db.question(ids[0])
    assert question["reps"] == 2 and question["last_attempt"] == NOW + 3 * DAY
    logs = db.attempts(ids[0])
    assert [r["source"] for r in logs] == ["practice", "practice"] and logs[0]["was_new"] == 1
    assert db.new_questions_attempted_since(NOW) == 1  # phone introductions count against the daily cap
    assert [tuple(r) for r in snapshot(root).execute("SELECT device, batch FROM acks")] == [("pixel", "0001")]
    # Read again (the phone hasn't deleted it yet): nothing happens twice.
    assert phone_sync.sync(db, root).applied == 0 and db.question(ids[0])["reps"] == 2
    # The same events in a second file are skipped by id.
    write_batch(root, [attempt(ids[0], NOW + 60, new=True)], batch="0002")
    assert phone_sync.import_batches(db, root).applied == 0
    # Once the phone deletes the files, the snapshot stops acknowledging them.
    path.unlink()
    (root / "phones" / "pixel" / "0002.json").unlink()
    phone_sync.sync(db, root)
    assert snapshot(root).execute("SELECT COUNT(*) FROM acks").fetchone()[0] == 0


def test_half_synced_batch_waits(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    path = write_batch(root, [attempt(ids[0], NOW)])
    path.write_text('{"format": 2, "events": [')
    result = phone_sync.sync(db, root, now=NOW)
    assert result.errors and result.applied == 0 and db.question(ids[0])["reps"] == 0
    write_batch(root, [attempt(ids[0], NOW)])
    assert phone_sync.sync(db, root, now=NOW).applied == 1


def test_older_phone_attempt_replays_the_log(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    practice.attempt(db, ids[0], 3, now=NOW)
    practice.attempt(db, ids[0], 3, now=NOW + 5 * DAY)  # here, after the phone's attempt below
    write_batch(root, [attempt(ids[0], NOW + 2 * DAY, rating=1)])
    phone_sync.sync(db, root, now=NOW + 5 * DAY)
    question = db.question(ids[0])
    assert question["reps"] == 3 and question["last_attempt"] == NOW + 5 * DAY
    assert [r["rating"] for r in sorted(db.attempts(ids[0]), key=lambda r: r["attempted_at"])] == [3, 1, 3]
    # The same as practicing in that order here.
    (tmp_path / "other").mkdir()
    db2, ids2, _ = make(tmp_path / "other", monkeypatch)
    for at, rating in ((NOW, 3), (NOW + 2 * DAY, 1), (NOW + 5 * DAY, 3)):
        practice.attempt(db2, ids2[0], rating, now=at)
    other = db2.question(ids2[0])
    assert (question["stage"], question["stability"], question["difficulty"]) == (other["stage"], other["stability"], other["difficulty"])


def test_suspend_flag_and_bury(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    practice.attempt(db, ids[1], 1, now=NOW)  # learning, due in minutes
    write_batch(root, [
        {"id": "s", "type": "suspend", "question": ids[0], "at": NOW, "suspended": 1},
        {"id": "f", "type": "flag", "question": ids[2], "at": NOW, "flag": 3},
        {"id": "b", "type": "bury", "question": ids[1], "at": NOW + 60},
        {"id": "n", "type": "bury", "question": ids[3], "at": NOW + 60},  # new: left alone
        {"id": "g", "type": "suspend", "question": 99999, "at": NOW},  # gone: ignored
    ])
    phone_sync.sync(db, root, now=NOW)
    assert db.question(ids[0])["suspended"] == 1 and db.question(ids[2])["flag"] == 3
    buried = db.question(ids[1])
    assert buried["due"] == practice.next_day(NOW + 60) and json.loads(buried["fsrs"])["due"].startswith("20")
    assert db.question(ids[3])["stage"] == 0


def test_rewards_merge(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    before = gamify.load(db, NOW)
    write_batch(root, [
        {"id": "x", "type": "xp", "at": NOW, "amount": 15},
        {"id": "a", "type": "achievement", "at": NOW, "key": "first"},
        {"id": "z", "type": "achievement", "at": NOW, "key": "no_such_thing"},
        {"id": "o", "type": "achievement", "at": NOW, "key": "reviews_100"},  # its old name
        {"id": "g", "type": "goal", "at": NOW, "day": "2027-01-15"},
        {"id": "g2", "type": "goal", "at": NOW, "day": "2027-01-15"},
        {"id": "f", "type": "freeze_spent", "at": NOW, "day": "2027-01-14"},
        {"id": "e", "type": "freeze_earned", "at": NOW, "day": "2027-01-15"},
    ])
    phone_sync.sync(db, root, now=NOW)
    state = gamify.load(db, NOW)
    assert state["xp"] == before["xp"] + 15 and "first" in state["achievements"]
    assert "no_such_thing" not in state["achievements"]
    assert "attempts_100" in state["achievements"] and "reviews_100" not in state["achievements"]
    assert (state["goals"], state["goal_day"]) == (before["goals"] + 1, "2027-01-15")
    assert state["frozen"] == ["2027-01-14"]
    assert state["freezes"] == before["freezes"] - 1 + 1 and state["freeze_earned"] == "2027-01-15"


def test_format_1_batches_from_older_phones(tmp_path, monkeypatch):
    """A phone not yet updated writes "card" for "question" and "review" for "attempt"."""
    db, ids, root = make(tmp_path, monkeypatch)
    write_batch(root, [
        {"id": "r", "type": "review", "card": ids[0], "at": NOW + 60, "rating": 3, "duration": 4000,
         "response": None, "verdict": None, "new": True},
        {"id": "s", "type": "suspend", "card": ids[1], "at": NOW, "suspended": 1},
    ], format=1)
    result = phone_sync.sync(db, root, now=NOW + 60)
    assert (result.applied, result.attempted, result.errors) == (2, [ids[0]], [])
    assert db.question(ids[0])["reps"] == 1 and db.attempts(ids[0])[0]["source"] == "practice"
    assert db.question(ids[1])["suspended"] == 1
    write_batch(root, [], batch="0002", format=3)  # from a newer app: left for later
    assert phone_sync.import_batches(db, root).errors


def test_a_batch_that_isnt_one_or_has_a_bad_time_doesnt_stop_the_sync(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    write_batch(root, []).write_text("[1, 2]")
    write_batch(root, [{**attempt(ids[0], NOW, eid="bad"), "at": "soon"}, attempt(ids[1], NOW)], batch="0002")
    result = phone_sync.sync(db, root, now=NOW)
    assert result.errors and db.question(ids[1])["reps"] == 1 and db.question(ids[0])["reps"] == 0


def test_phone_attempt_before_the_first_one_here_is_the_introduction(tmp_path, monkeypatch):
    """attempts.was_new marks the first attempt (and the first after a Forget), never two."""
    db, ids, root = make(tmp_path, monkeypatch)
    marks = lambda q: [(r["attempted_at"], r["was_new"]) for r in db.attempts(q)]
    # Practiced here first, but on the phone before that: the phone's attempt introduced it.
    practice.attempt(db, ids[0], 3, now=NOW + 2 * DAY)
    # The phone's copy was older than an attempt here: its "new" doesn't make a second introduction.
    practice.attempt(db, ids[1], 3, now=NOW)
    practice.attempt(db, ids[1], 3, now=NOW + 2 * DAY)
    # Forgotten here, then introduced on the phone before it was practiced here again.
    practice.attempt(db, ids[2], 3, now=NOW)
    practice.forget(db, [ids[2]], now=NOW + DAY)
    practice.attempt(db, ids[2], 3, now=NOW + 3 * DAY)
    write_batch(root, [attempt(ids[0], NOW, new=True), attempt(ids[1], NOW + DAY, new=True),
                       attempt(ids[2], NOW + 2 * DAY, new=True)])
    phone_sync.sync(db, root, now=NOW + 3 * DAY)
    assert marks(ids[0]) == [(NOW, 1), (NOW + 2 * DAY, 0)]
    assert marks(ids[1]) == [(NOW, 1), (NOW + DAY, 0), (NOW + 2 * DAY, 0)]
    assert marks(ids[2]) == [(NOW, 1), (NOW + 2 * DAY, 1), (NOW + 3 * DAY, 0)]
    # Replayed from the right introduction: the same as practicing in that order here.
    (tmp_path / "other").mkdir()
    db2, ids2, _ = make(tmp_path / "other", monkeypatch)
    for at in (NOW, NOW + 2 * DAY):
        practice.attempt(db2, ids2[0], 3, now=at)
    question, other = db.question(ids[0]), db2.question(ids2[0])
    assert (question["stage"], question["stability"], question["difficulty"], question["reps"]) == \
        (other["stage"], other["stability"], other["difficulty"], 2)


def test_a_batch_is_applied_whole_or_not_at_all(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    write_batch(root, [attempt(ids[0], NOW), {"id": "x", "type": "xp", "at": NOW + 1, "amount": 5},
                       attempt(ids[1], NOW + 2)])
    xp = gamify.load(db, NOW)["xp"]
    real = phone_sync.apply_event

    def failing(db, e):
        if e["id"] == f"r{ids[1]}-{NOW + 2}":
            raise sqlite3.OperationalError("disk I/O error")
        return real(db, e)

    monkeypatch.setattr(phone_sync, "apply_event", failing)
    try:
        phone_sync.import_batches(db, root)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("the error was swallowed")
    # Nothing of it stayed, so it is read again next time.
    assert db.question(ids[0])["reps"] == 0 and gamify.load(db, NOW)["xp"] == xp
    assert db.conn.execute("SELECT COUNT(*) FROM phone_events").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM phone_batches").fetchone()[0] == 0
    monkeypatch.setattr(phone_sync, "apply_event", real)
    assert phone_sync.import_batches(db, root).applied == 3
    assert db.question(ids[0])["reps"] == 1 and db.question(ids[1])["reps"] == 1


def test_an_event_that_fails_midway_leaves_nothing_behind(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    write_batch(root, [attempt(ids[0], NOW, eid="a"), attempt(ids[1], NOW, eid="b"), attempt(ids[1], NOW, eid="b")])
    real = phone_sync.apply_event

    def half(db, e):
        if e["id"] == "a":
            db.update_question(ids[0], flag=5)  # written (and, alone, committed) before it fails
            raise ValueError("no such rating")
        return real(db, e)

    monkeypatch.setattr(phone_sync, "apply_event", half)
    result = phone_sync.import_batches(db, root)
    assert result.errors and result.applied == 2  # the failed one is done with; the repeated one counts once
    assert db.question(ids[0])["flag"] == 0 and db.question(ids[1])["reps"] == 1
    assert [r[0] for r in db.conn.execute("SELECT id FROM phone_events ORDER BY id")] == ["a", "b"]


def test_transactions_hold_the_helpers_commits(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)
    try:
        with db.transaction():
            db.update_question(ids[0], flag=2)  # its own `with db.conn:` doesn't commit here
            with db.transaction():
                db.update_question(ids[1], flag=3)
            raise RuntimeError
    except RuntimeError:
        pass
    assert (db.question(ids[0])["flag"], db.question(ids[1])["flag"]) == (0, 0)
    with db.transaction():
        db.update_question(ids[0], flag=4)
        try:
            with db.conn:  # a savepoint inside one
                db.update_question(ids[1], flag=4)
                raise ValueError
        except ValueError:
            pass
    assert (db.question(ids[0])["flag"], db.question(ids[1])["flag"]) == (4, 0)
    assert not db.conn.in_transaction and db.conn.savepoints == 0
    db.update_question(ids[1], flag=1)  # outside one: committed as before
    assert sqlite3.connect(db.path).execute("SELECT flag FROM questions WHERE id=?", (ids[1],)).fetchone()[0] == 1


def test_a_snapshot_that_fails_leaves_no_temp_file(tmp_path, monkeypatch):
    db, ids, root = make(tmp_path, monkeypatch)

    def broken(snap, conn, plan):
        snap.execute("CREATE TABLE half (x)")
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(phone_sync, "_fill_snapshot", broken)
    try:
        phone_sync.sync(db, root, now=NOW)
    except sqlite3.OperationalError:
        pass
    assert list((root / "desktop").iterdir()) == []
    assert db.get_setting(phone_sync.FINGERPRINT_SETTING) == ""


def test_the_app_writes_the_snapshot_on_a_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.phone_sync_dialog import PhoneSync

    QApplication.instance() or QApplication([])
    db, ids, root = make(tmp_path, monkeypatch)
    db.set_setting(phone_sync.FOLDER_SETTING, str(root))
    threads = []
    real = phone_sync.write_snapshot_apart

    def apart(path, plan):
        import threading
        threads.append(threading.current_thread())
        return real(path, plan)

    monkeypatch.setattr(phone_sync, "write_snapshot_apart", apart)
    sync = PhoneSync(db)
    write_batch(root, [attempt(ids[0], NOW)])
    assert sync.sync().exported and sync.sync().applied == 0  # the second waits for the first
    sync.wait()
    assert threads and all(t is not __import__("threading").main_thread() for t in threads)
    assert sync.last_error == "" and db.get_setting(phone_sync.FINGERPRINT_SETTING)
    snap = snapshot(root)
    assert snap.execute("SELECT reps FROM questions WHERE id=?", (ids[0],)).fetchone()[0] == 1
    assert [tuple(r) for r in snap.execute("SELECT device, batch FROM acks")] == [("pixel", "0001")]
    # Nothing changed since: nothing is written.
    count = len(threads)
    assert not sync.sync().exported
    sync.wait()
    assert len(threads) == count
    # A failure on the thread is reported, and the next sync tries again.
    monkeypatch.setattr(phone_sync, "write_snapshot_apart", lambda path, plan: (_ for _ in ()).throw(OSError("full")))
    db.update_question(ids[1], suspended=1)
    sync.sync()
    sync.wait()
    assert sync.last_error == "full"
    monkeypatch.setattr(phone_sync, "write_snapshot_apart", apart)
    assert sync.sync().exported
    sync.wait()
    assert sync.last_error == "" and snapshot(root).execute(
        "SELECT suspended FROM questions WHERE id=?", (ids[1],)).fetchone()[0] == 1
