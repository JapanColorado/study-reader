import json
import sqlite3

import pytest

from study_reader import claude_client as cc, quiz, practice, variants
from study_reader.db import Database

from test_quiz import QUIZ

CALCULATION = {"type": "calculation", "question": "Rinse time?", "answer": "41", "unit": "s", "explanation": "",
           "page": 2, "answer_latex": "41"}

REPLY = r'''Original: 41 s. New: a 3-legged zorblatt rinses $\frac{3}{2}\cdot 6 = 9$ s.

```json
{"type": "calculation", "question": "Rinse time for $\frac{3}{2}$ units?", "answer": "3/2*6", "unit": "s",
 "explanation": "Six seconds a unit.", "tags": ["x"]}
```'''


MARKED = """```quiz Marked
[{"type": "calculation", "question": "2+2?", "answer": 4, "varies": true},
 {"type": "short_answer", "question": "Why?", "answer": "Tea.", "varies": false},
 {"type": "short_answer", "question": "How?", "answer": "Rinse."},
 {"type": "truefalse", "question": "Wet?", "answer": true, "varies": true}]
```"""


def test_parse_new_versions():
    q = variants.parse(REPLY, CALCULATION)
    assert q["type"] == "calculation" and q["question"] == r"Rinse time for $\frac{3}{2}$ units?"
    assert q["answer"] == "3/2*6" and q["unit"] == "s" and q["page"] == 2 and "tags" not in q
    assert variants.parse('Nothing to vary.\n```json\n{"skip": true}\n```', CALCULATION) == variants.SKIP
    assert variants.parse('{"skip": true}', CALCULATION) == variants.SKIP  # no fence
    # Told to rephrase rather than skip, and multiple choice always gets new choices: no skip is kept.
    with pytest.raises(ValueError):
        variants.parse('{"skip": true}', {**CALCULATION, "varies": True})
    with pytest.raises(ValueError):
        variants.parse('{"skip": true}', CHOICE)
    with pytest.raises(ValueError):
        variants.parse('```json\n{"type": "short_answer", "question": "Why?", "answer": "Tea."}\n```', CALCULATION)
    with pytest.raises(ValueError):
        variants.parse('```json\n{"type": "calculation", "question": "?", "answer": "lots"}\n```', CALCULATION)


def test_request_names_the_question_and_the_last_version():
    text = variants.request({**CALCULATION, "varies": True}, {**CALCULATION, "answer": "9"}, "A", "Zorblatts")
    assert '("Zorblatts")' in text and 'on "A"' in text and "<last_version>" in text and '"answer": "9"' in text
    assert "answer_latex" not in text and '"page"' not in text and "varies" not in text.split("</last_version>")[0]
    assert "on \"" not in variants.request(CALCULATION)


def test_quizzes_mark_which_questions_vary():
    (q,) = quiz.quizzes(MARKED)
    assert [x.get("varies") for x in q["questions"]] == [True, False, None, None]
    assert "\"varies\"" in cc.READER_INSTRUCTIONS


def _practice(chat_panel):
    from study_reader.practice_view import PracticePanel, VariantMaker

    panel, worker, db, book_id = chat_panel
    maker = VariantMaker(db, worker)
    panel.variants = maker
    panel.start_quiz("Quiz me.", "Quiz", 0)
    panel.send()
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    page = PracticePanel(db, variants=maker)
    page.choose_book(book_id)
    shown = []
    page._js = lambda fn, *args: shown.append((fn, args))
    return page, worker, db, reply, shown


def test_attempted_calculations_come_back_with_new_numbers(chat_panel):
    page, worker, db, reply, shown = _practice(chat_panel)
    calculation = db.question_for(reply, 0, 2)["id"]
    page.start()
    while page.question_id != calculation:
        page.act(page.question_id, "skip")
    assert not shown[-1][1][0]["varied"]
    page.check(calculation, {"latex": "41"})
    page.rate(calculation, 1)
    job, question_id = worker.variants[-1]
    # Asked without the book, in a fresh session.
    assert question_id == calculation and job.book is None and job.chat_session_id is None and not job.history
    assert "fresh version" in job.message and "<last_version>" not in job.message and 'on "A"' in job.message

    # True/false questions aren't varied.
    truefalse = db.question_for(reply, 0, 1)["id"]
    page.variants.after_attempt(truefalse)
    assert [c for _, c in worker.variants] == [calculation]

    worker.varied.emit(calculation, REPLY)
    row = db.question(calculation)
    assert variants.is_varied(row) and variants.question(row)["answer"] == "3/2*6"
    assert practice.question(row)["answer"] == "41"  # the question as written is unchanged

    db.update_question(calculation, due=0)
    while page.question_id != calculation:
        page.act(page.question_id, "skip")
    payload = shown[-1][1][0]
    assert payload["varied"] and payload["question"]["answer"] == "3/2*6"
    # A newer version arriving while it is on display doesn't replace it.
    worker.varied.emit(calculation, REPLY.replace("3/2*6", "7"))  # not asked for: ignored
    db.update_question(calculation, variant=json.dumps({**CALCULATION, "answer": "7"}))
    page.check(calculation, {"latex": "9"})
    assert page._answer["verdict"] == "correct" and shown[-1][1][0]["question"]["answer"] == "3/2*6"
    page.rate(calculation, 3)
    assert "<last_version>" in worker.variants[-1][0].message


