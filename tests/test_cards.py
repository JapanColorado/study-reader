import json

import pytest

from study_reader import cardmodel, search, srs, variants
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


def test_math_spans_hold_escaped_dollars():
    text = r"Pizza ($q_1$) at $p_1 = \$1$, income $Y = \$50$; it costs \$5 or $5 and $6."
    assert [text[a:b] for a, b in cardmodel.math_spans(text)] == ["$q_1$", r"$p_1 = \$1$", r"$Y = \$50$"]


def test_reverse_occlusion_and_tags():
    assert cardmodel.card_ords("reversed", {"front": "a", "back": "b"}) == [0, 1]
    back = cardmodel.card_data("reversed", {"front": "a", "back": "b"}, 1)
    assert (back["question"], back["answer"]) == ("b", "a")
    shapes = cardmodel.parse_occlusion(
        "{{c1::image-occlusion:rect:left=.1:top=.2:width=.3:height=.1:oi=1}}<br>"
        "{{c2::image-occlusion:polygon:points=0.1,0.2 0.3,0.4 0.5,0.1}}")
    assert shapes[0] == {"ord": 1, "shape": "rect", "left": 0.1, "top": 0.2, "width": 0.3, "height": 0.1, "oi": True}
    assert shapes[1]["points"] == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.1]]
    assert cardmodel.card_ords("occlusion", {"image": "a.png", "shapes": shapes}) == [0, 1]
    assert cardmodel.split_tags("a, b  a  c::d") == ["a", "b", "c::d"] and cardmodel.join_tags(["a", "b"]) == " a b "
    assert cardmodel.media_files("flashcard", {"front": '<img src="x.png"> ![](y.jpg)', "back": "![](http://e/z.png)"}) \
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
    assert note["kind"] == json.loads(note["fields"])["type"] and json.loads(note["fields"]) == json.loads(db.card(ids[0])["data"])
    # A second book with the same title gets its own deck.
    other = db.upsert_book("h2", "/b.pdf", "A", 3)["id"]
    with db.conn:
        assert db.deck(db.book_deck(other))["name"] == "A (2)"


def test_old_cards_become_notes_in_book_decks(tmp_path):
    """A database from before decks: its cards table is rebuilt; reviews survive; a flashcard
    becomes a flashcard."""
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
    assert len(db.reviews(7)) == 1 and db.card_note(card["note_id"])["kind"] == "flashcard"
    assert json.loads(card["data"])["type"] == "flashcard" and json.loads(card["data"])["question"] == "F"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.delete_card(7)  # the review log follows its card again
    assert db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_migration_drops_card_passages_and_conversions(tmp_path):
    """Make cards from a highlight and Claude Conversion are gone: their tables and cards.passage_id
    go, and the cards they made stay."""
    path = tmp_path / "t.db"
    db = Database(path)
    book = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    note = db.add_card_note("flashcard", {"front": "F", "back": "B"}, db.book_deck(book), book_id=book)
    (card,) = db.note_cards(note)
    srs.review(db, card["id"], 3, now=NOW)
    db.conn.executescript("""
        CREATE TABLE card_passages (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id)
            ON DELETE CASCADE, page INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
            rects TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
        ALTER TABLE cards ADD COLUMN passage_id INTEGER REFERENCES card_passages(id) ON DELETE SET NULL;
        CREATE INDEX cards_passage ON cards (passage_id) WHERE passage_id IS NOT NULL;
        CREATE TABLE conversions (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id)
            ON DELETE CASCADE, status TEXT NOT NULL, proposal TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '', original TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
    """)
    db.conn.execute("INSERT INTO card_passages (id, book_id, page, kind, created_at) VALUES (1, ?, 0, 'text', 0)",
                    (book,))
    db.conn.execute("UPDATE cards SET passage_id=1 WHERE id=?", (card["id"],))
    db.conn.execute("INSERT INTO conversions (card_id, status, created_at) VALUES (?, 'accepted', 0)", (card["id"],))
    db.set_setting("convert_instructions", "be brief")
    db.conn.commit()
    db.conn.close()

    db = Database(path)
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master")}
    assert not tables & {"card_passages", "conversions", "cards_passage"}
    assert "passage_id" not in {r["name"] for r in db.conn.execute("PRAGMA table_info(cards)")}
    assert db.get_setting("convert_instructions") == ""
    kept = db.card(card["id"])
    assert kept["note_id"] == note and kept["reps"] == 1 and len(db.reviews(card["id"])) == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()
    Database(path).conn.close()  # opening it again is a no-op


