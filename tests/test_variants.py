import json
import sqlite3

import pytest

from study_reader import claude_client as cc, quiz, srs, variants
from study_reader.db import Database

from test_quiz import QUIZ

NUMERIC = {"type": "numeric", "question": "Rinse time?", "answer": "41", "unit": "s", "explanation": "",
           "page": 2, "answer_latex": "41"}

REPLY = r'''Original: 41 s. New: a 3-legged zorblatt rinses $\frac{3}{2}\cdot 6 = 9$ s.

```json
{"type": "numeric", "question": "Rinse time for $\frac{3}{2}$ units?", "answer": "3/2*6", "unit": "s",
 "explanation": "Six seconds a unit.", "tags": ["x"]}
```'''


MARKED = """```quiz Marked
[{"type": "numeric", "question": "2+2?", "answer": 4, "varies": true},
 {"type": "written", "question": "Why?", "answer": "Tea.", "varies": false},
 {"type": "written", "question": "How?", "answer": "Rinse."},
 {"type": "truefalse", "question": "Wet?", "answer": true, "varies": true}]
```"""


def test_parse_new_versions():
    q = variants.parse(REPLY, NUMERIC)
    assert q["type"] == "numeric" and q["question"] == r"Rinse time for $\frac{3}{2}$ units?"
    assert q["answer"] == "3/2*6" and q["unit"] == "s" and q["page"] == 2 and "tags" not in q
    assert variants.parse('Nothing to vary.\n```json\n{"skip": true}\n```', NUMERIC) == variants.SKIP
    assert variants.parse('{"skip": true}', NUMERIC) == variants.SKIP  # no fence
    with pytest.raises(ValueError):
        variants.parse('```json\n{"type": "written", "question": "Why?", "answer": "Tea."}\n```', NUMERIC)
    with pytest.raises(ValueError):
        variants.parse('```json\n{"type": "numeric", "question": "?", "answer": "lots"}\n```', NUMERIC)


def test_request_names_the_question_and_the_last_version():
    text = variants.request({**NUMERIC, "varies": True}, {**NUMERIC, "answer": "9"}, "A", "Zorblatts")
    assert '("Zorblatts")' in text and 'on "A"' in text and "<last_version>" in text and '"answer": "9"' in text
    assert "answer_latex" not in text and '"page"' not in text and "varies" not in text.split("</last_version>")[0]
    assert "on \"" not in variants.request(NUMERIC)


def test_quizzes_mark_which_questions_vary():
    (q,) = quiz.quizzes(MARKED)
    assert [x.get("varies") for x in q["questions"]] == [True, False, None, None]
    assert "\"varies\"" in cc.READER_INSTRUCTIONS


def _review(chat_panel):
    from study_reader.review import ReviewPanel, VariantMaker

    panel, worker, db, book_id = chat_panel
    maker = VariantMaker(db, worker)
    panel.variants = maker
    panel.start_quiz("Quiz me.", "Quiz", 0)
    panel.send()
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    review = ReviewPanel(db, variants=maker)
    review.choose_book(book_id)
    shown = []
    review._js = lambda fn, *args: shown.append((fn, args))
    return review, worker, db, reply, shown


def test_reviewed_numeric_cards_come_back_with_new_numbers(chat_panel):
    review, worker, db, reply, shown = _review(chat_panel)
    numeric = db.card_for(reply, 0, 2)["id"]
    review.start()
    while review.card_id != numeric:
        review.act(review.card_id, "skip")
    assert not shown[-1][1][0]["varied"]
    review.check(numeric, {"latex": "41"})
    review.rate(numeric, 1)
    job, card_id = worker.variants[-1]
    # Asked without the book, in a fresh session.
    assert card_id == numeric and job.book is None and job.chat_session_id is None and not job.history
    assert "fresh version" in job.question and "<last_version>" not in job.question and 'on "A"' in job.question

    # Choice and true/false cards aren't varied.
    assert [c for _, c in worker.variants] == [numeric]

    worker.varied.emit(numeric, REPLY)
    card = db.card(numeric)
    assert variants.is_varied(card) and variants.question(card)["answer"] == "3/2*6"
    assert srs.question(card)["answer"] == "41"  # the card's own question is unchanged

    db.update_card(numeric, due=0)
    while review.card_id != numeric:
        review.act(review.card_id, "skip")
    payload = shown[-1][1][0]
    assert payload["varied"] and payload["question"]["answer"] == "3/2*6"
    # A newer version arriving while it is on display doesn't replace it.
    worker.varied.emit(numeric, REPLY.replace("3/2*6", "7"))  # not asked for: ignored
    db.update_card(numeric, variant=json.dumps({**NUMERIC, "answer": "7"}))
    review.check(numeric, {"latex": "9"})
    assert review._answer["verdict"] == "correct" and shown[-1][1][0]["question"]["answer"] == "3/2*6"
    review.rate(numeric, 3)
    assert "<last_version>" in worker.variants[-1][0].question


