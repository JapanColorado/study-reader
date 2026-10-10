import json
import time

from study_reader import quiz, srs
from study_reader.db import Database

from test_quiz import QUIZ

CARDS = r'''```quiz Zorblatt facts
{"questions": [
  {"type": "flashcard", "question": "How long is a zorblatt rinsed?", "answer": "41 seconds, in cold tea.", "page": 2},
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


# -- flashcards in quizzes -----------------------------------------------------------------
def test_flashcards_parse_and_grade_by_rating():
    (q,) = quiz.quizzes(CARDS)
    front, back = q["questions"]
    assert front["type"] == back["type"] == "flashcard" and back["answer"] == "- near a window"
    assert [quiz.grade(front, {"rating": r}) for r in (1, 2, 3, 4)] == ["incorrect", "partial", "correct", "correct"]
    assert quiz.describe_response(front, {"rating": 2}) == "rated my recall Hard"
    _, (bad,) = quiz.split('```quiz\n[{"type": "flashcard", "question": "Front only"}]\n```')
    assert "back" in bad["error"]


# -- cards ---------------------------------------------------------------------------------
def test_every_quiz_question_becomes_a_card_once(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    assert len(ids) == 4 and srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW) == []
    cards = db.quiz_cards(reply, 0)
    assert [c["question"] for c in cards] == [0, 1, 2, 3] and cards[0]["title"] == "Zorblatts"
    # The question's own page, else the page of the passage the quiz was asked about.
    assert [c["page"] for c in cards] == [0, 2, 2, 2]
    assert srs.question(cards[2])["type"] == "numeric" and all(srs.is_new(c) for c in cards)


def test_first_answer_in_the_chat_is_the_first_review(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    assert srs.answered_in_chat(db, reply, 0, 0, "correct", {"choice": 1}, now=NOW)
    card = db.card_for(reply, 0, 0)
    assert card["reps"] == 1 and card["stage"] > 0 and card["due"] > NOW
    (log,) = db.reviews(card["id"])
    assert (log["rating"], log["source"], log["verdict"]) == (3, "chat", "correct")
    # A retake doesn't touch the schedule; ungraded answers don't count.
    assert not srs.answered_in_chat(db, reply, 0, 0, "incorrect", {"choice": 0}, now=NOW + 60)
    assert not srs.answered_in_chat(db, reply, 0, 3, None, {"text": "?"}, now=NOW)
    assert db.card_for(reply, 0, 3)["reps"] == 0


def test_ratings_space_reviews_out(tmp_path):
    db, book_id, chat, reply = setup(tmp_path, CARDS)
    card_id = srs.add_quiz_cards(db, book_id, reply, CARDS, now=NOW)[0]
    t = NOW
    for _ in range(4):
        srs.review(db, card_id, 3, now=t)
        t = db.card(card_id)["due"]
    card = db.card(card_id)
    assert card["stage"] == 2 and card["due"] - NOW > 3 * 86400 and card["reps"] == 4
    srs.review(db, card_id, 1, now=card["due"])
    assert db.card(card_id)["lapses"] == 1 and db.card(card_id)["stage"] == 3
    previews = srs.previews(db.card(card_id), now=NOW)
    assert set(previews) == {1, 2, 3, 4} and all(previews.values())


def test_queue_order_new_card_limit_and_suspension(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    a, b, c, d = srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    assert srs.counts(db, book_id, NOW) == {"due": 0, "new": 4, "new_total": 4, "total": 4, "suspended": 0}
    assert srs.next_card(db, book_id, NOW)["id"] == a  # new cards in the order they were written

    db.set_setting(srs.NEW_PER_DAY_SETTING, "1")
    srs.review(db, a, 1, now=NOW)  # introduced in review: uses up today's one new card
    assert srs.counts(db, book_id, NOW)["new"] == 0
    # `a` is back in a minute; until then, with no new cards left today, it is shown early...
    assert srs.next_card(db, book_id, NOW)["id"] == a
    # ...and once due it comes before anything else.
    assert srs.counts(db, book_id, NOW + 120)["due"] == 1 and srs.next_card(db, book_id, NOW + 120)["id"] == a
    # A chat answer isn't counted against the new-card allowance.
    srs.answered_in_chat(db, reply, 0, 1, "correct", {"value": False}, now=NOW)
    assert db.new_cards_reviewed_since(srs.day_start(NOW)) == 1

    db.update_card(a, suspended=1)
    db.set_quiz_suspended(reply, 0, True)
    assert srs.next_card(db, book_id, NOW + 3600) is None and srs.counts(db, book_id, NOW)["suspended"] == 4
    db.set_quiz_suspended(reply, 0, False)
    db.set_setting(srs.NEW_PER_DAY_SETTING, "20")
    # Skipping the card just shown, unless it's the only one.
    first = srs.next_card(db, book_id, NOW + 3600)
    second = srs.next_card(db, book_id, NOW + 3600, skip=[first["id"]])
    third = srs.next_card(db, book_id, NOW + 3600, skip=[first["id"], second["id"]])
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert srs.counts(db, None, NOW)["total"] == 4  # all books


def test_review_days_roll_over_at_4am():
    from datetime import datetime
    late = datetime(2026, 3, 10, 2, 30).timestamp()
    assert datetime.fromtimestamp(srs.day_start(late)) == datetime(2026, 3, 9, 4)
    assert datetime.fromtimestamp(srs.next_day(late)) == datetime(2026, 3, 10, 4)
    assert srs.format_interval(30) == "1m" and srs.format_interval(3 * 86400) == "3d"


def test_deleting_messages_keeps_only_reviewed_cards(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    srs.answered_in_chat(db, reply, 0, 0, "correct", {"choice": 1}, now=NOW)
    kept = db.card_for(reply, 0, 0)["id"]
    db.delete_messages_from(chat, reply)  # e.g. regenerating the reply
    (card,) = db.cards(book_id)
    assert card["id"] == kept and card["message_id"] is None and len(db.reviews(kept)) == 1

    reply2 = db.add_message(chat, "assistant", CARDS)
    srs.add_quiz_cards(db, book_id, reply2, CARDS, now=NOW)
    db.delete_chat(chat)
    assert [c["id"] for c in db.cards(book_id)] == [kept]
    db.delete_book(book_id)
    assert db.cards() == []


def test_backfill_makes_cards_for_earlier_quizzes(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    db.save_quiz_answer(reply, 0, 0, '{"choice": 0}', "done", "incorrect")
    db.save_quiz_answer(reply, 0, 3, '{"text": "Tea"}', "pending")
    assert srs.backfill(db) == 1 and srs.backfill(db) == 0
    cards = db.quiz_cards(reply, 0)
    assert len(cards) == 4 and [c["reps"] for c in cards] == [1, 0, 0, 0]
    assert db.reviews(cards[0]["id"])[0]["rating"] == 1


# -- the chat panel ------------------------------------------------------------------------
def test_chat_quizzes_feed_the_deck(chat_panel):
    panel, worker, db, book_id = chat_panel
    changes = []
    panel.cardsChanged.connect(lambda: changes.append(1))
    panel.start_quiz("Make flashcards on this passage.", "Cards: x", 0)
    chat = panel.chat_id
    worker.done.emit(chat, "s1", QUIZ + "\n\n" + CARDS, "u1")
    reply = db.messages(chat)[1]["id"]
    assert len(db.cards(book_id)) == 6 and changes

    panel._on_quiz_answer(reply, 1, 0, json.dumps({"rating": 4}))
    card = db.card_for(reply, 1, 0)
    assert card["reps"] == 1 and db.reviews(card["id"])[0]["rating"] == 4
    assert db.quiz_answers(chat)[reply][0]["verdict"] == "correct"

    # A free response counts once Claude has graded it.
    panel._on_quiz_answer(reply, 0, 3, json.dumps({"text": "Because tea."}))
    _, answer_id = worker.gradings[-1]
    assert db.card_for(reply, 0, 3)["reps"] == 0
    worker.graded.emit(answer_id, '{"verdict": "partial", "feedback": "Why tea?"}')
    assert db.reviews(db.card_for(reply, 0, 3)["id"])[0]["rating"] == 2

    view = panel._quiz_view(quiz.quizzes(db.message(reply)["text"])[0], 0, [], reply)
    assert view["review"] == "on"
    panel._on_quiz_review_toggled(reply, 0)
    assert all(c["suspended"] for c in db.quiz_cards(reply, 0)) and not db.quiz_cards(reply, 1)[0]["suspended"]
    assert panel._quiz_view(quiz.quizzes(db.message(reply)["text"])[0], 0, [], reply)["review"] == "off"

    # A quiz on a reply is asked for in the same chat.
    worker.done.emit(chat, "s1", QUIZ, "u1")
    panel.quiz_on_reply(reply)
    assert worker.jobs[-1].question.startswith("Quiz me on your earlier reply that begins “Here's a quiz.")
    worker.done.emit(chat, "s2", "Ok", "u2")
    panel.quiz_on_reply(db.messages(chat)[-1]["id"])
    assert worker.jobs[-1].question == "Quiz me on your last reply."


# -- the review panel ----------------------------------------------------------------------
def test_reviewing_cards(chat_panel):
    from study_reader.review import ReviewPanel

    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me.", "Quiz", 0)
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    review = ReviewPanel(db, worker)
    review.set_book(book_id)
    shown = []
    review._js = lambda fn, *args: shown.append((fn, args))
    reviewed = []
    review.countsChanged.connect(lambda: reviewed.append(1))

    review.start()
    first = review.card_id
    assert first == db.card_for(reply, 0, 0)["id"] and shown[-1][0] == "showCard"
    assert shown[-1][1][0]["previews"] is None and shown[-1][1][0]["new"]
    review.check(first, {"choice": 1})
    payload = shown[-1][1][0]
    assert payload["answer"]["verdict"] == "correct" and payload["suggested"] == 3 and set(payload["previews"]) == {1, 2, 3, 4}
    review.rate(first, 3)
    assert db.card(first)["reps"] == 1 and db.reviews(first)[0]["source"] == "review" and reviewed
    assert review.card_id != first

    # A numeric answer it can't read leaves the card unanswered.
    while srs.question(db.card(review.card_id))["type"] != "numeric":
        review.act(review.card_id, "skip")
    review.check(review.card_id, {"latex": r"\frac{1}{"})
    assert shown[-1][0] == "inputError" and review._answer is None

    # Free responses: graded by Claude in a detached fork at the quiz's reply.
    while srs.question(db.card(review.card_id))["type"] != "free":
        review.act(review.card_id, "skip")
    free = review.card_id
    review.grade_with_claude(free, "Because tea.")
    job, card_id = worker.card_gradings[-1]
    assert card_id == free and job.detached and (job.chat_session_id, job.resume_at) == ("s1", "u1")
    assert "spaced-repetition" in job.question and "Because tea." in job.question
    worker.cardGraded.emit(free, '{"verdict": "incorrect", "feedback": "No."}')
    assert review._answer["verdict"] == "incorrect" and shown[-1][1][0]["suggested"] == 1
    review.rate(free, 1)
    assert db.reviews(free)[0]["verdict"] == "incorrect"

    review.act(review.card_id, "suspend")
    assert db.count_cards(book_id, "suspended=1") == 1
    review.stop()
    assert shown[-1][0] == "showOverview" and shown[-1][1][0]["reviewed"] == 2


def test_main_window_review_panel(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.open_book(pdf_path)
    chat = db.create_chat(win.book_id, "Quiz")
    reply = db.add_message(chat, "assistant", CARDS)
    srs.add_quiz_cards(db, win.book_id, reply, CARDS)
    win._on_cards_changed()
    assert "2 cards to review" in win.review_action.toolTip()

    win.review_action.trigger()
    assert win.right.currentWidget() is win.review and db.book(win.book_id)["right_panel"] == "review"
    win._review_source(win.book_id, 2)
    assert win.reader.current_page() == 2

    win.show_review_all()
    assert win.center.currentWidget() is win.review_all and win.book_id is None
    assert win.library.review.text() == "Review · 2"
    win._review_chat(db.book_by_hash(db.recent_books()[0]["hash"])["id"], chat)
    assert win.center.currentWidget() is win.reader and win.chat.chat_id == chat
    win.close()