def test_old_card_kinds_become_types(tmp_path):
    """Notes were once basic, reverse and quiz (any answered question; flashcards too, before
    that), and written answers "free": each becomes its card type, keeping ids, schedules,
    reviews, times, variants and the cards it had (a reverse side deleted before isn't made)."""
    path = tmp_path / "t.db"
    db = Database(path)
    deck = db.ensure_deck("D")

    def old(kind, fields, ords=(0,), variant=""):
        """A note and its cards as they were stored: data from the fields as card_data made it then."""
        note = db.conn.execute("INSERT INTO card_notes (kind, fields, tags, sort_text, search_text, created_at, "
                               "updated_at) VALUES (?,?,' t ','old','old',1,2)", (kind, json.dumps(fields))).lastrowid
        for ord in ords:
            if kind == "quiz":
                data = dict(fields)
            else:
                front, back = (fields["front"], fields["back"]) if ord == 0 else (fields["back"], fields["front"])
                data = {"type": "flashcard", "question": front, "answer": back, "explanation": "", "html": True}
            db.conn.execute("INSERT INTO cards (note_id, ord, deck_id, data, variant, due, reps, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,5,2,1,3)", (note, ord, deck, json.dumps(data), variant))
        return note

    with db.conn:
        basic = old("basic", {"front": "F", "back": "B", "extra": ""})
        reverse = old("reverse", {"front": "Fr", "back": "Bk", "extra": ""}, ords=(0, 1))
        one_side = old("reverse", {"front": "Lone", "back": "side", "extra": ""})  # its reverse card was deleted
        choice = old("quiz", {"type": "choice", "question": "Pick", "choices": ["a", "b"], "answer": 1, "explanation": ""})
        tf = old("quiz", {"type": "truefalse", "question": "True?", "answer": True, "explanation": ""})
        numeric = old("quiz", {"type": "numeric", "question": "How many?", "answer": 3, "unit": "", "answer_latex": "3",
                               "explanation": ""}, variant=json.dumps({"type": "numeric", "question": "How many now?",
                                                                       "answer": 4, "answer_latex": "4"}))
        free = old("quiz", {"type": "free", "question": "Why tea?", "answer": "Calm.", "rubric": "calm",
                            "explanation": "", "varies": True},
                   variant=json.dumps({"type": "free", "question": "Why milk?", "answer": "Fat.", "rubric": ""}))
        flash = old("quiz", {"type": "flashcard", "question": "Q", "answer": "A", "explanation": "E"})
        skipped = old("quiz", {"type": "free", "question": "Concept?", "answer": "Idea.", "rubric": "", "explanation": ""},
                      variant=json.dumps({"skip": True}))
    reviewed = db.note_cards(free)[0]["id"]
    with db.conn:
        db.conn.execute("INSERT INTO reviews (card_id, rating, source, reviewed_at) VALUES (?, 3, 'review', 4)", (reviewed,))
    db.conn.close()

    for _ in range(2):  # and opening it again changes nothing
        db = Database(path)
        kind = lambda n: db.card_note(n)["kind"]
        fields = lambda n: json.loads(db.card_note(n)["fields"])
        data = lambda n, i=0: json.loads(db.note_cards(n)[i]["data"])
        assert [kind(n) for n in (basic, reverse, one_side, choice, tf, numeric, free, flash, skipped)] == [
            "flashcard", "reversed", "reversed", "choice", "truefalse", "numeric", "written", "flashcard", "written"]
        assert not db.conn.execute("SELECT 1 FROM card_notes WHERE kind NOT IN (%s)"
                                   % ",".join("?" * len(cardmodel.TYPES)), cardmodel.TYPES).fetchall()
        assert [c["ord"] for c in db.note_cards(reverse)] == [0, 1] and [c["ord"] for c in db.note_cards(one_side)] == [0]
        assert data(reverse, 1)["question"] == "Bk" and data(reverse, 1)["type"] == "flashcard"
        assert fields(free)["type"] == data(free)["type"] == "written" and fields(free)["varies"] is True
        assert json.loads(db.note_cards(free)[0]["variant"])["type"] == "written"
        assert json.loads(db.note_cards(numeric)[0]["variant"])["question"] == "How many now?"
        assert json.loads(db.note_cards(skipped)[0]["variant"]) == {"skip": True}
        assert data(numeric) == {**fields(numeric), "type": "numeric"}
        assert fields(flash) == {"front": "Q", "back": "A", "extra": "E"} and data(flash)["type"] == "flashcard"
        assert db.card_note(free)["sort_text"] == "Why tea?" and "Calm." in db.card_note(free)["search_text"]
        card = db.card(reviewed)
        assert (card["reps"], card["due"], card["updated_at"]) == (2, 5, 3) and len(db.reviews(reviewed)) == 1
        assert db.card_note(free)["updated_at"] == 2 and db.card_note(free)["tags"] == " t "
        assert search.card_ids(db, "type:written") == [c["id"] for n in (free, skipped) for c in db.note_cards(n)]
        assert variants.wants_variant(db.card(reviewed)) and not variants.wants_variant(db.note_cards(skipped)[0])
        db.conn.close()