def test_written_cards_without_numbers_are_skipped_for_good(chat_panel):
    review, worker, db, reply, shown = _review(chat_panel)
    written = db.card_for(reply, 0, 3)["id"]
    review.start()
    while review.card_id != written:
        review.act(review.card_id, "skip")
    review.check(written, {"text": "Tea."})
    review.rate(written, 3)
    assert worker.variants[-1][1] == written
    worker.varied.emit(written, '{"skip": true}')
    card = db.card(written)
    assert not variants.is_varied(card) and not variants.wants_variant(card)
    assert variants.question(card) == srs.question(card)


def test_first_answer_in_chat_asks_for_a_new_version(chat_panel):
    panel, worker, db, book_id = chat_panel
    review, worker, db, reply, shown = _review(chat_panel)
    panel._on_quiz_answer(reply, 0, 2, json.dumps({"latex": "41"}))
    assert worker.variants[-1][1] == db.card_for(reply, 0, 2)["id"]
    panel._on_quiz_answer(reply, 0, 0, json.dumps({"choice": 1}))
    assert len(worker.variants) == 1


def test_questions_marked_not_to_vary_are_never_asked_about(tmp_path):
    from test_srs import setup
    db, book_id, chat, reply = setup(tmp_path, MARKED)
    srs.add_quiz_cards(db, book_id, reply, MARKED)
    assert [variants.wants_variant(c) for c in db.quiz_cards(reply, 0)] == [True, False, True, False]


def test_editing_a_card_drops_its_new_version(tmp_path):
    from test_srs import setup
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ)
    card = db.card_for(reply, 0, 2)
    db.update_card(card["id"], variant=json.dumps({**NUMERIC, "answer": "9"}))
    note = db.card_note(card["note_id"])
    db.update_card_note(note["id"], tags=["x"])  # tags only: kept
    assert variants.is_varied(db.card(card["id"]))
    db.update_card_note(note["id"], json.loads(note["fields"]))  # the same fields: kept
    assert variants.is_varied(db.card(card["id"]))
    db.update_card_note(note["id"], {**json.loads(note["fields"]), "answer": "42"})
    assert db.card(card["id"])["variant"] == ""


def test_migration_adds_the_variant_column(tmp_path):
    Database(tmp_path / "t.db").conn.close()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("ALTER TABLE cards DROP COLUMN variant")
    conn.commit()
    conn.close()
    db = Database(tmp_path / "t.db")
    assert "variant" in {r["name"] for r in db.conn.execute("PRAGMA table_info(cards)")}


def test_new_versions_can_be_turned_off(chat_panel, tmp_path):
    from study_reader import phone_sync

    review, worker, db, reply, shown = _review(chat_panel)
    numeric = db.card_for(reply, 0, 2)["id"]
    db.update_card(numeric, variant=json.dumps({**NUMERIC, "answer": "7"}))
    db.set_setting(variants.ENABLED_SETTING, "0")
    card = db.card(numeric)
    assert variants.question(card, db) == srs.question(card) and variants.question(card)["answer"] == "7"
    review.variants.after_review(numeric)
    assert worker.variants == []
    phone_sync.export_snapshot(db, tmp_path)
    snap = sqlite3.connect(tmp_path / phone_sync.SNAPSHOT)
    assert snap.execute("SELECT variant FROM cards WHERE id=?", (numeric,)).fetchone() == ("",)

    db.set_setting(variants.ENABLED_SETTING, "1")
    assert variants.question(db.card(numeric), db)["answer"] == "7"
    review.variants.after_review(numeric)
    assert worker.variants[-1][1] == numeric


def test_a_cards_new_versions_are_switched_in_the_browser(chat_panel):
    from study_reader.browser import CardBrowser

    review, worker, db, reply, shown = _review(chat_panel)
    written = db.card_for(reply, 0, 3)["id"]
    db.update_card(written, variant=json.dumps(variants.SKIP))  # Claude found nothing to vary
    browser = CardBrowser(db)
    browser.show()
    browser.set_search(f"cid:{written}")
    e = browser.editor
    assert e.q_varies.isVisible() and not e.q_varies.isChecked()
    e.q_varies.click()  # on: asked again, and told to rephrase rather than skip
    browser.save_note()
    card = db.card(written)
    assert srs.question(card)["varies"] is True and variants.wants_variant(card)
    assert "skip" not in variants.request(srs.question(card))
    e.q_varies.click()
    browser.save_note()
    assert srs.question(db.card(written))["varies"] is False and not variants.wants_variant(db.card(written))
    browser.close()
