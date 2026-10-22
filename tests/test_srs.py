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
    reply3 = db.add_message(db.create_chat(book_id, "Again"), "assistant", CARDS)
    srs.add_quiz_cards(db, book_id, reply3, CARDS, now=NOW)
    db.delete_book(book_id)  # reviewed cards stay in their deck; unreviewed ones go with the chats
    (card,) = db.cards()
    assert card["id"] == kept and card["book_id"] is None


def test_backfill_makes_cards_for_earlier_quizzes(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    db.save_quiz_answer(reply, 0, 0, '{"choice": 0}', "done", "incorrect")
    db.save_quiz_answer(reply, 0, 3, '{"text": "Tea"}', "pending")
    assert srs.backfill(db) == 1 and srs.backfill(db) == 0
    cards = db.quiz_cards(reply, 0)
    assert len(cards) == 4 and [c["reps"] for c in cards] == [1, 0, 0, 0]
    assert db.reviews(cards[0]["id"])[0]["rating"] == 1


# -- sections ------------------------------------------------------------------------------
TOC = [[1, "Part I", 0], [2, "Chapter 1: Rinsing, mostly", 0], [2, "Chapter 2", 2], [3, "2.1 Storage", 2], [1, "Index", 9]]


def test_section_tags_from_the_contents():
    assert srs.section_path(TOC, 1) == ["Part I", "Chapter 1: Rinsing, mostly"]
    assert srs.section_path(TOC, 2) == ["Part I", "Chapter 2", "2.1 Storage"]
    assert srs.section_path(TOC, None) == [] and srs.section_path([[1, "Late", 3]], 1) == []
    assert srs.section_tag("A Book, Vol. 1", TOC, 0) == "A_Book_Vol._1::Part_I::Chapter_1:_Rinsing_mostly"
    assert srs.section_tag("A", [], 0) is None
    assert srs.section_tag("A", TOC, 2, 3) == "A::Part_I::Chapter_2::2.1_Storage"
    assert srs.section_tag("A", TOC, 0, 3) == "A::Part_I" and srs.section_tag("A", TOC, 0, 9) is None
    assert srs.section_tag("A", [[1, "Chapter 6 Firms \x00\x00", 0]], 0) == "A::Chapter_6_Firms"


def test_migration_cleans_nul_padded_section_tags(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ)
    note = db.card_note(db.cards(book_id)[0]["note_id"])
    with db.conn:
        db.conn.execute("UPDATE card_notes SET tags=? WHERE id=?", (" A::Chapter_6_\x00\x00 a ", note["id"]))
    db.conn.close()
    db = Database(tmp_path / "t.db")
    assert db.card_note(note["id"])["tags"] == " A::Chapter_6 a "
    assert db.count_cards(tag="A::Chapter_6") == 1


def test_quiz_cards_are_tagged_with_their_section(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ)
    assert all(db.card_note(c["note_id"])["tags"] == "" for c in db.quiz_cards(reply, 0))
    # The first time the book's contents are known, the cards made before get tagged.
    assert srs.learn_toc(db, book_id, TOC) == 4 and srs.learn_toc(db, book_id, TOC) == 0
    tags = [db.card_note(c["note_id"])["tags"] for c in db.quiz_cards(reply, 0)]
    assert tags[0] == " A::Part_I::Chapter_1:_Rinsing_mostly " and tags[1] == " A::Part_I::Chapter_2::2.1_Storage "

    later = db.add_message(chat, "assistant", CARDS)
    srs.add_quiz_cards(db, book_id, later, CARDS)
    assert db.card_note(db.card_for(later, 0, 0)["note_id"])["tags"] == " A::Part_I::Chapter_1:_Rinsing_mostly "
    assert db.card_note(db.card_for(later, 0, 1)["note_id"])["tags"] == " A::Part_I::Chapter_2::2.1_Storage "

    # Reviewing one section (or the part holding it).
    assert srs.counts(db, book_id, NOW, tag="A::Part_I::Chapter_2")["total"] == 4
    assert srs.counts(db, None, NOW, tag="A::Part_I")["total"] == 6
    assert srs.counts(db, book_id, NOW, tag="A::Part_I::Chapter_")["total"] == 0  # whole parts only
    card = srs.next_card(db, book_id, NOW, tag="A::Part_I::Chapter_1:_Rinsing_mostly")
    assert card["id"] == db.card_for(reply, 0, 0)["id"]

    tree = srs.sections(db, book_id, NOW)[book_id]
    assert [(n["tag"], n["parent"], n["total"]) for n in tree] == [
        ("A::Part_I", None, 6), ("A::Part_I::Chapter_1:_Rinsing_mostly", "A::Part_I", 2),
        ("A::Part_I::Chapter_2", "A::Part_I", 4), ("A::Part_I::Chapter_2::2.1_Storage", "A::Part_I::Chapter_2", 4),
    ]
    assert tree[2]["name"] == "Chapter 2" and tree[0]["new"] == 6


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


def test_reviewing_one_book_or_section(chat_panel):
    from study_reader.review import ReviewPanel

    panel, worker, db, book_id = chat_panel
    srs.learn_toc(db, book_id, TOC)
    panel.start_quiz("Quiz me on pages 1-4.", "Quiz", 0, last=3)  # spans chapters 1 and 2
    worker.done.emit(panel.chat_id, "s1", QUIZ, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    # A question with no page of its own goes into the section holding all the quizzed pages.
    tags = [db.card_note(c["note_id"])["tags"] for c in db.quiz_cards(reply, 0)]
    assert tags[0] == " A::Part_I::Chapter_1:_Rinsing_mostly " and set(tags[1:]) == {" A::Part_I "}
    assert db.quiz_cards(reply, 0)[1]["page"] == 0
    other = db.upsert_book("h2", "/b.pdf", "B", 3)["id"]
    db.add_card_note("basic", {"front": "q", "back": "a"}, db.book_deck(other), book_id=other)

    review = ReviewPanel(db, worker)
    review.set_book(book_id)
    shown = []
    review._js = lambda fn, *args: shown.append((fn, args))
    review.refresh()
    o = shown[-1][1][0]
    assert [s["name"] for s in o["sections"]] == ["Part I", "Chapter 1: Rinsing mostly"] and o["section"] is None
    review.choose_section(0, "A::Part_I::Chapter_1:_Rinsing_mostly")
    o = shown[-1][1][0]
    assert o["total"] == 1 and o["title"] == "Part I › Chapter 1: Rinsing mostly" and o["whole_new"] == 4
    review.start()
    assert review.card_id == db.card_for(reply, 0, 0)["id"]
    review.set_book(book_id)
    assert review.section is None

    everything = ReviewPanel(db, worker, all_books=True)
    everything._js = lambda fn, *args: shown.append((fn, args))
    everything.refresh()
    o = shown[-1][1][0]
    assert [b["title"] for b in o["books"]] == ["A", "B"] and o["total"] == 5
    assert o["decks"] == []  # books' own decks are listed as books
    assert o["books"][0]["sections"][0]["tag"] == "A::Part_I"
    everything.choose_book(other)
    assert shown[-1][1][0]["total"] == 1 and shown[-1][1][0]["title"] == "B"
    everything.choose_section(book_id, "A::Part_I")
    o = shown[-1][1][0]
    assert o["total"] == 4 and o["book"] == book_id and o["title"] == "A › Part I"
    everything.choose_deck(None)
    assert shown[-1][1][0]["total"] == 5 and everything.picked_book is None


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


# -- Claude's tags and revisions -----------------------------------------------------------
TAGGED = r'''```quiz Zorblatts
{"questions": [
  {"type": "truefalse", "question": "Store it near a window?", "answer": false, "page": 1, "tags": ["Storage Rules", "care::light"]},
  {"type": "free", "question": "Why rinse?", "answer": "To calibrate.", "tags": "rinsing"}
]}
```'''

REVISED = r'''Here it is, harder.
```quiz Zorblatts, harder
{"revises": "Zorblatts", "questions": [
  {"type": "free", "question": "Why rinse, and why in tea?", "answer": "To calibrate; tea is neutral.", "revises": 4},
  {"type": "flashcard", "question": "Rinse time?", "answer": "41 s", "revises": 1},
  {"type": "truefalse", "question": "Zorblatts have seven legs.", "answer": true}
]}
```'''


def test_claude_tags_its_quiz_questions(tmp_path):
    db, book_id, chat, reply = setup(tmp_path, TAGGED)
    srs.learn_toc(db, book_id, TOC)
    (q,) = quiz.quizzes(TAGGED)
    assert q["questions"][0]["tags"] == ["Storage_Rules", "care::light"] and q["questions"][1]["tags"] == ["rinsing"]
    srs.add_quiz_cards(db, book_id, reply, TAGGED)
    first, second = db.quiz_cards(reply, 0)
    assert db.card_note(first["note_id"])["tags"] == " Storage_Rules care::light A::Part_I::Chapter_1:_Rinsing_mostly "
    assert "tags" not in json.loads(first["data"])
    # Claude is shown the user's tags (not section tags) to reuse.
    assert srs.concept_tags(db) == ["care::light", "rinsing", "Storage_Rules"]
    assert "<existing_tags>care::light rinsing Storage_Rules</existing_tags>" in srs.tags_context(db)


def test_a_revised_quiz_takes_over_the_cards_it_keeps(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    old = db.quiz_cards(reply, 0)
    srs.review(db, old[0]["id"], 3, now=NOW)
    srs.review(db, old[1]["id"], 3, now=NOW)  # reviewed, and left out of the revision
    db.add_message(chat, "user", "Revise your quiz “Zorblatts”: harder")
    revision = db.add_message(chat, "assistant", REVISED)
    srs.add_quiz_cards(db, book_id, revision, REVISED, now=NOW + 60)

    free, card, new = db.quiz_cards(revision, 0)
    assert free["id"] == old[3]["id"] and json.loads(free["data"])["question"] == "Why rinse, and why in tea?"
    assert card["id"] == old[0]["id"] and card["reps"] == 1 and len(db.reviews(card["id"])) == 1
    assert db.card_note(card["note_id"])["kind"] == "basic" and card["title"] == "Zorblatts, harder"
    assert new["reps"] == 0 and new["id"] not in [c["id"] for c in old]
    # The old quiz keeps only its reviewed card that wasn't carried over.
    assert [c["id"] for c in db.quiz_cards(reply, 0)] == [old[1]["id"]] and db.card(old[2]["id"]) is None
    assert quiz.revised_quiz([(reply, 0, quiz.quizzes(QUIZ)[0])], "nothing like it") == (reply, 0)


def test_chat_panel_revises_quizzes_and_shares_tags(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me.", "Quiz", 0)
    worker.done.emit(panel.chat_id, "s1", TAGGED, "u1")
    reply = db.messages(panel.chat_id)[1]["id"]
    first = db.quiz_cards(reply, 0)[0]["id"]
    panel.revise_quiz(reply, 0, "make it harder")
    job = worker.jobs[-1]
    assert job.question == "Revise your quiz “Zorblatts”: make it harder" and "<existing_tags>" in job.context
    worker.done.emit(panel.chat_id, "s1", REVISED, "u2")
    revision = db.messages(panel.chat_id)[-1]["id"]
    assert panel._revisions(db.messages(panel.chat_id)) == {(reply, 0): revision}
    assert db.quiz_cards(revision, 0)[1]["id"] == first and not db.quiz_cards(reply, 0)


def test_browser_sidebar_orders_and_filters_tags(chat_panel):
    from study_reader.browser import Sidebar

    _, _, db, book_id = chat_panel
    deck = db.book_deck(book_id)
    for tag, page in (("A::Ch_10::Late_part", 5), ("A::Ch_2", 1), ("zebra", None), ("apple", None)):
        db.add_card_note("basic", {"front": tag, "back": "x"}, deck, tags=[tag], book_id=book_id, page=page)
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