CHOICE = {"type": "choice", "question": "What rinses a zorblatt?", "choices": ["Tea", "Sand", "Wind", "Soap"],
          "answer": 0, "explanation": "Tea does.", "page": 2, "tags": ["x"]}
CHOICES_REPLY = r'''Tea is the one.

```json
{"question": "Something else?", "choices": ["Dust", "A brew of $\frac{1}{2}$ tea", "Air", "Suds"], "answer": 1}
```'''


def test_multiple_choice_requests_and_replies():
    text = variants.request(CHOICE, {**CHOICE, "choices": ["Mud", "Fog", "Brewed leaves", "Oil"], "answer": 2}, "A")
    assert "new set of choices" in text and "random order" in text and "skip" not in text
    assert '"Sand"' in text and "<last_version>" in text and '"Brewed leaves"' in text and "the last version's" in text
    assert "<last_version>" not in variants.request(CHOICE) and "new set of choices" not in variants.request(CALCULATION)
    q = variants.parse(CHOICES_REPLY, {**CHOICE, "html": True})
    # The question text is the one written, whatever the reply says; the explanation too when none is given.
    assert q["question"] == CHOICE["question"] and q["explanation"] == "Tea does." and q["page"] == 2
    assert q["choices"][1] == r"A brew of $\frac{1}{2}$ tea" and q["answer"] == 1 and q["html"] and "tags" not in q
    assert quiz.grade(q, {"choice": 1}) == "correct"
    with pytest.raises(ValueError):
        variants.parse('{"choices": ["Dust", "Tea"], "answer": 2}', CHOICE)
    with pytest.raises(ValueError):
        variants.parse(REPLY, CHOICE)


def test_attempted_multiple_choice_questions_come_back_with_new_choices(chat_panel):
    page, worker, db, reply, shown = _practice(chat_panel)
    choice = db.question_for(reply, 0, 0)["id"]
    own = practice.question(db.question(choice))
    page.start()
    while page.question_id != choice:
        page.act(page.question_id, "skip")
    page.check(choice, {"choice": own["answer"]})
    page.rate(choice, 3)
    job, question_id = worker.variants[-1]
    assert question_id == choice and job.book is None and "new set of choices" in job.message
    right = (own["answer"] + 1) % len(own["choices"])
    worker.varied.emit(choice, json.dumps({"choices": [f"new {k}" for k in range(len(own["choices"]))], "answer": right}))
    assert practice.question(db.question(choice)) == own

    db.update_question(choice, due=0)
    while page.question_id != choice:
        page.act(page.question_id, "skip")
    payload = shown[-1][1][0]
    assert payload["varied"] and payload["question"]["question"] == own["question"]
    assert payload["question"]["choices"][right] == f"new {right}"
    page.check(choice, {"choice": right})  # graded against the new choices
    assert page._answer["verdict"] == "correct"
    page.rate(choice, 3)
    assert "<last_version>" in worker.variants[-1][0].message

    # "varies": false (the browser's checkbox) leaves a question's choices alone.
    group = db.question_group(db.question(choice)["group_id"])
    db.update_question_group(group["id"], {**json.loads(group["fields"]), "varies": False})
    row = db.question(choice)
    assert practice.question(row)["varies"] is False and not variants.wants_variant(row) and not variants.is_varied(row)


def test_short_answers_without_numbers_are_skipped_for_good(chat_panel):
    page, worker, db, reply, shown = _practice(chat_panel)
    short = db.question_for(reply, 0, 3)["id"]
    page.start()
    while page.question_id != short:
        page.act(page.question_id, "skip")
    page.check(short, {"text": "Tea."})
    page.rate(short, 3)
    assert worker.variants[-1][1] == short
    worker.varied.emit(short, '{"skip": true}')
    row = db.question(short)
    assert not variants.is_varied(row) and not variants.wants_variant(row)
    assert variants.question(row) == practice.question(row)


