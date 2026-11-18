import json

from PySide6.QtCore import Qt

from study_reader import quiz, practice
from study_reader.db import Database

from test_quiz import QUIZ

SELF_CHECKS = r'''```quiz Zorblatt facts
{"questions": [
  {"type": "self_check", "question": "How long is a zorblatt rinsed?", "answer": "41 seconds, in cold tea.", "page": 2},
  {"type": "card", "question": "Where must a zorblatt never be stored?", "answer": ["near a window"]}
]}
```'''
NOW = 1_800_000_000.0  # a fixed moment, so "today" is the same in every run


def setup(tmp_path, text=QUIZ):
    """A book with a chat whose reply holds a quiz: (db, book id, chat id, reply id)."""
    db = Database(tmp_path / "t.db")
    book_id = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    chat = db.create_chat(book_id, "Quiz")
    db.add_message(chat, "user", "Quiz me on this passage.",
                   [{"kind": "text", "page": 2, "text": "Never store a zorblatt", "rects": []}])
    reply = db.add_message(chat, "assistant", text)
    return db, book_id, chat, reply


# -- self-check questions in quizzes -------------------------------------------------------------
def test_self_checks_parse_and_grade_by_rating():
    (q,) = quiz.quizzes(SELF_CHECKS)
    front, back = q["questions"]
    assert front["type"] == back["type"] == "self_check" and back["answer"] == "- near a window"
    assert [quiz.grade(front, {"rating": r}) for r in (1, 2, 3, 4)] == ["incorrect", "partial", "correct", "correct"]
    assert quiz.describe_response(front, {"rating": 2}) == "rated my recall Shaky"
    _, (bad,) = quiz.split('```quiz\n[{"type": "flashcard", "question": "Front only"}]\n```')
    assert "needs an answer" in bad["error"]


# -- questions -----------------------------------------------------------------------------
def test_every_quiz_question_goes_into_practice_once(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    assert len(ids) == 4 and practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW) == []
    rows = db.quiz_questions(reply, 0)
    assert [c["position"] for c in rows] == [0, 1, 2, 3] and rows[0]["title"] == "Zorblatts"
    # The question's own page, else the page of the passage the quiz was asked about.
    assert [c["page"] for c in rows] == [0, 2, 2, 2]
    assert practice.question(rows[2])["type"] == "calculation" and all(practice.is_new(c) for c in rows)


