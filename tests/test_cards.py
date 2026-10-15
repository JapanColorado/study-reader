import json

import pytest

from study_reader import cardmodel, convert, srs
from study_reader.db import Database

from test_srs import NOW, setup
from test_quiz import QUIZ

CLOZE = r"In {{c1::equilibrium::where}}, \(\rho = {{c2::0}}\) and {{c1::<b>surface</b>}}"


# -- notes and their cards -----------------------------------------------------------------
def test_cloze_cards_and_sides():
    fields = {"text": CLOZE, "extra": "Gauss"}
    assert cardmodel.card_ords("cloze", fields) == [0, 1]
    one = cardmodel.card_data("cloze", fields, 0)
    assert one["type"] == "cloze" and one["html"] and one["explanation"] == "Gauss"
    assert one["question"] == r'In <span class="cloze">[where]</span>, \(\rho = 0\) and <span class="cloze">[…]</span>'
    two = cardmodel.card_data("cloze", fields, 1)
    assert r"\htmlClass{cloze}{\text{[\dots]}}" in two["question"] and "<b>surface</b>" in two["question"]
    assert r"\htmlClass{cloze}{0}" in two["answer"]
    assert cardmodel.answer_text(one) == "equilibrium … surface"
    assert cardmodel.plain(CLOZE) == r"In equilibrium, \(\rho = 0\) and surface"
    assert cardmodel.renumber_next(CLOZE) == 3
    with pytest.raises(cardmodel.NoteError):
        cardmodel.check("cloze", {"text": "no deletions"})


def test_cloze_math_holding_closing_braces():
    text = r"\(\omega_0\) = {{c1::\(\frac{1}{\sqrt{LC}}\)}}, called {{c2::resonance::what}}"
    assert cardmodel.card_ords("cloze", {"text": text}) == [0, 1]
    assert cardmodel.cloze_side(text, 1, True) == (
        r'\(\omega_0\) = <span class="cloze">\(\frac{1}{\sqrt{LC}}\)</span>, called resonance')
    assert cardmodel.cloze_side(text, 2, False).endswith(r'\(\frac{1}{\sqrt{LC}}\), called <span class="cloze">[what]</span>')


def test_reverse_occlusion_and_tags():
    assert cardmodel.card_ords("reverse", {"front": "a", "back": "b"}) == [0, 1]
    back = cardmodel.card_data("reverse", {"front": "a", "back": "b"}, 1)
    assert (back["question"], back["answer"]) == ("b", "a")
    shapes = cardmodel.parse_occlusion(
        "{{c1::image-occlusion:rect:left=.1:top=.2:width=.3:height=.1:oi=1}}<br>"
        "{{c2::image-occlusion:polygon:points=0.1,0.2 0.3,0.4 0.5,0.1}}")
    assert shapes[0] == {"ord": 1, "shape": "rect", "left": 0.1, "top": 0.2, "width": 0.3, "height": 0.1, "oi": True}
    assert shapes[1]["points"] == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.1]]
    assert cardmodel.card_ords("occlusion", {"image": "a.png", "shapes": shapes}) == [0, 1]
    assert cardmodel.split_tags("a, b  a  c::d") == ["a", "b", "c::d"] and cardmodel.join_tags(["a", "b"]) == " a b "
    assert cardmodel.media_files("basic", {"front": '<img src="x.png"> ![](y.jpg)', "back": "![](http://e/z.png)"}) \
        == ["x.png", "y.jpg"]