# -- scheduling ----------------------------------------------------------------------------
def test_parameters_retention_forget_and_due_dates(db):
    deck = db.ensure_deck("D")
    note = db.add_card_note("flashcard", {"front": "f", "back": "b"}, deck, now=NOW)
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
    db.add_card_note("flashcard", {"front": "1", "back": "x"}, a, now=NOW)
    db.add_card_note("cloze", {"text": "{{c1::x}} {{c2::y}}"}, b, now=NOW)
    counts = srs.deck_counts(db, NOW)
    top = db.deck_by_name("Top")["id"]
    assert counts[top]["new"] == 3 and counts[a]["new"] == 1 and counts[b]["total"] == 2
    assert srs.counts(db, None, NOW, deck=b)["new"] == 2
    card = srs.next_card(db, None, NOW, deck=a)
    assert card["deck_id"] == a


def test_flashcards_in_quizzes_are_flashcards(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    chat = db.create_chat(book, "Cards")
    reply = db.add_message(chat, "assistant", '```quiz Cards\n[{"type": "flashcard", "question": "F", '
                           '"answer": "B", "explanation": "E"}]\n```')
    (card_id,) = srs.add_quiz_cards(db, book, reply, db.message(reply)["text"], now=NOW)
    note = db.card_note(db.card(card_id)["note_id"])
    assert note["kind"] == "flashcard" and json.loads(note["fields"]) == {"front": "F", "back": "B", "extra": "E"}
    assert srs.question(db.card(card_id))["type"] == "flashcard" and db.card_for(reply, 0, 0)["id"] == card_id
    with pytest.raises(cardmodel.NoteError):
        db.add_card_note("written", {"type": "flashcard", "question": "F", "answer": "B"}, db.ensure_deck("D"))


# -- review of cards from no book ----------------------------------------------------------
def test_reviewing_cloze_and_bookless_written_cards(db, chat_panel):
    from study_reader.review import ReviewPanel

    _, worker, _, _ = chat_panel
    deck = db.ensure_deck("Anki::Physics")
    db.add_card_note("cloze", {"text": "{{c1::Gauss}}'s law"}, deck, now=NOW - 10)
    db.add_card_note("written", {"type": "written", "question": "Why?", "answer": "Because.", "explanation": ""}, deck, now=NOW)
    db.ensure_deck("Elsewhere")
    review = ReviewPanel(db)
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

    written = review.card_id
    review.check(written, {"text": ""})  # shown without writing an answer
    assert shown[-1][1][0]["previews"] and shown[-1][1][0]["answer"]["verdict"] is None
    review.rate(written, 3)
    assert db.reviews(written)[0]["verdict"] is None and not worker.jobs


# -- the browser ---------------------------------------------------------------------------
def test_browser_saves_after_a_pause(db, chat_panel):
    """The save timer firing saves: a single-shot timer is no longer active in its timeout."""
    from PySide6.QtTest import QTest
    from study_reader.browser import CardBrowser

    note = db.add_card_note("flashcard", {"front": "Q?", "back": "A"}, db.ensure_deck("Physics"))
    browser = CardBrowser(db)
    browser.table.selectRow(0)
    assert browser.editor.note_id == note
    browser.editor.fields["back"].setPlainText("changed")
    QTest.qWait(browser._save_timer.interval() + 200)
    assert json.loads(db.card_note(note)["fields"])["back"] == "changed"
    assert browser.editor_status.text().startswith("Saved")


def test_browser_search_edit_and_bulk_actions(db, chat_panel, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from study_reader.browser import CardBrowser, CardInfoDialog, find_replace

    _, worker, _, _ = chat_panel
    physics, maths = db.ensure_deck("Physics"), db.ensure_deck("Maths")
    gauss = db.add_card_note("flashcard", {"front": "Gauss's law relates?", "back": "flux and charge"}, physics, tags=["em"])
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

    # Cards made from the same text: named by their place, and an edit or tags reach them all.
    browser.set_search("Stokes")
    browser.table.selectRow(1)
    ord = db.card(browser.model.ids[1])["ord"]
    assert browser.card_line.text() == f"Maths · Cloze {ord + 1} of 2"
    browser.editor.fields["text"].setPlainText("{{c1::Stokes}} and {{c2::Green}}'s theorems")
    browser.save_note()
    assert "its other cloze card too" in browser.editor_status.text()
    asked = []
    monkeypatch.setattr(browser, "_ask_tags", lambda title, label: asked.append(label) or ["vector"])
    browser.add_tags()
    assert asked == ["Tags to add to 1 card (and 1 more made from the same text):"]
    assert browser.status.text() == "Added tags to 1 card (and 1 more made from the same text)."
    browser.table.selectAll()
    browser.delete()
    assert db.count_cards(deck_id=maths) == 0
    browser.sidebar.refresh()
    sections = {browser.sidebar.topLevelItem(i).text(0): browser.sidebar.topLevelItem(i)
                for i in range(browser.sidebar.topLevelItemCount())}
    types = sections["Types"]
    assert [types.child(i).text(0) for i in range(types.childCount())] == [
        cardmodel.TYPE_LABELS[t] for t in cardmodel.TYPES]
    assert types.child(0).data(0, Qt.ItemDataRole.UserRole) == "type:flashcard"
    menus = [a.text() for a in browser.menuBar().actions()]
    assert "&Notes" not in menus and "&Cards" in menus
    browser.close()


def test_add_dialog_makes_cards(db, chat_panel):
    from study_reader.card_editor import AddDialog

    dialog = AddDialog(db)
    added = []
    dialog.added.connect(added.append)
    dialog.deck.setCurrentText("New::Deck")
    assert [dialog.type.itemData(i) for i in range(dialog.type.count())] == [
        "flashcard", "reversed", "cloze", "choice", "truefalse", "numeric", "written"]
    dialog.type.setCurrentIndex(dialog.type.findData("cloze"))
    dialog.editor.fields["text"].setPlainText("The {{c1::mitochondria}} is the {{c2::powerhouse}}")
    dialog.editor.tags.setText("bio")
    dialog.add()
    assert len(db.note_cards(added[0])) == 2 and db.deck_by_name("New::Deck")
    assert dialog.editor.fields["text"].toPlainText() == "" and dialog.editor.tags.text() == "bio"

    dialog.type.setCurrentIndex(dialog.type.findData("choice"))
    dialog.editor.q_question.setPlainText("Pick B")
    dialog.editor.q_choices.setPlainText("A\nB\nC")
    dialog.editor.q_correct.setValue(2)
    dialog.add()
    q = json.loads(db.card_note(added[1])["fields"])
    assert q["type"] == "choice" and q["answer"] == 1 and q["choices"] == ["A", "B", "C"]
    assert db.card_note(added[1])["kind"] == "choice" and db.get_setting("add_type") == "choice"

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
    db.add_card_note("flashcard", {"front": "f", "back": "b"}, deck)
    win = MainWindow(db)
    win.show()
    assert win.nav.buttons["review"].badge == "1"
    browser = win.show_browser()  # the Cards page
    assert browser is win.show_browser() and len(browser.model.ids) == 1
    assert win.center.currentWidget() is win.cards_page and browser.isVisible() and not browser.isWindow()
    assert win.nav.buttons["cards"].isChecked() and not win.web_action.isEnabled()  # its Ctrl+Shift+O wins
    browser.reviewDeckRequested.emit(deck)
    assert win.page() == "review" and win.review.deck_id == deck
    assert win.web_action.isEnabled()
    win.review.start()
    win.review.act(win.review.card_id, "edit")
    assert win.page() == "cards" and browser.search_box.text().startswith("cid:")
    win.show_stats(deck)
    assert win.page() == "stats" and win.stats_window.isVisible() and not win.stats_window.isWindow()
    assert win.stats_window.scope.currentData() == ("deck", deck)
    browser.close()
    win.close()


# -- regressions ---------------------------------------------------------------------------
def test_edits_never_lose_reviewed_cards_or_revive_deleted_ones(db):
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

    # A deleted card isn't brought back by editing its note.
    db.delete_cards([c1["id"]])
    db.update_card_note(note, {"text": "{{c1::a}} {{c2::B}} {{c3::c}} fixed typo"})
    assert 0 not in {c["ord"] for c in db.note_cards(note)}


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
    note = db.add_card_note("flashcard", {"front": "f"}, db.ensure_deck("D"), tags=["A", "A::B", "C"])
    db.rename_tag("A", "")
    assert db.card_note(note)["tags"] == " C "


def test_browser_columns_fit_the_table(db, chat_panel):
    from study_reader.browser import COLUMNS_SETTING, DEFAULT_COLUMNS, WIDTHS_SETTING, CardBrowser

    db.add_card_note("flashcard", {"front": "Gauss's law relates?", "back": "flux and charge"}, db.ensure_deck("Physics"))
    browser = CardBrowser(db)
    browser.resize(1600, 800)
    browser.show()
    header, view = browser.table.horizontalHeader(), browser.table.viewport()

    def sizes():
        return {key: header.sectionSize(i) for i, key in enumerate(browser.model.columns)}

    def fits():
        return sum(sizes().values()) == view.width()

    assert fits() and sizes()["question"] > sizes()["answer"] > sizes()["due"] >= browser._compact_width("due")
    browser.table.resize(600, 400)  # narrower than the columns' minimums: they shrink rather than scroll
    assert fits()
    browser.table.resize(1200, 400)
    assert fits() and sizes()["deck"] <= 240

    question = sizes()["question"]
    header.resizeSection(0, question - 100)  # the user narrows Question: the other wide columns take the space
    assert sizes()["question"] == question - 100 and fits()
    browser._widths_timer.timeout.emit()
    assert json.loads(db.get_setting(WIDTHS_SETTING, "{}")) == {"question": question - 100}

    header.moveSection(header.visualIndex(browser.model.columns.index("due")), 0)  # a dragged column stays put
    assert browser.model.columns[0] == "due" and header.logicalIndex(0) == 0
    assert json.loads(db.get_setting(COLUMNS_SETTING, "[]"))[0] == "due"
    assert sizes()["question"] == question - 100 and fits()

    browser._reset_columns()
    assert browser.model.columns == DEFAULT_COLUMNS and fits() and db.get_setting(WIDTHS_SETTING, "") == "{}"
    assert all(header.visualIndex(i) == i for i in range(header.count()))
    browser.close()


def test_clicking_a_row_shows_that_card(db, chat_panel):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from study_reader.browser import CardBrowser

    deck = db.ensure_deck("D")
    for i in range(4):
        db.add_card_note("flashcard", {"front": f"front {i}", "back": "b"}, deck)
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


def test_switching_a_questions_type_in_the_browser(db, chat_panel):
    from study_reader.browser import CardBrowser

    deck = db.ensure_deck("D")
    written = db.add_card_note("written", {"type": "written", "question": "Rinse time in seconds?", "answer": "41"}, deck)
    choice = db.add_card_note("choice", {"type": "choice", "question": "Pick", "choices": ["tea", "milk"], "answer": 1}, deck)
    browser = CardBrowser(db)
    browser.show()
    browser.set_search("")
    e = browser.editor
    fields = lambda note: json.loads(db.card_note(note)["fields"])

    def show(note):
        browser.table.selectRow(next(r for r, c in enumerate(browser.model.ids) if db.card(c)["note_id"] == note))
        assert e.note_id == note

    def switch(t):
        e.switch_type(t)
        browser.save_note()
        assert e.type_box.currentData() == t

    kind = lambda note: db.card_note(note)["kind"]
    # The answer comes along, so the new type saves at once.
    show(written)
    switch("numeric")
    assert kind(written) == fields(written)["type"] == "numeric" and fields(written)["answer"] == 41
    switch("written")
    assert kind(written) == fields(written)["type"] == "written" and fields(written)["answer"] == "41"
    switch("flashcard")  # the question and answer are its front and back
    assert kind(written) == "flashcard" and fields(written) == {"front": "Rinse time in seconds?", "back": "41", "extra": ""}
    assert json.loads(db.note_cards(written)[0]["data"])["type"] == "flashcard"
    switch("reversed")
    assert len(db.note_cards(written)) == 2 and "both sides" in browser.editor_status.text()
    switch("written")
    assert kind(written) == "written" and fields(written)["question"] == "Rinse time in seconds?"
    assert len(db.note_cards(written)) == 1  # the reverse side, never reviewed, goes
    show(choice)
    switch("written")
    assert fields(choice)["answer"] == "milk"
    switch("truefalse")
    switch("choice")
    assert fields(choice)["choices"] == ["tea", "milk"]  # what was entered is still there

    # A type whose answer is missing isn't saved yet, but the switch isn't lost on leaving the card.
    show(written)
    e.q_answer.setPlainText("Because tea.")
    switch("choice")
    assert "Not saved" in browser.editor_status.text() and fields(written)["type"] == "written"
    show(choice)
    show(written)
    assert e.type_box.currentData() == "choice" and "Not saved" in browser.editor_status.text()
    assert e.q_choices.toPlainText().strip() == "Because tea."
    e.q_choices.setPlainText("Because tea.\nBecause milk.")
    browser.save_note()
    assert fields(written)["type"] == "choice" and fields(written)["choices"] == ["Because tea.", "Because milk."]
    show(choice)
    show(written)
    assert browser.editor_status.text() == "" and e.type_box.currentData() == "choice"
    browser.close()
