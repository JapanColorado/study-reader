import json

from study_reader import gamify, practice

from test_practice import NOW, setup

DAY = 86400


def questions(tmp_path, n=1):
    """A book with n self-check questions: (db, question ids)."""
    db, book_id, _, reply = setup(tmp_path)
    ids = []
    for i in range(n):
        q = {"type": "self_check", "question": f"Q{i}?", "answer": f"A{i}."}
        ids.append(db.add_question(book_id, reply, 1, i, "Questions", json.dumps(q), None, NOW - 30 * DAY))
    return db, ids


def test_levels_and_multiplier():
    assert gamify.level(0)["level"] == 1 and gamify.level(99)["level"] == 1
    lv = gamify.level(130)
    assert (lv["level"], lv["into"], lv["need"], lv["title"]) == (2, 30, 150, "Novice")
    assert gamify.level(gamify.level_xp(10))["title"] == "Student"
    assert [gamify.multiplier(c) for c in (0, 4, 5, 12, 200)] == [1.0, 1.0, 1.1, 1.2, gamify.MAX_MULTIPLIER]


def test_xp_goal_and_achievements(tmp_path):
    db, (question, *_) = questions(tmp_path)
    gamify.load(db, NOW)
    db.set_setting(gamify.GOAL_SETTING, "2")
    practice.attempt(db, question, 1, now=NOW)
    events = gamify.attempted(db, new=True, now=NOW)
    assert events[0] == {"kind": "xp", "xp": gamify.BASE_XP + gamify.NEW_XP}
    assert "first" in {e.get("id") for e in events} and "goal" not in {e["kind"] for e in events}
    # XP never depends on the rating; the combo multiplies it.
    practice.attempt(db, question, 1, now=NOW + 60)
    events = gamify.attempted(db, new=False, combo_multiplier=1.5, now=NOW + 60)
    assert events[0]["xp"] == 15 and [e for e in events if e["kind"] == "goal"]
    practice.attempt(db, question, 3, now=NOW + 120)
    assert not [e for e in gamify.attempted(db, new=False, now=NOW + 120) if e["kind"] == "goal"]  # once a day
    status = gamify.status(db, NOW + 180)
    assert status["xp"] == 40 and status["today"] == 3 and status["goal_met"] and status["streak"] == 1
    assert {a["id"] for a in status["achievements"] if a["unlocked"]} >= {"first", "goal_1"}


def test_streak_freezes(tmp_path):
    db, (question, *_) = questions(tmp_path)
    gamify.load(db, NOW)
    for day in (5, 4, 3):
        practice.attempt(db, question, 3, now=NOW - day * DAY)
    status = gamify.status(db, NOW - 2 * DAY)  # not practiced yet today
    assert status["streak"] == 3 and status["at_risk"] and status["freezes"] == gamify.START_FREEZES
    # The day after, the freeze covers the missed day: the streak survives.
    status = gamify.status(db, NOW - DAY)
    assert status["streak"] == 3 and status["freezes"] == 0 and status["heat"]["frozen"]
    practice.attempt(db, question, 3, now=NOW - DAY)
    assert gamify.status(db, NOW - DAY)["streak"] == 4
    # Two days missed with no freezes left: a fresh start.
    status = gamify.status(db, NOW + 2 * DAY)
    assert status["streak"] == 0 and status["lapsed"]


def test_a_week_of_streak_earns_a_freeze(tmp_path):
    db, (question, *_) = questions(tmp_path)
    gamify.load(db, NOW)
    earned = []
    for day in range(6, -1, -1):
        practice.attempt(db, question, 3, now=NOW - day * DAY)
        earned += [e for e in gamify.attempted(db, new=False, now=NOW - day * DAY) if e["kind"] == "freeze"]
    assert earned == [{"kind": "freeze", "freezes": gamify.START_FREEZES + 1}]


def test_state_is_made_from_the_attempt_log(tmp_path):
    db, (question, *_) = questions(tmp_path)
    for i in range(3):
        practice.attempt(db, question, 3, now=NOW - (2 - i) * DAY)
    state = gamify.load(db, NOW)
    assert state["xp"] == 3 * gamify.BASE_XP + gamify.NEW_XP
    assert {"first", "streak_3"} <= state["achievements"].keys()