def test_notes_decks_and_editing(db):
    deck = db.ensure_deck(" Physics :: Lesson 1 ")
    assert [d["name"] for d in db.decks()] == ["Physics", "Physics::Lesson 1"]
    note = db.add_card_note("cloze", {"text": CLOZE, "extra": ""}, deck, tags=["exam", "ch1"])
    cards = db.note_cards(note)
    assert [c["ord"] for c in cards] == [0, 1] and all(c["deck_id"] == deck for c in cards)
    assert db.card_note(note)["tags"] == " exam ch1 " and "equilibrium" in db.card_note(note)["search_text"]

    # A new deletion makes a card; one taken out loses its card.
    db.update_card_note(note, {"text": "{{c1::a}} {{c3::b}}", "extra": ""})
    assert [c["ord"] for c in db.note_cards(note)] == [0, 2]
    assert db.note_cards(note)[0]["id"] == cards[0]["id"]

    db.add_tags([note], ["new"])
    db.remove_tags([note], ["CH1"])
    assert db.card_note(note)["tags"].split() == ["exam", "new"]
    db.rename_tag("exam", "test::final")
    assert db.all_tags() == ["new", "test::final"]

    # Renaming a deck moves its subdecks; onto an existing name, it merges.
    other = db.ensure_deck("Maths")
    db.rename_deck(db.deck_by_name("Physics")["id"], "Science::Physics")
    assert db.deck(deck)["name"] == "Science::Physics::Lesson 1"
    db.rename_deck(other, "Science::Physics::Lesson 1")
    assert db.deck(other) is None and db.deck_by_name("Science::Physics::Lesson 1")["id"] == deck
    with pytest.raises(ValueError):
        db.rename_deck(db.deck_by_name("Science")["id"], "Science::Inner")
    assert db.delete_deck(db.deck_by_name("Science")["id"]) == 2
    assert db.card_note(note) is None and [d["name"] for d in db.decks()] == []