def test_first_answer_in_the_chat_is_the_first_attempt(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    assert practice.answered_in_chat(db, reply, 0, 0, "correct", {"choice": 1}, now=NOW)
    row = db.question_for(reply, 0, 0)
    assert row["reps"] == 1 and row["stage"] > 0 and row["due"] > NOW
    (log,) = db.attempts(row["id"])
    assert (log["rating"], log["source"], log["verdict"]) == (3, "chat", "correct")
    # A retake doesn't touch the schedule; ungraded answers don't count.
    assert not practice.answered_in_chat(db, reply, 0, 0, "incorrect", {"choice": 0}, now=NOW + 60)
    assert not practice.answered_in_chat(db, reply, 0, 3, None, {"text": "?"}, now=NOW)
    assert db.question_for(reply, 0, 3)["reps"] == 0


def test_ratings_space_attempts_out(tmp_path):
    db, book_id, chat, reply = setup(tmp_path, SELF_CHECKS)
    question_id = practice.add_quiz_questions(db, book_id, reply, SELF_CHECKS, now=NOW)[0]
    t = NOW
    for _ in range(4):
        practice.attempt(db, question_id, 3, now=t)
        t = db.question(question_id)["due"]
    row = db.question(question_id)
    assert row["stage"] == 2 and row["due"] - NOW > 3 * 86400 and row["reps"] == 4
    practice.attempt(db, question_id, 1, now=row["due"])
    assert db.question(question_id)["lapses"] == 1 and db.question(question_id)["stage"] == 3
    previews = practice.previews(db.question(question_id), now=NOW)
    assert set(previews) == {1, 2, 3, 4} and all(previews.values())


def test_previewed_interval_is_the_one_scheduled(tmp_path, monkeypatch):
    import random

    monkeypatch.setattr(practice, "format_interval", lambda seconds: seconds)  # previews in seconds
    db, book_id, chat, reply = setup(tmp_path, SELF_CHECKS)
    ids = practice.add_quiz_questions(db, book_id, reply, SELF_CHECKS, now=NOW)
    for question_id in ids:
        t = NOW
        for _ in range(6):  # learned, with intervals long enough to be fuzzed
            row = db.question(question_id)
            random.seed(7)
            previews = practice.previews(row, now=t)
            assert random.random() == random.Random(7).random()  # `random` is left as it was
            practice.attempt(db, question_id, 3, now=t)
            after = db.question(question_id)
            assert after["due"] - t == previews[3]
            t = after["due"]
        assert after["stage"] == 2 and after["due"] - after["last_attempt"] > 30 * 86400


def test_queue_order_new_question_limit_and_suspension(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    a, b, c, d = practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    here = [{"book": book_id, "section": None}]
    assert practice.counts(db, NOW, picks=here) == {"due": 0, "new": 4, "new_total": 4, "total": 4, "suspended": 0}
    assert practice.next_question(db, NOW, picks=here)["id"] == a  # new questions in the order they were written

    db.set_setting(practice.NEW_PER_DAY_SETTING, "1")
    practice.attempt(db, a, 1, now=NOW)  # introduced in practice: uses up today's one new question
    assert practice.counts(db, NOW, picks=here)["new"] == 0
    # `a` is back in a minute; until then, with no new questions left today, it is shown early...
    assert practice.next_question(db, NOW, picks=here)["id"] == a
    # ...and once due it comes before anything else.
    assert practice.counts(db, NOW + 120, picks=here)["due"] == 1 and practice.next_question(db, NOW + 120, picks=here)["id"] == a
    # A chat answer isn't counted against the new-question allowance.
    practice.answered_in_chat(db, reply, 0, 1, "correct", {"value": False}, now=NOW)
    assert db.new_questions_attempted_since(practice.day_start(NOW)) == 1

    db.update_question(a, suspended=1)
    db.set_quiz_suspended(reply, 0, True)
    assert practice.next_question(db, NOW + 3600, picks=here) is None and practice.counts(db, NOW, picks=here)["suspended"] == 4
    db.set_quiz_suspended(reply, 0, False)
    db.set_setting(practice.NEW_PER_DAY_SETTING, "20")
    # Skipping the question just shown, unless it's the only one.
    first = practice.next_question(db, NOW + 3600, picks=here)
    second = practice.next_question(db, NOW + 3600, skip=[first["id"]], picks=here)
    third = practice.next_question(db, NOW + 3600, skip=[first["id"], second["id"]], picks=here)
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert practice.counts(db, NOW)["total"] == 4  # all books


def test_practice_days_roll_over_at_4am():
    from datetime import datetime
    late = datetime(2026, 3, 10, 2, 30).timestamp()
    assert datetime.fromtimestamp(practice.day_start(late)) == datetime(2026, 3, 9, 4)
    assert datetime.fromtimestamp(practice.next_day(late)) == datetime(2026, 3, 10, 4)
    assert practice.format_interval(30) == "1m" and practice.format_interval(3 * 86400) == "3d"


def test_deleting_messages_keeps_only_attempted_questions(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    practice.answered_in_chat(db, reply, 0, 0, "correct", {"choice": 1}, now=NOW)
    kept = db.question_for(reply, 0, 0)["id"]
    db.delete_messages_from(chat, reply)  # e.g. regenerating the reply
    (row,) = db.questions(book_id)
    assert row["id"] == kept and row["message_id"] is None and len(db.attempts(kept)) == 1

    reply2 = db.add_message(chat, "assistant", SELF_CHECKS)
    practice.add_quiz_questions(db, book_id, reply2, SELF_CHECKS, now=NOW)
    db.delete_chat(chat)
    assert [c["id"] for c in db.questions(book_id)] == [kept]
    reply3 = db.add_message(db.create_chat(book_id, "Again"), "assistant", SELF_CHECKS)
    practice.add_quiz_questions(db, book_id, reply3, SELF_CHECKS, now=NOW)
    db.delete_book(book_id)  # attempted questions stay in their deck; the others go with the chats
    (row,) = db.questions()
    assert row["id"] == kept and row["book_id"] is None


def test_backfill_puts_earlier_quizzes_into_practice(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    db.save_quiz_answer(reply, 0, 0, '{"choice": 0}', "done", "incorrect")
    db.save_quiz_answer(reply, 0, 3, '{"text": "Tea"}', "pending")
    assert practice.backfill(db) == 1 and practice.backfill(db) == 0
    rows = db.quiz_questions(reply, 0)
    assert len(rows) == 4 and [c["reps"] for c in rows] == [1, 0, 0, 0]
    assert db.attempts(rows[0]["id"])[0]["rating"] == 1


# -- sections ------------------------------------------------------------------------------
TOC = [[1, "Part I", 0], [2, "Chapter 1: Rinsing, mostly", 0], [2, "Chapter 2", 2], [3, "2.1 Storage", 2], [1, "Index", 9]]


def test_section_tags_from_the_contents():
    assert practice.section_path(TOC, 1) == ["Part I", "Chapter 1: Rinsing, mostly"]
    assert practice.section_path(TOC, 2) == ["Part I", "Chapter 2", "2.1 Storage"]
    assert practice.section_path(TOC, None) == [] and practice.section_path([[1, "Late", 3]], 1) == []
    assert practice.section_tag("A Book, Vol. 1", TOC, 0) == "A_Book_Vol._1::Part_I::Chapter_1:_Rinsing_mostly"
    assert practice.section_tag("A", [], 0) is None
    assert practice.section_tag("A", TOC, 2, 3) == "A::Part_I::Chapter_2::2.1_Storage"
    assert practice.section_tag("A", TOC, 0, 3) == "A::Part_I" and practice.section_tag("A", TOC, 0, 9) is None
    assert practice.section_tag("A", [[1, "Chapter 6 Firms \x00\x00", 0]], 0) == "A::Chapter_6_Firms"


def test_migration_cleans_nul_padded_section_tags(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ)
    group = db.question_group(db.questions(book_id)[0]["group_id"])
    with db.conn:
        db.conn.execute("UPDATE question_groups SET tags=? WHERE id=?", (" A::Chapter_6_\x00\x00 a ", group["id"]))
    db.conn.close()
    db = Database(tmp_path / "t.db")
    assert db.question_group(group["id"])["tags"] == " A::Chapter_6 a "
    assert db.count_questions(tag="A::Chapter_6") == 1


def test_quiz_questions_are_tagged_with_their_section(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ)
    assert all(db.question_group(c["group_id"])["tags"] == "" for c in db.quiz_questions(reply, 0))
    # The first time the book's contents are known, the questions made before get tagged.
    assert practice.learn_toc(db, book_id, TOC) == 4 and practice.learn_toc(db, book_id, TOC) == 0
    tags = [db.question_group(c["group_id"])["tags"] for c in db.quiz_questions(reply, 0)]
    assert tags[0] == " A::Part_I::Chapter_1:_Rinsing_mostly " and tags[1] == " A::Part_I::Chapter_2::2.1_Storage "

    later = db.add_message(chat, "assistant", SELF_CHECKS)
    practice.add_quiz_questions(db, book_id, later, SELF_CHECKS)
    assert db.question_group(db.question_for(later, 0, 0)["group_id"])["tags"] == " A::Part_I::Chapter_1:_Rinsing_mostly "
    assert db.question_group(db.question_for(later, 0, 1)["group_id"])["tags"] == " A::Part_I::Chapter_2::2.1_Storage "

    # Practicing one section (or the part holding it).
    def section(tag):
        return [{"book": book_id, "section": tag}]

    assert practice.counts(db, NOW, picks=section("A::Part_I::Chapter_2"))["total"] == 4
    assert practice.counts(db, NOW, picks=section("A::Part_I"))["total"] == 6
    assert practice.counts(db, NOW, picks=section("A::Part_I::Chapter_"))["total"] == 0  # whole parts only
    row = practice.next_question(db, NOW, picks=section("A::Part_I::Chapter_1:_Rinsing_mostly"))
    assert row["id"] == db.question_for(reply, 0, 0)["id"]

    tree = practice.sections(db, NOW)[book_id]
    assert [(n["tag"], n["parent"], n["total"]) for n in tree] == [
        ("A::Part_I", None, 6), ("A::Part_I::Chapter_1:_Rinsing_mostly", "A::Part_I", 2),
        ("A::Part_I::Chapter_2", "A::Part_I", 4), ("A::Part_I::Chapter_2::2.1_Storage", "A::Part_I::Chapter_2", 4),
    ]
    assert tree[2]["name"] == "Chapter 2" and tree[0]["new"] == 6


# Like many textbooks' contents, Summary and Exercises sit inside the chapter's last section.
BOOK_TOC = [[1, "Chapter 6 Firms", 0], [2, "6.1 Ownership", 0], [2, "6.4 Long-Run Production", 1],
            [3, "Isoquants", 1], [2, "6.41 Odd", 2], [2, "6.6 Productivity", 3], [3, "Innovations", 3],
            [3, "Summary", 4], [3, "Exercises", 5], [1, "Chapter 7 Costs", 6], [2, "6.4 Not this one", 6]]


def test_chapter_back_matter_and_named_sections():
    assert practice.section_path(BOOK_TOC, 3) == ["Chapter 6 Firms", "6.6 Productivity", "Innovations"]
    assert practice.section_path(BOOK_TOC, 5) == ["Chapter 6 Firms", "Exercises"]
    assert practice.section_tag("A", BOOK_TOC, 4) == "A::Chapter_6_Firms::Summary"
    # Not when every section has its own.
    own = [[1, "Ch 1", 0], [2, "1.1 A", 0], [3, "Exercises", 1], [2, "1.2 B", 2], [3, "Exercises", 3]]
    assert practice.section_path(own, 3) == ["Ch 1", "1.2 B", "Exercises"]
    # A section named by number or title; of several, the last starting by the page.
    assert practice.section_tag("A", BOOK_TOC, 5, 5, "6.4") == "A::Chapter_6_Firms::6.4_Long-Run_Production"
    assert practice.section_tag("A", BOOK_TOC, 5, None, "long-run production") == "A::Chapter_6_Firms::6.4_Long-Run_Production"
    assert practice.section_tag("A", BOOK_TOC, 6, None, "6.4") == "A::Chapter_7_Costs::6.4_Not_this_one"
    assert practice.find_section(BOOK_TOC, "6.41") == 4 and practice.find_section(BOOK_TOC, "Chapter 6") == 0
    assert practice.section_tag("A", BOOK_TOC, 5, None, "9.9") == "A::Chapter_6_Firms::Exercises"


def test_quiz_questions_name_the_section_they_practice(tmp_path):
    db, book_id, chat, _ = setup(tmp_path)
    practice.learn_toc(db, book_id, BOOK_TOC)
    text = """```quiz Exercises
{"questions": [
  {"type": "self_check", "question": "MRTS?", "answer": "Slope of the isoquant.", "page": 6, "section": "6.4"},
  {"type": "self_check", "question": "Summary?", "answer": "Firms.", "page": 6}
]}
```"""
    reply = db.add_message(chat, "assistant", text)
    practice.add_quiz_questions(db, book_id, reply, text, now=NOW)
    first, second = db.quiz_questions(reply, 0)
    assert db.question_group(first["group_id"])["tags"] == " A::Chapter_6_Firms::6.4_Long-Run_Production "
    assert db.question_group(second["group_id"])["tags"] == " A::Chapter_6_Firms::Exercises "
    assert first["page"] == 5 and "section" not in practice.question(first)
    # Sections come in the book's order, not that of their questions' pages.
    tree = practice.sections(db, NOW)[book_id]
    assert [n["name"] for n in tree] == ["Chapter 6 Firms", "6.4 Long-Run Production", "Exercises"]


# -- the chat panel ------------------------------------------------------------------------
def test_chat_quizzes_feed_the_deck(chat_panel):
    panel, worker, db, book_id = chat_panel
    changes = []
    panel.questionsChanged.connect(lambda: changes.append(1))
    panel.start_quiz("Make flashcards on this passage.", "Self-check: x", 0)
    panel.send()
    chat = panel.chat_id
    worker.done.emit(chat, "s1", QUIZ + "\n\n" + SELF_CHECKS, "u1")
    reply = db.messages(chat)[1]["id"]
    assert len(db.questions(book_id)) == 6 and changes

    panel._on_quiz_answer(reply, 1, 0, json.dumps({"rating": 4}))
    row = db.question_for(reply, 1, 0)
    assert row["reps"] == 1 and db.attempts(row["id"])[0]["rating"] == 4
    assert db.quiz_answers(chat)[reply][0]["verdict"] == "correct"

    # A short answer counts once Claude has graded it.
    panel._on_quiz_answer(reply, 0, 3, json.dumps({"text": "Because tea."}))
    _, answer_id = worker.gradings[-1]
    assert db.question_for(reply, 0, 3)["reps"] == 0
    worker.graded.emit(answer_id, '{"verdict": "partial", "feedback": "Why tea?"}')
    assert db.attempts(db.question_for(reply, 0, 3)["id"])[0]["rating"] == 2

    view = panel._quiz_view(quiz.quizzes(db.message(reply)["text"])[0], 0, [], reply)
    assert view["practice"] == "on"
    panel._on_quiz_practice_toggled(reply, 0)
    assert all(q["suspended"] for q in db.quiz_questions(reply, 0)) and not db.quiz_questions(reply, 1)[0]["suspended"]
    assert panel._quiz_view(quiz.quizzes(db.message(reply)["text"])[0], 0, [], reply)["practice"] == "off"

    # A quiz on a reply is asked for in the same chat.
    db.add_message(chat, "assistant", QUIZ)  # a later reply
    panel.quiz_on_reply(reply)
    panel.send()
    assert worker.jobs[-1].message.startswith("Quiz me on your earlier reply that begins “Here's a quiz.")
    worker.done.emit(chat, "s2", "Ok", "u2")
    panel.quiz_on_reply(db.messages(chat)[-1]["id"])
    panel.send()
    assert worker.jobs[-1].message == "Quiz me on your last reply"


# -- the Practice page ---------------------------------------------------------------------
def test_practicing_questions(chat_panel):
    from study_reader.practice_view import PracticePanel

    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me.", "Quiz", 0)
    panel.send()
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    page = PracticePanel(db)
    page.choose_book(book_id)
    shown = []
    page._js = lambda fn, *args: shown.append((fn, args))
    attempted = []
    page.countsChanged.connect(lambda: attempted.append(1))

    page.start()
    first = page.question_id
    assert first == db.question_for(reply, 0, 0)["id"] and shown[-1][0] == "showQuestion"
    assert shown[-1][1][0]["previews"] is None and shown[-1][1][0]["new"]
    page.check(first, {"choice": 1})
    payload = shown[-1][1][0]
    assert payload["answer"]["verdict"] == "correct" and payload["suggested"] == 3 and set(payload["previews"]) == {1, 2, 3, 4}
    page.rate(first, 3)
    assert db.question(first)["reps"] == 1 and db.attempts(first)[0]["source"] == "practice" and attempted
    assert page.question_id != first

    # A calculation's answer it can't read leaves the question unanswered.
    while practice.question(db.question(page.question_id))["type"] != "calculation":
        page.act(page.question_id, "skip")
    page.check(page.question_id, {"latex": r"\frac{1}{"})
    assert shown[-1][0] == "inputError" and page._answer is None

    # Short answers are rated by the learner: showing the answer keeps what was typed, ungraded.
    while practice.question(db.question(page.question_id))["type"] != "short_answer":
        page.act(page.question_id, "skip")
    short = page.question_id
    gradings = len(worker.gradings)
    page.check(short, {"text": "Because tea."})
    payload = shown[-1][1][0]
    assert payload["answer"]["verdict"] is None and payload["suggested"] is None and payload["previews"]
    assert len(worker.gradings) == gradings
    page.rate(short, 1)
    assert db.attempts(short)[0]["verdict"] is None and json.loads(db.attempts(short)[0]["response"]) == {"text": "Because tea."}

    page.act(page.question_id, "suspend")
    assert db.count_questions(book_id, "suspended=1") == 1
    page.stop()
    assert shown[-1][0] == "showOverview" and shown[-1][1][0]["attempted"] == 2


def test_skipped_questions_stay_skipped_for_the_session(chat_panel):
    from study_reader.practice_view import PracticePanel

    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me.", "Quiz", 0)
    panel.send()
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    page = PracticePanel(db)
    page.choose_book(book_id)
    shown = []
    page._js = lambda fn, *args: shown.append((fn, args))

    page.start()
    skipped = page.question_id
    assert shown[-1][1][0]["session"]["total"] == 4
    page.act(skipped, "skip")
    # The skipped question is still counted as due/new, but not in the session's progress.
    assert shown[-1][1][0]["remaining"]["new"] == 4 and shown[-1][1][0]["session"]["total"] == 3
    seen = []
    while page.question_id is not None:  # rating the rest never brings the skipped question back
        assert page.question_id != skipped
        seen.append(page.question_id)
        page.rate(page.question_id, 4)
    assert skipped not in seen and len(set(seen)) == 3
    # Over, but not caught up: the skipped question is still to practice.
    overview = [args[0] for fn, args in shown if fn == "showOverview"][-1]
    assert overview["summary"]["caught_up"] is False
    assert {"kind": "done", "caught_up": False, "quick": False} in [e for fn, args in shown if fn == "celebrate" for e in args[0]]
    page.start()  # a new session shows it again
    assert page.question_id == skipped

    # Skipping everything ends the session.
    for _ in range(4):
        page.act(page.question_id, "skip")
    assert page.question_id is None and shown[-1][0] == "showOverview"


def run_js(page, code: str):
    """Run code in a loaded QWebEnginePage and wait for its value."""
    import time
    from PySide6.QtWidgets import QApplication
    out = []
    page.runJavaScript(code, 0, out.append)
    end = time.monotonic() + 10
    while not out and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert out, f"no answer from {code!r}"
    return out[0]


def wait_ready(view, page, ready: str = "true") -> None:
    """Wait for a page (a PracticePanel's, a QuestionView) to have loaded and the JS ready to be true in it."""
    import time
    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + 10
    while not (view._ready and run_js(page, ready)) and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert view._ready


def test_practice_page_empty_state_and_twisting_rows(chat_panel):
    """The desktop's Make a deck with Claude link shows in the empty state too (never without
    o.make, as on the phone); expanding a picker row is drawn in the page, without a round trip."""
    from study_reader.practice_view import PracticePanel

    _, _, db, _ = chat_panel
    panel = PracticePanel(db)
    page = panel.view.page()
    wait_ready(panel, page, "bridge !== null")
    root = 'document.getElementById("root").innerHTML'
    assert 'data-act="make"' in run_js(page, f'showOverview({{"total": 0, "make": true}}); {root}')
    assert 'data-act="make"' not in run_js(page, f'showOverview({{"total": 0}}); {root}')

    refreshed = []
    panel.refresh = lambda: refreshed.append(1)
    overview = {"total": 1, "due": 1, "new": 0, "new_total": 0, "picks": [], "all_due": 1, "all_new": 0, "books": [],
                "quick": {"questions": 5, "minutes": 5},
                "decks": [{"id": 1, "parent": None, "name": "Physics", "path": "Physics", "due": 1, "new": 0},
                          {"id": 2, "parent": 1, "name": "Optics", "path": "Physics::Optics", "due": 1, "new": 0}]}
    assert "Optics" not in run_js(page, f"showOverview({json.dumps(overview)}); {root}")
    run_js(page, 'document.querySelector("[data-act=twist][data-key=d1]").click()')
    assert "Optics" in run_js(page, root) and not refreshed


def test_practicing_one_book_or_section(chat_panel):
    from study_reader.practice_view import PracticePanel

    panel, worker, db, book_id = chat_panel
    practice.learn_toc(db, book_id, TOC)
    panel.start_quiz("Quiz me on pages 1-4.", "Quiz", 0, last=3)  # spans chapters 1 and 2
    panel.send()
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    # A question with no page of its own goes into the section holding all the quizzed pages.
    tags = [db.question_group(c["group_id"])["tags"] for c in db.quiz_questions(reply, 0)]
    assert tags[0] == " A::Part_I::Chapter_1:_Rinsing_mostly " and set(tags[1:]) == {" A::Part_I "}
    assert db.quiz_questions(reply, 0)[1]["page"] == 0
    other = db.upsert_book("h2", "/b.pdf", "B", 3)["id"]
    db.add_question_group("self_check", {"front": "q", "back": "a"}, db.book_deck(other), book_id=other)

    page = PracticePanel(db)
    shown = []
    page._js = lambda fn, *args: shown.append((fn, args))
    page.refresh()
    o = shown[-1][1][0]
    assert [b["title"] for b in o["books"]] == ["A", "B"] and o["total"] == 5
    assert o["decks"] == []  # books' own decks are listed as books
    assert [s["name"] for s in o["books"][0]["sections"]] == ["Part I", "Chapter 1: Rinsing mostly"]
    page.choose_book(other)
    assert shown[-1][1][0]["total"] == 1 and shown[-1][1][0]["title"] == "B"
    page.choose_book(book_id, "A::Part_I")
    o = shown[-1][1][0]
    assert o["total"] == 4 and o["picks"] == [{"book": book_id, "section": "A::Part_I"}] and o["title"] == "A › Part I"
    page.choose_book(book_id, "A::Part_I::Chapter_1:_Rinsing_mostly")
    o = shown[-1][1][0]
    assert o["total"] == 1 and o["title"] == "A › Part I › Chapter 1: Rinsing mostly"
    page.start()
    assert page.question_id == db.question_for(reply, 0, 0)["id"]
    page.choose_book(book_id)
    assert page.picks == [{"book": book_id, "section": None}] and shown[-1][1][0]["total"] == 4
    page.choose_deck(None)
    assert shown[-1][1][0]["total"] == 5 and page.picks == []

    # Several at once (Ctrl+click): the questions in any of them, each counted once.
    page.choose([{"book": other}, {"book": book_id, "section": "A::Part_I::Chapter_1:_Rinsing_mostly"},
                   {"book": other, "section": None}])
    o = shown[-1][1][0]
    assert o["picks"] == [{"book": other, "section": None},
                          {"book": book_id, "section": "A::Part_I::Chapter_1:_Rinsing_mostly"}]
    assert o["total"] == 2 and o["title"] == "B and A › Part I › Chapter 1: Rinsing mostly"
    page.choose([{"book": book_id, "section": "A::Part_I"}, {"book": book_id, "section": None}])
    assert shown[-1][1][0]["total"] == 4  # a section and its book overlap
    deck = db.ensure_deck("Loose")
    db.add_question_group("self_check", {"front": "x", "back": "y"}, deck)
    page.choose([{"deck": deck}, {"book": other, "section": None}])
    o = shown[-1][1][0]
    assert o["total"] == 2 and o["title"] == "Loose and B" and page.picked_deck() == deck
    page.start()
    seen = set()
    while page.question_id is not None:
        seen.add(db.question(page.question_id)["deck_id"])
        page.rate(page.question_id, 3)
    assert seen == {deck, db.book(other)["deck_id"]}
    # A section whose questions are gone falls back to its book; a book that is gone is dropped.
    page.choose([{"book": book_id, "section": "A::Nowhere"}, {"book": 999, "section": None}])
    assert shown[-1][1][0]["picks"] == [{"book": book_id, "section": None}]


def test_main_window_practice_of_the_open_book(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.activateWindow()
    app.processEvents()
    win.open_book(pdf_path)
    chat = db.create_chat(win.book_id, "Quiz")
    reply = db.add_message(chat, "assistant", SELF_CHECKS)
    practice.add_quiz_questions(db, win.book_id, reply, SELF_CHECKS)
    win._on_questions_changed()
    assert win._counts_timer.isActive()  # counted once the burst of changes is over
    QTest.qWait(win._counts_timer.interval() + 50)
    assert "2 questions waiting" in win.practice_action.toolTip()

    assert win.practice_action.text() == "Practice · 2" and win.nav.buttons["practice"].badge == "2"
    other = db.upsert_book("h2", "/b.pdf", "B", 3)["id"]
    db.add_question_group("self_check", {"front": "q", "back": "a"}, db.book_deck(other), book_id=other)
    book_id = win.book_id

    # F8 (the toolbar's Practice): the Practice page narrowed to the open book, which stays open behind it.
    QTest.keyClick(win, Qt.Key.Key_F8)
    assert win.page() == "practice" and win.book_id == book_id
    assert win.practice.picks == [{"book": book_id, "section": None}]
    assert db.question(win.practice.question_id)["book_id"] == book_id
    assert not win.toolbar.isVisible() and not win.right.isVisible() and win.nav.book.isVisible()
    assert not win.practice_action.isEnabled()  # only while reading
    win.practice.act(win.practice.question_id, "source")  # a question's page: back in the book
    assert win.page() == "book" and win.reader.current_page() == db.question(win.practice.question_id)["page"]
    assert db.book(book_id)["right_panel"] == "chat"  # the right-hand panel is left as it was

    win.show_page("practice")
    win._practice_chat(chat)  # a question's Open chat: in the chat panel beside the questions
    assert win.page() == "practice" and win.practice_chat.isVisible() and win.practice_chat.chat_id == chat
    assert win.practice_chat.book_id == book_id and win.chat.chat_id is None

    win.practice.choose_deck(None)
    win.library.practiceBookRequested.emit(book_id)  # the Library's Practice button does the same
    assert win.page() == "practice" and win.practice.picks == [{"book": book_id, "section": None}]
    assert win.practice.question_id is not None
    win.close()


def test_navigating_pages_keeps_the_book_open(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    assert win.page() == "home" and not win.toolbar.isVisible() and not win.nav.book.isVisible()
    win.show_page("book")  # nothing open: Home
    assert win.page() == "home"

    win.open_book(pdf_path)
    book_id = win.book_id
    assert win.page() == "book" and win.toolbar.isVisible() and win.nav.book.isVisible()
    assert win.nav.buttons["book"].isChecked() and win.right.isVisible()
    win.reader.go_to(2)
    win.show_page("library")
    assert db.book(book_id)["last_page"] == 2  # saved on the way out
    assert win.book_id == book_id and not win.right.isVisible() and not win.quiz_action.isEnabled()
    rows = [win.library.list.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(win.library.list.count())]
    assert rows[0]["id"] == book_id and rows[0]["questions"] == 0 and not rows[0]["due"]
    win.show_page("home")
    assert win.home.data["reading"][0]["id"] == book_id
    QApplication.processEvents()  # covers not drawn yet come a turn of the event loop later
    assert win.home.data["reading"][0]["cover"].startswith("file:")
    win.show_page("book")
    assert win.reader.current_page() == 2 and win.right.isVisible() and win.quiz_action.isEnabled()

    win.close_book()
    assert win.book_id is None and win.page() == "home" and not win.nav.book.isVisible()
    win.home.openRequested.emit(book_id)
    assert win.book_id == book_id and win.page() == "book"
    win.close()
    win.close()


def test_calculator_beside_practice(pdf_path, tmp_path, monkeypatch):
    """In a book the calculator takes the right-hand slot; on the Practice page F11 opens one beside
    the questions, showing the history of the question's book (or, without one, a shared history)."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.activateWindow()
    win.open_book(pdf_path)
    book_id = win.book_id
    chat = db.create_chat(book_id, "Quiz")
    reply = db.add_message(chat, "assistant", SELF_CHECKS)
    practice.add_quiz_questions(db, book_id, reply, SELF_CHECKS)
    db.add_question_group("self_check", {"front": "Loose", "back": "question"}, db.ensure_deck("Loose"))

    win.calc_action.trigger()  # replaces the chats
    assert win.calc.isVisible() and not win.chat.isVisible() and db.book(book_id)["right_panel"] == "calc"
    win.notes_action.trigger()
    assert not win.calc_action.isChecked() and win.notes.isVisible()
    win.calc_action.trigger()
    win.show_library()
    win.open_book(pdf_path)
    assert win.calc.isVisible() and win.right.currentWidget() is win.calc and not win.notes_action.isChecked()

    win.show_page("practice")
    assert not win.practice_calc.isVisible() and not win.calc_action.isEnabled()
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_F11)
    assert win.practice_calc.isVisible() and win.calc_action.isChecked()  # the book's is left alone
    assert db.get_setting("practice_panel") == "calc"
    assert win.practice_calc.book_id is None  # before any question: the shared history

    win.practice.choose_deck(None)
    win.practice.start()
    seen = set()
    for _ in range(3):
        row = db.question(win.practice.question_id)
        assert win.practice_calc.book_id == row["book_id"]
        seen.add(row["book_id"])
        win.practice.act(row["id"], "skip")
    assert seen == {book_id, None}

    win.practice_calc.set_book(book_id)
    win.practice_calc.submit(r"6\cdot7")  # saved to the question's book, and the book's calculator sees it
    assert win.calc.entries[-1]["result"] == "42" and db.calculations(book_id)[-1]["result"] == "42"

    win.show_page("book")
    QTest.keyClick(win, Qt.Key.Key_F11)  # in the book, F11 is the book's calculator
    assert not win.calc_action.isChecked() and db.get_setting("practice_panel") == "calc"
    win.show_page("practice")
    assert win.practice_calc.isVisible()
    win.practice_calc.closeRequested.emit()
    assert not win.practice_calc.isVisible() and db.get_setting("practice_panel") == ""

    # The Practice page's Calculator button toggles its calculator and shows whether it's open.
    shown = []
    monkeypatch.setattr(win.practice, "_js", lambda f, *args: shown.append((f, args)))
    bridge = win.practice.channel.registeredObjects()["practice"]
    bridge.open("calc")
    assert win.practice_calc.isVisible() and win.practice.calculator is True and ("setCalc", (True,)) in shown
    win.practice.refresh()
    assert shown[-1][1][0]["calc"] is True
    bridge.open("calc")
    assert not win.practice_calc.isVisible() and win.practice.calculator is False
    win.close()


# -- Claude's tags and revisions -----------------------------------------------------------
TAGGED = r'''```quiz Zorblatts
{"questions": [
  {"type": "truefalse", "question": "Store it near a window?", "answer": false, "page": 1, "tags": ["Storage Rules", "care::light"]},
  {"type": "short_answer", "question": "Why rinse?", "answer": "To calibrate.", "tags": "rinsing"}
]}
```'''

REVISED = r'''Here it is, harder.
```quiz Zorblatts, harder
{"revises": "Zorblatts", "questions": [
  {"type": "short_answer", "question": "Why rinse, and why in tea?", "answer": "To calibrate; tea is neutral.", "revises": 4},
  {"type": "self_check", "question": "Rinse time?", "answer": "41 s", "revises": 1},
  {"type": "truefalse", "question": "Zorblatts have seven legs.", "answer": true}
]}
```'''


def test_claude_tags_its_quiz_questions(tmp_path):
    db, book_id, chat, reply = setup(tmp_path, TAGGED)
    practice.learn_toc(db, book_id, TOC)
    (q,) = quiz.quizzes(TAGGED)
    assert q["questions"][0]["tags"] == ["Storage_Rules", "care::light"] and q["questions"][1]["tags"] == ["rinsing"]
    practice.add_quiz_questions(db, book_id, reply, TAGGED)
    first, second = db.quiz_questions(reply, 0)
    assert db.question_group(first["group_id"])["tags"] == " Storage_Rules care::light A::Part_I::Chapter_1:_Rinsing_mostly "
    assert "tags" not in json.loads(first["data"])
    # Claude is shown the user's tags (not section tags) to reuse.
    assert practice.concept_tags(db) == ["care::light", "rinsing", "Storage_Rules"]
    assert "<existing_tags>care::light rinsing Storage_Rules</existing_tags>" in practice.tags_context(db)


def test_a_revised_quiz_takes_over_the_questions_it_keeps(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    old = db.quiz_questions(reply, 0)
    practice.attempt(db, old[0]["id"], 3, now=NOW)
    practice.attempt(db, old[1]["id"], 3, now=NOW)  # attempted, and left out of the revision
    db.add_message(chat, "user", "Revise your quiz “Zorblatts”: harder")
    revision = db.add_message(chat, "assistant", REVISED)
    practice.add_quiz_questions(db, book_id, revision, REVISED, now=NOW + 60)

    short, kept, new = db.quiz_questions(revision, 0)
    assert short["id"] == old[3]["id"] and json.loads(short["data"])["question"] == "Why rinse, and why in tea?"
    assert kept["id"] == old[0]["id"] and kept["reps"] == 1 and len(db.attempts(kept["id"])) == 1
    assert db.question_group(kept["group_id"])["kind"] == "self_check" and kept["title"] == "Zorblatts, harder"
    assert new["reps"] == 0 and new["id"] not in [c["id"] for c in old]
    # The old quiz keeps only its attempted question that wasn't carried over.
    assert [c["id"] for c in db.quiz_questions(reply, 0)] == [old[1]["id"]] and db.question(old[2]["id"]) is None
    assert quiz.revised_quiz([(reply, 0, quiz.quizzes(QUIZ)[0])], "nothing like it") == (reply, 0)


def test_chat_panel_revises_quizzes_and_shares_tags(chat_panel, monkeypatch):
    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me.", "Quiz", 0)
    panel.send()
    worker.done.emit(panel.chat_id, "s1", TAGGED, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    first = db.quiz_questions(reply, 0)[0]["id"]
    panel.revise_quiz(reply, 0, "make it harder")
    job = worker.jobs[-1]
    assert job.message == "Revise your quiz “Zorblatts”: make it harder" and "<existing_tags>" in job.context
    worker.done.emit(panel.chat_id, "s1", REVISED, "u2")
    revision = db.messages(panel.chat_id)[-1]["id"]
    assert panel._revisions(db.messages(panel.chat_id)) == {(reply, 0): revision}
    assert db.quiz_questions(revision, 0)[1]["id"] == first and not db.quiz_questions(reply, 0)
    # A redraw reads each reply's quizzes once, revisions included.
    read, split = [], quiz.split
    monkeypatch.setattr(quiz, "split", lambda text: read.append(text) or split(text))
    panel._refresh_transcript()
    assert sorted(read) == sorted([REVISED, TAGGED])
    read.clear()
    panel._update_quiz(revision, 0)
    assert sorted(read) == sorted([REVISED, TAGGED])


def test_browser_sidebar_orders_and_filters_tags(chat_panel):
    from study_reader.browser import Sidebar

    _, _, db, book_id = chat_panel
    deck = db.book_deck(book_id)
    for tag, page in (("A::Ch_10::Late_part", 5), ("A::Ch_2", 1), ("zebra", None), ("apple", None)):
        db.add_question_group("self_check", {"front": tag, "back": "x"}, deck, tags=[tag], book_id=book_id, page=page)
    bar = Sidebar(db)
    bar.refresh()
    tags = next(bar.topLevelItem(i) for i in range(bar.topLevelItemCount()) if bar.topLevelItem(i).text(0) == "Tags")
    labels = lambda item: [item.child(i).text(0) for i in range(item.childCount()) if not item.child(i).isHidden()]  # noqa: E731
    assert labels(tags) == ["A", "apple", "zebra"]
    assert labels(tags.child(0)) == ["Ch 2", "Ch 10"]  # reading order, underscores as spaces
    bar.filter_tags("late part")
    assert labels(tags) == ["A"] and labels(tags.child(0)) == ["Ch 10"] and tags.child(0).child(1).isExpanded()
    bar.filter_tags("")
    assert labels(tags) == ["A", "apple", "zebra"] and not tags.child(0).isExpanded()


def test_a_passage_across_pages_gives_its_questions_all_its_pages(tmp_path):
    db = Database(tmp_path / "t.db")
    book_id = db.upsert_book("h", "/a.pdf", "A", 10)["id"]
    chat = db.create_chat(book_id, "Quiz")
    rects = [[1, 2, 3, 4], [1, 2, 3, 4, 4], [1, 2, 3, 4, 5]]
    db.add_message(chat, "user", "Quiz me on this passage", [{"kind": "text", "page": 3, "text": "x", "rects": rects}])
    reply = db.add_message(chat, "assistant", "...")
    assert practice._asked_pages(db, reply) == (3, 5)


def test_a_book_narrows_picks_rather_than_replacing_them(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    other = db.upsert_book("h2", "/other.pdf", "Other", 3)["id"]
    assert db.count_questions(book_id) > 0
    assert db.count_questions(book_id, picks=[{"book": other, "section": None}]) == 0