def test_session_combo_and_summary(tmp_path):
    db, (question, *_) = questions(tmp_path)
    s = gamify.Session(started=0)
    before = db.question(question)
    practice.attempt(db, question, 3, now=NOW)
    after = db.question(question)
    events = []
    for i in range(5):
        events += s.record(before, after, 3, seconds=5)
    assert s.combo == 5 and s.multiplier == 1.1 and events == [{"kind": "combo", "combo": 5, "multiplier": 1.1}]
    s.record(before, after, 1, seconds=gamify.COMBO_PAUSE + 1)  # a long pause starts the combo over
    assert (s.combo, s.best, s.attempted, s.passed) == (1, 5, 6, 5) and s.stability > 0
    quick = gamify.Session("questions", started=0)
    assert not quick.over(1)
    quick.attempted = gamify.QUICK_QUESTIONS
    assert quick.over(1) and quick.summary(False, 61)["quick"]
    assert gamify.Session("time", started=0).over(gamify.QUICK_SECONDS)


def test_quick_practice_session(chat_panel, tmp_path):
    from study_reader.practice_view import PracticePanel

    _, worker, _, _ = chat_panel
    (tmp_path / "q").mkdir()
    db, ids = questions(tmp_path / "q", gamify.QUICK_QUESTIONS + 2)
    panel = PracticePanel(db)
    shown = []
    panel._js = lambda fn, *args: shown.append((fn, args))
    panel.start("questions")
    assert shown[-1][1][0]["session"]["total"] == gamify.QUICK_QUESTIONS
    for _ in range(gamify.QUICK_QUESTIONS):
        panel.rate(panel.question_id, 3)
    assert panel.question_id is None
    overview = [a[0] for fn, a in shown if fn == "showOverview"][-1]
    assert overview["summary"]["quick"] and overview["summary"]["attempted"] == gamify.QUICK_QUESTIONS
    # The fifth question in a row raises the multiplier.
    xp = sum(round((gamify.BASE_XP + gamify.NEW_XP) * gamify.multiplier(i)) for i in range(1, gamify.QUICK_QUESTIONS + 1))
    assert overview["summary"]["xp"] == xp and overview["game"]["xp"] == xp
    assert overview["game"]["today"] == gamify.QUICK_QUESTIONS
    kinds = [e["kind"] for fn, a in shown if fn == "celebrate" for e in a[0]]
    assert kinds.count("xp") == gamify.QUICK_QUESTIONS and "done" in kinds
    assert "quick" in gamify.load(db)["achievements"]

    # With rewards off, nothing is celebrated or shown.
    db.set_setting(gamify.ENABLED_SETTING, "0")
    shown.clear()
    panel.start()
    panel.rate(panel.question_id, 3)
    assert not [fn for fn, _ in shown if fn == "celebrate"] and shown[-1][1][0]["rewards"] is False
    panel.stop()
    assert shown[-1][1][0]["game"] is None and shown[-1][1][0]["summary"]["attempted"] == 1


def test_old_achievement_ids_are_read_as_the_new_ones(tmp_path):
    db, _ = questions(tmp_path)
    db.set_setting(gamify.STATE_SETTING, json.dumps({"xp": 5000, "achievements": {
        "first": NOW - 9, "reviews_100": NOW - 5, "attempts_100": NOW - 3, "reviews_1000": NOW - 1}}))
    unlocked = gamify.load(db, NOW)["achievements"]
    assert unlocked == {"first": NOW - 9, "attempts_100": NOW - 5, "attempts_1000": NOW - 1}
    assert gamify.achievement_key("reviews_10000") == "attempts_10000" and gamify.achievement_key("first") == "first"
    status = {a["id"]: a["unlocked"] for a in gamify.status(db, NOW)["achievements"]}
    assert status["attempts_100"] and status["attempts_1000"] and not status["attempts_10000"]