def test_quiz_cards_go_into_their_books_deck(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    deck = db.book(book_id)["deck_id"]
    assert db.deck(deck)["name"] == "A" and {db.card(i)["deck_id"] for i in ids} == {deck}
    note = db.card_note(db.card(ids[0])["note_id"])
    assert note["kind"] == "quiz" and json.loads(note["fields"]) == json.loads(db.card(ids[0])["data"])
    # A second book with the same title gets its own deck.
    other = db.upsert_book("h2", "/b.pdf", "A", 3)["id"]
    with db.conn:
        assert db.deck(db.book_deck(other))["name"] == "A (2)"


def test_old_cards_become_notes_in_book_decks(tmp_path):
    """A database from before decks: its cards table is rebuilt; reviews survive; a flashcard
    becomes a Basic note."""
    path = tmp_path / "old.db"
    db = Database(path)
    book = db.upsert_book("h", "/a.pdf", "Old book", 3)["id"]
    db.conn.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TABLE cards; DROP TABLE card_notes; DROP TABLE decks;
        CREATE TABLE cards (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL, quiz INTEGER NOT NULL, question INTEGER NOT NULL,
            title TEXT NOT NULL, data TEXT NOT NULL, page INTEGER, stage INTEGER NOT NULL DEFAULT 0,
            fsrs TEXT NOT NULL DEFAULT '', due REAL NOT NULL, reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            UNIQUE (message_id, quiz, question));
    """)
    fsrs_json = json.dumps({"card_id": 1, "state": 2, "step": None, "stability": 9.5, "difficulty": 3.0,
                            "due": "2027-01-01T00:00:00+00:00", "last_review": "2026-12-20T00:00:00+00:00"})
    db.conn.execute("INSERT INTO cards VALUES (7, ?, NULL, 0, 0, 'Q', ?, 1, 2, ?, 1.8e9, 3, 0, 0, 1.7e9)",
                    (book, json.dumps({"type": "flashcard", "question": "F", "answer": "B"}), fsrs_json))
    db.conn.execute("INSERT INTO reviews (card_id, rating, source, reviewed_at) VALUES (7, 3, 'review', 1.75e9)")
    db.conn.execute("ALTER TABLE books DROP COLUMN deck_id")
    db.conn.commit()
    db.conn.close()

    db = Database(path)
    card = db.card(7)
    assert card["note_id"] and db.deck(card["deck_id"])["name"] == "Old book" and card["reps"] == 3
    assert (card["stability"], card["difficulty"]) == (9.5, 3.0) and card["last_review"] > 1.7e9
    assert len(db.reviews(7)) == 1 and db.card_note(card["note_id"])["kind"] == "basic"
    assert json.loads(card["data"])["type"] == "flashcard" and json.loads(card["data"])["question"] == "F"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.delete_card(7)  # the review log follows its card again
    assert db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


# -- scheduling ----------------------------------------------------------------------------
def test_parameters_retention_forget_and_due_dates(db):
    deck = db.ensure_deck("D")
    note = db.add_card_note("basic", {"front": "f", "back": "b"}, deck, now=NOW)
    (card,) = db.note_cards(note)
    assert srs.desired_retention(db) == 0.9 and len(srs.parameters(db)) == 21
    db.set_setting(srs.RETENTION_SETTING, "0.8")
    srs.set_parameters(db, list(srs.parameters(None))[:19])  # FSRS-5's 19 weights are extended
    assert srs.parameters(db)[19:] == (0.0, 0.5) and srs.scheduler(db).desired_retention == 0.8
    with pytest.raises(ValueError):
        srs.set_parameters(db, [1.0] * 7)

    srs.review(db, card["id"], 3, now=NOW, duration=4200)
    srs.review(db, card["id"], 3, now=db.card(card["id"])["due"])
    reviewed = db.card(card["id"])
    assert reviewed["stage"] == 2 and reviewed["stability"] and reviewed["last_review"]
    assert db.reviews(card["id"])[0]["duration"] == 4200 and srs.retrievability(db, reviewed, reviewed["due"]) > 0.5

    assert srs.set_due(db, [card["id"]], 3, now=NOW) == 1
    assert srs.day_start(NOW) + 3 * 86400 <= db.card(card["id"])["due"] < srs.next_day(NOW) + 3 * 86400
    srs.forget(db, [card["id"]], now=NOW)
    forgotten = db.card(card["id"])
    assert (forgotten["stage"], forgotten["reps"], forgotten["fsrs"]) == (0, 0, "") and len(db.reviews(card["id"])) == 2
    assert srs.set_due(db, [card["id"]], 1, now=NOW) == 0  # new cards stay new


def test_deck_counts_and_deck_scoped_queue(db):
    a, b = db.ensure_deck("Top::A"), db.ensure_deck("Top::B")
    db.add_card_note("basic", {"front": "1", "back": "x"}, a, now=NOW)
    db.add_card_note("cloze", {"text": "{{c1::x}} {{c2::y}}"}, b, now=NOW)
    counts = srs.deck_counts(db, NOW)
    top = db.deck_by_name("Top")["id"]
    assert counts[top]["new"] == 3 and counts[a]["new"] == 1 and counts[b]["total"] == 2
    assert srs.counts(db, None, NOW, deck=b)["new"] == 2
    card = srs.next_card(db, None, NOW, deck=a)
    assert card["deck_id"] == a


# -- Claude Conversion ---------------------------------------------------------------------
def test_conversion_proposals_keep_the_schedule(db):
    deck = db.ensure_deck("Physics")
    cloze = db.add_card_note("cloze", {"text": "{{c1::Gauss}} found {{c2::flux}} laws", "extra": ""}, deck, tags=["em"])
    first, second = db.note_cards(cloze)
    srs.review(db, first["id"], 3, now=NOW)
    basic = db.add_card_note("basic", {"front": "What is 2+2?", "back": "4"}, deck)
    (plain,) = db.note_cards(basic)

    payload = convert.card_payload(db, db.card(first["id"]))
    assert payload["front"] == "[…] found flux laws" and payload["back"] == "[Gauss] found flux laws"
    assert payload["deck"] == "Physics" and payload["tags"] == ["em"]
    assert convert.batches(db, [first["id"], plain["id"]]) == [[first["id"], plain["id"]]]
    blocks = convert.blocks(db, [first["id"]], "prefer free responses")
    assert "learner_instructions" in blocks[1]["text"] and '"id": %d' % first["id"] in blocks[-1]["text"]

    ids = convert.start(db, [first["id"], second["id"], plain["id"]])
    reply = json.dumps({"cards": [
        {"id": first["id"], "action": "convert", "reason": "conceptual",
         "question": {"type": "free", "question": "Who found the law of flux, and what does it relate?",
                      "answer": "Gauss; flux to enclosed charge.", "rubric": "Gauss; enclosed charge"}},
        {"id": plain["id"], "action": "convert", "question": {"type": "numeric", "question": "2+2?", "answer": "4"}},
    ]})
    convert.record(db, ids, "Here you go:\n" + reply)
    assert db.conversion(ids[first["id"]])["status"] == "proposed"
    assert db.conversion(ids[second["id"]])["status"] == "error"  # left out of the reply
    assert convert.accept(db, ids[first["id"]])
    card = db.card(first["id"])
    assert json.loads(card["data"])["type"] == "free" and card["reps"] == 1 and len(db.reviews(card["id"])) == 1
    assert db.card_note(card["note_id"])["tags"] == " em " and card["deck_id"] == deck
    assert db.card(second["id"])["note_id"] == cloze  # the other deletion stays on the cloze note

    assert convert.revert(db, convert.accepted_conversion(db, first["id"])["id"])
    back = db.card(first["id"])
    assert back["note_id"] == cloze and back["ord"] == 0 and json.loads(back["data"])["type"] == "cloze"

    with pytest.raises(convert.ConvertError):
        convert.parse("no json here", [1])
    bad = convert.parse('{"cards": [{"id": 1, "action": "convert", "question": {"type": "choice", "question": "q"}}]}', [1])
    assert bad[1]["action"] == "error"


def test_conversion_manager(db, chat_panel):
    from study_reader.convert_dialog import ConversionManager

    _, worker, _, _ = chat_panel
    deck = db.ensure_deck("D")
    ids = [c["id"] for n in range(3) for c in db.note_cards(db.add_card_note("basic", {"front": f"q{n}", "back": "a"}, deck))]
    manager = ConversionManager(db, worker)
    changed = []
    manager.changed.connect(lambda: changed.append(1))
    assert manager.start(ids, "be brief") == 1 and manager.busy()
    batch_id, blocks, model, system = worker.conversions[-1]
    assert model == "sonnet" and system == convert.SYSTEM_PROMPT
    worker.converted.emit(batch_id, json.dumps({"cards": [
        {"id": ids[0], "action": "keep"},
        {"id": ids[1], "action": "convert", "question": {"type": "truefalse", "question": "q1 is true", "answer": True}},
        {"id": ids[2], "action": "convert", "question": {"type": "flashcard", "question": "q2?", "answer": "a"}},
    ]}))
    assert not manager.busy() and changed
    assert [c["status"] for c in db.conversions()] == ["kept", "proposed", "proposed"]
    assert manager.accept_all() == 2 and json.loads(db.card(ids[1])["data"])["type"] == "truefalse"
    assert db.card_note(db.card(ids[2])["note_id"])["kind"] == "basic"  # a flashcard stays a Basic note
    manager.start([ids[0]])
    worker.convertFailed.emit(worker.conversions[-1][0], "rate limited")
    assert db.conversions(("error",))[0]["reason"] == "rate limited"


def test_conversion_never_makes_needless_flashcards(db):
    deck = db.ensure_deck("D")
    free = db.add_card_note("quiz", {"type": "free", "question": "Why?", "answer": "Because.", "rubric": ""}, deck)
    basic = db.add_card_note("basic", {"front": "What is $x$?", "back": "A variable"}, deck)
    other = db.add_card_note("basic", {"front": "Vague", "back": "Too much"}, deck)
    (q,), (b,), (o,) = db.note_cards(free), db.note_cards(basic), db.note_cards(other)
    ids = convert.start(db, [q["id"], b["id"], o["id"]])
    convert.record(db, ids, json.dumps({"cards": [
        {"id": q["id"], "action": "convert", "reason": "recall", "question":
            {"type": "flashcard", "question": "Why?", "answer": "Because."}},
        {"id": b["id"], "action": "convert", "question": {"type": "flashcard", "question": "What is $x$?",
                                                         "answer": "A  variable"}},
        {"id": o["id"], "action": "convert", "question": {"type": "flashcard", "question": "Sharp?", "answer": "Short"}},
    ]}))
    assert [db.conversion(ids[i])["status"] for i in (q["id"], b["id"], o["id"])] == ["kept", "kept", "proposed"]
    assert "quiz questions stay" in db.conversion(ids[q["id"]])["reason"]
    assert convert.accept(db, ids[o["id"]])
    note = db.card_note(db.card(o["id"])["note_id"])
    assert note["kind"] == "basic" and json.loads(note["fields"])["front"] == "Sharp?"
    with pytest.raises(cardmodel.NoteError):
        db.add_card_note("quiz", {"type": "flashcard", "question": "F", "answer": "B"}, deck)


def test_flashcards_in_quizzes_are_basic_notes(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    chat = db.create_chat(book, "Cards")
    reply = db.add_message(chat, "assistant", '```quiz Cards\n[{"type": "flashcard", "question": "F", '
                           '"answer": "B", "explanation": "E"}]\n```')
    (card_id,) = srs.add_quiz_cards(db, book, reply, db.message(reply)["text"], now=NOW)
    note = db.card_note(db.card(card_id)["note_id"])
    assert note["kind"] == "basic" and json.loads(note["fields"]) == {"front": "F", "back": "B", "extra": "E"}
    assert srs.question(db.card(card_id))["type"] == "flashcard" and db.card_for(reply, 0, 0)["id"] == card_id


# -- review of cards from no book ----------------------------------------------------------
def test_reviewing_cloze_and_bookless_free_cards(db, chat_panel):
    from study_reader.review import ReviewPanel

    _, worker, _, _ = chat_panel
    deck = db.ensure_deck("Anki::Physics")
    db.add_card_note("cloze", {"text": "{{c1::Gauss}}'s law"}, deck, now=NOW - 10)
    db.add_card_note("quiz", {"type": "free", "question": "Why?", "answer": "Because.", "explanation": ""}, deck, now=NOW)
    db.ensure_deck("Elsewhere")
    review = ReviewPanel(db, worker, all_books=True)
    shown = []
    review._js = lambda fn, *args: shown.append((fn, args))
    review.choose_deck(db.deck_by_name("Anki")["id"])
    overview = shown[-1][1][0]
    assert overview["title"] == "Anki" and [d["path"] for d in overview["decks"]] == ["Anki", "Anki::Physics"]

    review.start()
    card = shown[-1][1][0]
    assert card["question"]["type"] == "cloze" and card["deck"] == "Anki::Physics" and card["page"] is None
    review.check(review.card_id, {})  # revealed: self-rated, nothing graded
    assert shown[-1][1][0]["previews"] and shown[-1][1][0]["answer"]["verdict"] is None
    review.rate(review.card_id, 3)

    free = review.card_id
    review.grade_with_claude(free, "Because I said so.")
    job, card_id = worker.card_gradings[-1]
    assert card_id == free and job.book is None and job.chat_session_id is None
    assert 'deck "Anki::Physics"' in job.question
    worker.cardGraded.emit(free, '{"verdict": "correct", "feedback": "Yes."}')
    review.rate(free, 3)
    assert db.reviews(free)[0]["verdict"] == "correct"


# -- the browser ---------------------------------------------------------------------------
def test_browser_search_edit_and_bulk_actions(db, chat_panel, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from study_reader.browser import CardBrowser, CardInfoDialog, find_replace

    _, worker, _, _ = chat_panel
    physics, maths = db.ensure_deck("Physics"), db.ensure_deck("Maths")
    gauss = db.add_card_note("basic", {"front": "Gauss's law relates?", "back": "flux and charge"}, physics, tags=["em"])
    db.add_card_note("cloze", {"text": "{{c1::Stokes}} and {{c2::Green}}"}, maths)
    browser = CardBrowser(db)
    changes = []
    browser.cardsChanged.connect(lambda: changes.append(1))

    browser.set_search("deck:Physics")
    assert len(browser.model.ids) == 1 and browser.model.data(browser.model.index(0, 0)) == "Gauss's law relates?"
    browser.set_search("")
    assert len(browser.model.ids) == 3
    browser.set_search("deck:Physics (")
    assert "expected" in browser.status.text() and len(browser.model.ids) == 3  # the last good search stays

    browser.set_search("deck:Physics")
    browser.table.selectRow(0)
    assert browser.editor.note_id == gauss and browser.editor.fields["front"].toPlainText() == "Gauss's law relates?"
    browser.editor.fields["back"].setPlainText("electric flux and enclosed charge")
    browser.save_note()
    assert json.loads(db.card_note(gauss)["fields"])["back"] == "electric flux and enclosed charge" and changes

    card_id = browser.model.ids[0]
    browser.toggle_suspend()
    assert db.card(card_id)["suspended"] == 1
    browser.set_flag(2)
    assert db.card(card_id)["flag"] == 2
    browser.set_flag(2)
    assert db.card(card_id)["flag"] == 0
    monkeypatch.setattr(browser, "_ask_tags", lambda *a: ["exam"])
    browser.add_tags()
    assert db.card_note(gauss)["tags"].split() == ["em", "exam"]
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Science::Physics", True))
    browser.rename_deck(physics)
    assert db.deck(physics)["name"] == "Science::Physics"
    browser.set_search("tag:exam")
    browser.table.selectAll()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    srs.review(db, card_id, 3)
    browser.forget()
    assert db.card(card_id)["stage"] == 0
    CardInfoDialog(db, card_id).close()

    assert find_replace(db, [gauss], "FLUX", "field") == (1, 0)
    assert json.loads(db.card_note(gauss)["fields"])["back"] == "electric field and enclosed charge"
    browser.set_search("Stokes")
    browser.table.selectAll()
    browser.delete()
    assert db.count_cards(deck_id=maths) == 0
    browser.sidebar.refresh()
    browser.close()


def test_add_dialog_makes_cards(db, chat_panel):
    from study_reader.card_editor import AddDialog

    dialog = AddDialog(db)
    added = []
    dialog.added.connect(added.append)
    dialog.deck.setCurrentText("New::Deck")
    dialog.kind.setCurrentIndex(dialog.kind.findData("cloze"))
    dialog.editor.fields["text"].setPlainText("The {{c1::mitochondria}} is the {{c2::powerhouse}}")
    dialog.editor.tags.setText("bio")
    dialog.add()
    assert len(db.note_cards(added[0])) == 2 and db.deck_by_name("New::Deck")
    assert dialog.editor.fields["text"].toPlainText() == "" and dialog.editor.tags.text() == "bio"

    dialog.kind.setCurrentIndex(dialog.kind.findData("quiz"))
    dialog.editor.q_type.setCurrentIndex(dialog.editor.q_type.findData("choice"))
    dialog.editor.q_question.setPlainText("Pick B")
    dialog.editor.q_choices.setPlainText("A\nB\nC")
    dialog.editor.q_correct.setValue(2)
    dialog.add()
    q = json.loads(db.card_note(added[1])["fields"])
    assert q["type"] == "choice" and q["answer"] == 1 and q["choices"] == ["A", "B", "C"]

    dialog.add()
    assert dialog.status.text() == "A question has no text."
    dialog.editor.q_question.setPlainText("Pick one")
    dialog.editor.q_choices.setPlainText("only one")
    dialog.add()
    assert "choices" in dialog.status.text() and len(added) == 2
    dialog.close()


def test_anki_dialog_migrates_picked_decks(db, chat_panel, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from study_reader import anki_import
    from study_reader.anki_dialog import AnkiImportDialog
    from test_anki_import import make_collection

    path = make_collection(tmp_path)
    monkeypatch.setattr(anki_import, "find_collections", lambda: [("User 1", path)])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dialog = AnkiImportDialog(db)
    dialog.check_decks(["Physics"])
    assert "4 decks" not in dialog.picked.text() and dialog.migrate_button.isEnabled()
    dialog.migrate()
    # It closes once done (it's modal, so the browser it leads to couldn't be used otherwise).
    assert dialog.result() == dialog.DialogCode.Accepted and dialog.collection is None
    assert dialog.report.cards >= 6 and dialog.query == 'deck:"Physics"'
    assert db.deck_by_name("Physics::Lesson 1") and not db.deck_by_name("Japanese")


def test_main_window_cards_menu(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    deck = db.ensure_deck("Physics::Optics")
    db.add_card_note("basic", {"front": "f", "back": "b"}, deck)
    win = MainWindow(db)
    win.show()
    assert win.library.cards.isVisible() and win.library.review.text() == "Review · 1"
    browser = win.show_browser()
    assert browser is win.show_browser() and len(browser.model.ids) == 1
    browser.reviewDeckRequested.emit(deck)
    assert win.center.currentWidget() is win.review_all and win.review_all.deck_id == deck
    win.review_all.start()
    win.review_all.act(win.review_all.card_id, "edit")
    assert browser.search_box.text().startswith("cid:")
    win.show_stats(deck)
    assert win.stats_window.isVisible()
    win.stats_window.close()
    browser.close()
    win.close()


# -- regressions ---------------------------------------------------------------------------
def test_edits_never_lose_reviewed_cards_or_revive_converted_ones(db):
    deck = db.ensure_deck("D")
    note = db.add_card_note("cloze", {"text": "{{c1::a}} {{c2::b}} {{c3::c}}"}, deck, now=NOW)
    c1, c2, c3 = db.note_cards(note)
    srs.review(db, c2["id"], 3, now=NOW)
    # Retyping c2 and c3 (an autosave mid-edit has neither): the reviewed card stays, the new one goes.
    db.update_card_note(note, {"text": "{{c1::a}} b c"})
    assert {c["id"] for c in db.note_cards(note)} == {c1["id"], c2["id"]}
    db.update_card_note(note, {"text": "{{c1::a}} {{c2::B}} {{c3::c}}"})
    cards = {c["ord"]: c for c in db.note_cards(note)}
    assert cards[1]["id"] == c2["id"] and "B" in cards[1]["data"] and len(db.reviews(c2["id"])) == 1 and 2 in cards

    # A converted card isn't brought back by editing its old note, and undo puts it back there.
    (conversion,) = convert.start(db, [c1["id"]]).values()
    convert.record(db, {c1["id"]: conversion}, json.dumps({"cards": [{"id": c1["id"], "action": "convert",
                   "question": {"type": "flashcard", "question": "a?", "answer": "a"}}]}))
    convert.accept(db, conversion)
    db.update_card_note(note, {"text": "{{c1::a}} {{c2::B}} {{c3::c}} fixed typo"})
    assert 0 not in {c["ord"] for c in db.note_cards(note)}
    assert convert.revert(db, conversion) and db.card(c1["id"])["note_id"] == note


def test_forgotten_cards_count_as_new_and_outlive_their_chat(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = srs.add_quiz_cards(db, book_id, reply, QUIZ, now=NOW)
    db.set_setting(srs.NEW_PER_DAY_SETTING, "1")
    srs.review(db, ids[0], 3, now=NOW - 3 * 86400)
    srs.forget(db, [ids[0]], now=NOW)
    srs.review(db, ids[0], 3, now=NOW)  # introduced again today: uses up the allowance
    assert srs.new_left_today(db, NOW) == 0
    db.delete_messages_from(chat, reply)
    assert [c["id"] for c in db.cards()] == [ids[0]]


def test_removing_a_tag_removes_its_children(db):
    note = db.add_card_note("basic", {"front": "f"}, db.ensure_deck("D"), tags=["A", "A::B", "C"])
    db.rename_tag("A", "")
    assert db.card_note(note)["tags"] == " C "


def test_browser_editor_follows_accepted_conversions(db, chat_panel):
    from study_reader.browser import CardBrowser
    from study_reader.convert_dialog import ConversionManager

    _, worker, _, _ = chat_panel
    note = db.add_card_note("basic", {"front": "2+2?", "back": "4"}, db.ensure_deck("D"))
    (card,) = db.note_cards(note)
    manager = ConversionManager(db, worker)
    browser = CardBrowser(db, manager)
    browser.show()
    browser.set_search("")
    browser.table.selectRow(0)
    manager.start([card["id"]])
    worker.converted.emit(worker.conversions[-1][0], json.dumps({"cards": [{"id": card["id"], "action": "convert",
                          "question": {"type": "numeric", "question": "2+2?", "answer": 4}}]}))
    browser.editor.fields["back"].setPlainText("four")  # an edit pending when the proposal is accepted is saved first
    browser._save_timer.start()
    saved = []
    manager.aboutToChange.connect(lambda: saved.append(json.loads(db.card_note(note)["fields"])["back"]))
    manager.accept_all()
    assert saved == ["four"]
    assert browser.editor.note_id == db.card(card["id"])["note_id"] and browser.editor.kind == "quiz"
    browser.close()


def test_clicking_a_row_shows_that_card(db, chat_panel):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from study_reader.browser import CardBrowser

    deck = db.ensure_deck("D")
    for i in range(4):
        db.add_card_note("basic", {"front": f"front {i}", "back": "b"}, deck)
    browser = CardBrowser(db)
    browser.show()
    browser.set_search("")
    for row in (2, 0, 3):
        rect = browser.table.visualRect(browser.model.index(row, 0))
        QTest.mouseClick(browser.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        assert browser.editor.fields["front"].toPlainText() == browser.model.data(browser.model.index(row, 0))
    # Ctrl+click adds to the selection and shows the card clicked.
    rect = browser.table.visualRect(browser.model.index(1, 0))
    QTest.mouseClick(browser.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, rect.center())
    assert len(browser.selected_ids()) == 2 and browser.editor.fields["front"].toPlainText() == browser.model.data(browser.model.index(1, 0))
    browser.close()


def test_stopping_conversions(db, chat_panel):
    from study_reader.convert_dialog import ConversionManager

    _, worker, _, _ = chat_panel
    deck = db.ensure_deck("D")
    ids = [c["id"] for n in range(2) for c in db.note_cards(db.add_card_note("basic", {"front": f"q{n}", "back": "a"}, deck))]
    manager = ConversionManager(db, worker)
    manager.start(ids)
    batch_id = worker.conversions[-1][0]
    manager.stop()
    assert worker.conversions_stopped and not manager.busy() and db.conversions() == []
    worker.converted.emit(batch_id, json.dumps({"cards": [{"id": i, "action": "keep"} for i in ids]}))
    assert db.conversions() == []  # a reply that arrives anyway is ignored