def test_questions_that_always_vary_are_not_skipped_for_good(chat_panel):
    page, worker, db, reply, shown = _practice(chat_panel)
    short = db.question_for(reply, 0, 3)["id"]
    group = db.question_group(db.question(short)["group_id"])
    db.update_question_group(group["id"], {**json.loads(group["fields"]), "varies": True})
    page.variants.after_attempt(short)
    worker.varied.emit(short, '{"skip": true}')  # declined anyway: nothing kept, asked again next time
    row = db.question(short)
    assert row["variant"] == "" and variants.wants_variant(row)
    # A skip stored before (a choice question, or one marked since) doesn't stop it being asked.
    db.update_question(short, variant=json.dumps(variants.SKIP))
    assert variants.wants_variant(db.question(short))
    page.variants.after_attempt(short)
    assert [c for _, c in worker.variants] == [short, short]


def test_first_answer_in_chat_asks_for_a_new_version(chat_panel):
    panel, worker, db, book_id = chat_panel
    page, worker, db, reply, shown = _practice(chat_panel)
    panel._on_quiz_answer(reply, 0, 2, json.dumps({"latex": "41"}))
    assert worker.variants[-1][1] == db.question_for(reply, 0, 2)["id"]
    panel._on_quiz_answer(reply, 0, 1, json.dumps({"value": True}))
    assert len(worker.variants) == 1
    panel._on_quiz_answer(reply, 0, 0, json.dumps({"choice": 1}))  # multiple choice: new choices
    assert worker.variants[-1][1] == db.question_for(reply, 0, 0)["id"] and len(worker.variants) == 2


def test_questions_marked_not_to_vary_are_never_asked_about(tmp_path):
    from test_practice import setup
    db, book_id, chat, reply = setup(tmp_path, MARKED)
    practice.add_quiz_questions(db, book_id, reply, MARKED)
    assert [variants.wants_variant(c) for c in db.quiz_questions(reply, 0)] == [True, False, True, False]


def test_editing_a_question_drops_its_new_version(tmp_path):
    from test_practice import setup
    db, book_id, chat, reply = setup(tmp_path)
    practice.add_quiz_questions(db, book_id, reply, QUIZ)
    row = db.question_for(reply, 0, 2)
    db.update_question(row["id"], variant=json.dumps({**CALCULATION, "answer": "9"}))
    group = db.question_group(row["group_id"])
    db.update_question_group(group["id"], tags=["x"])  # tags only: kept
    assert variants.is_varied(db.question(row["id"]))
    db.update_question_group(group["id"], json.loads(group["fields"]))  # the same fields: kept
    assert variants.is_varied(db.question(row["id"]))
    db.update_question_group(group["id"], {**json.loads(group["fields"]), "answer": "42"})
    assert db.question(row["id"])["variant"] == ""


def test_migration_adds_the_variant_column(tmp_path):
    Database(tmp_path / "t.db").conn.close()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("ALTER TABLE questions DROP COLUMN variant")
    conn.commit()
    conn.close()
    db = Database(tmp_path / "t.db")
    assert "variant" in {r["name"] for r in db.conn.execute("PRAGMA table_info(questions)")}


def test_new_versions_can_be_turned_off(chat_panel, tmp_path):
    from study_reader import phone_sync

    page, worker, db, reply, shown = _practice(chat_panel)
    calculation = db.question_for(reply, 0, 2)["id"]
    db.update_question(calculation, variant=json.dumps({**CALCULATION, "answer": "7"}))
    db.set_setting(variants.ENABLED_SETTING, "0")
    row = db.question(calculation)
    assert variants.question(row, db) == practice.question(row) and variants.question(row)["answer"] == "7"
    page.variants.after_attempt(calculation)
    assert worker.variants == []
    phone_sync.export_snapshot(db, tmp_path)
    snap = sqlite3.connect(tmp_path / phone_sync.SNAPSHOT)
    assert snap.execute("SELECT variant FROM questions WHERE id=?", (calculation,)).fetchone() == ("",)

    db.set_setting(variants.ENABLED_SETTING, "1")
    assert variants.question(db.question(calculation), db)["answer"] == "7"
    page.variants.after_attempt(calculation)
    assert worker.variants[-1][1] == calculation


def test_a_questions_new_versions_are_switched_in_the_browser(chat_panel):
    from study_reader.browser import QuestionBrowser

    page, worker, db, reply, shown = _practice(chat_panel)
    short = db.question_for(reply, 0, 3)["id"]
    db.update_question(short, variant=json.dumps(variants.SKIP))  # Claude found nothing to vary
    browser = QuestionBrowser(db)
    browser.show()
    browser.set_search(f"cid:{short}")
    e = browser.editor
    assert e.q_varies.isVisible() and not e.q_varies.isChecked()
    e.q_varies.click()  # on: asked again, and told to rephrase rather than skip
    browser.save_question()
    row = db.question(short)
    assert practice.question(row)["varies"] is True and variants.wants_variant(row)
    assert "skip" not in variants.request(practice.question(row))
    e.q_varies.click()
    browser.save_question()
    assert practice.question(db.question(short))["varies"] is False and not variants.wants_variant(db.question(short))
    browser.close()
