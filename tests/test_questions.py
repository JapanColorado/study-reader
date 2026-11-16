import json

import pytest

from study_reader import question_model, search, practice, variants
from study_reader import db as db_module
from study_reader.db import Database

from test_practice import NOW, setup
from test_quiz import QUIZ

FILL_IN = r"In {{c1::equilibrium::where}}, \(\rho = {{c2::0}}\) and {{c1::<b>surface</b>}}"


# -- groups and their questions-------------------------------------------------------------
def test_fill_in_questions_and_sides():
    fields = {"text": FILL_IN, "extra": "Gauss"}
    assert question_model.question_ords("fill_in", fields) == [0, 1]
    one = question_model.question_data("fill_in", fields, 0)
    assert one["type"] == "fill_in" and one["html"] and one["explanation"] == "Gauss"
    assert one["question"] == r'In <span class="cloze">[where]</span>, \(\rho = 0\) and <span class="cloze">[…]</span>'
    two = question_model.question_data("fill_in", fields, 1)
    assert r"\htmlClass{cloze}{\text{[\dots]}}" in two["question"] and "<b>surface</b>" in two["question"]
    assert r"\htmlClass{cloze}{0}" in two["answer"]
    assert question_model.answer_text(one) == "equilibrium … surface"
    assert question_model.plain(FILL_IN) == r"In equilibrium, \(\rho = 0\) and surface"
    assert question_model.renumber_next(FILL_IN) == 3
    with pytest.raises(question_model.QuestionError):
        question_model.check("fill_in", {"text": "no deletions"})


def test_fill_in_math_holding_closing_braces():
    text = r"\(\omega_0\) = {{c1::\(\frac{1}{\sqrt{LC}}\)}}, called {{c2::resonance::what}}"
    assert question_model.question_ords("fill_in", {"text": text}) == [0, 1]
    assert question_model.fill_in_side(text, 1, True) == (
        r'\(\omega_0\) = <span class="cloze">\(\frac{1}{\sqrt{LC}}\)</span>, called resonance')
    assert question_model.fill_in_side(text, 2, False).endswith(r'\(\frac{1}{\sqrt{LC}}\), called <span class="cloze">[what]</span>')


def test_math_spans_hold_escaped_dollars():
    text = r"Pizza ($q_1$) at $p_1 = \$1$, income $Y = \$50$; it costs \$5 or $5 and $6."
    assert [text[a:b] for a, b in question_model.math_spans(text)] == ["$q_1$", r"$p_1 = \$1$", r"$Y = \$50$"]


def test_both_ways_image_labels_and_tags():
    assert question_model.question_ords("both_ways", {"front": "a", "back": "b"}) == [0, 1]
    back = question_model.question_data("both_ways", {"front": "a", "back": "b"}, 1)
    assert (back["question"], back["answer"]) == ("b", "a")
    shapes = question_model.parse_image_labels(
        "{{c1::image-occlusion:rect:left=.1:top=.2:width=.3:height=.1:oi=1}}<br>"
        "{{c2::image-occlusion:polygon:points=0.1,0.2 0.3,0.4 0.5,0.1}}")
    assert shapes[0] == {"ord": 1, "shape": "rect", "left": 0.1, "top": 0.2, "width": 0.3, "height": 0.1, "oi": True}
    assert shapes[1]["points"] == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.1]]
    assert question_model.question_ords("image_labels", {"image": "a.png", "shapes": shapes}) == [0, 1]
    assert question_model.split_tags("a, b  a  c::d") == ["a", "b", "c::d"] and question_model.join_tags(["a", "b"]) == " a b "
    assert question_model.media_files("self_check", {"front": '<img src="x.png"> ![](y.jpg)', "back": "![](http://e/z.png)"}) \
        == ["x.png", "y.jpg"]


def test_groups_decks_and_editing(db):
    deck = db.ensure_deck(" Physics :: Lesson 1 ")
    assert [d["name"] for d in db.decks()] == ["Physics", "Physics::Lesson 1"]
    group = db.add_question_group("fill_in", {"text": FILL_IN, "extra": ""}, deck, tags=["exam", "ch1"])
    questions = db.group_questions(group)
    assert [c["ord"] for c in questions] == [0, 1] and all(c["deck_id"] == deck for c in questions)
    assert db.question_group(group)["tags"] == " exam ch1 " and "equilibrium" in db.question_group(group)["search_text"]

    # A new blank makes a question; one taken out loses its question.
    db.update_question_group(group, {"text": "{{c1::a}} {{c3::b}}", "extra": ""})
    assert [c["ord"] for c in db.group_questions(group)] == [0, 2]
    assert db.group_questions(group)[0]["id"] == questions[0]["id"]

    db.add_tags([group], ["new"])
    db.remove_tags([group], ["CH1"])
    assert db.question_group(group)["tags"].split() == ["exam", "new"]
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
    assert db.question_group(group) is None and [d["name"] for d in db.decks()] == []


def test_quiz_questions_go_into_their_books_deck(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    deck = db.book(book_id)["deck_id"]
    assert db.deck(deck)["name"] == "A" and {db.question(i)["deck_id"] for i in ids} == {deck}
    group = db.question_group(db.question(ids[0])["group_id"])
    assert group["kind"] == json.loads(group["fields"])["type"] and json.loads(group["fields"]) == json.loads(db.question(ids[0])["data"])
    # A second book with the same title gets its own deck.
    other = db.upsert_book("h2", "/b.pdf", "A", 3)["id"]
    with db.conn:
        assert db.deck(db.book_deck(other))["name"] == "A (2)"


def test_old_questions_become_groups_in_book_decks(tmp_path):
    """A database from before decks (its questions in a cards table, attempts in reviews): the
    table is rebuilt; attempts survive; a flashcard becomes a self-check."""
    path = tmp_path / "old.db"
    db = Database(path)
    book = db.upsert_book("h", "/a.pdf", "Old book", 3)["id"]
    db.conn.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TABLE questions; DROP TABLE question_groups; DROP TABLE decks; DROP TABLE attempts;
        CREATE TABLE cards (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL, quiz INTEGER NOT NULL, question INTEGER NOT NULL,
            title TEXT NOT NULL, data TEXT NOT NULL, page INTEGER, stage INTEGER NOT NULL DEFAULT 0,
            fsrs TEXT NOT NULL DEFAULT '', due REAL NOT NULL, reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            UNIQUE (message_id, quiz, question));
        CREATE TABLE reviews (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL, source TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', verdict TEXT,
            reviewed_at REAL NOT NULL);
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
    question = db.question(7)
    assert question["group_id"] and db.deck(question["deck_id"])["name"] == "Old book" and question["reps"] == 3
    assert (question["stability"], question["difficulty"]) == (9.5, 3.0) and question["last_attempt"] > 1.7e9
    (attempt,) = db.attempts(7)
    assert (attempt["source"], attempt["was_new"]) == ("practice", 1)
    assert db.question_group(question["group_id"])["kind"] == "self_check"
    assert json.loads(question["data"])["type"] == "self_check" and json.loads(question["data"])["question"] == "F"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (tmp_path / "old.before-rename.db").exists()
    db.delete_question(7)  # the attempts follow their question again
    assert db.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_migration_drops_question_passages_and_conversions(tmp_path):
    """Making questions from a highlight and Claude Conversion are gone: their tables and the
    questions table's passage_id go, and the questions they made stay."""
    path = tmp_path / "t.db"
    db = Database(path)
    book = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    group = db.add_question_group("self_check", {"front": "F", "back": "B"}, db.book_deck(book), book_id=book)
    (question,) = db.group_questions(group)
    practice.attempt(db, question["id"], 3, now=NOW)
    db.conn.executescript("""
        CREATE TABLE card_passages (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id)
            ON DELETE CASCADE, page INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
            rects TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
        ALTER TABLE questions ADD COLUMN passage_id INTEGER REFERENCES card_passages(id) ON DELETE SET NULL;
        CREATE INDEX cards_passage ON questions (passage_id) WHERE passage_id IS NOT NULL;
        CREATE TABLE conversions (id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES questions(id)
            ON DELETE CASCADE, status TEXT NOT NULL, proposal TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '', original TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
    """)
    db.conn.execute("INSERT INTO card_passages (id, book_id, page, kind, created_at) VALUES (1, ?, 0, 'text', 0)",
                    (book,))
    db.conn.execute("UPDATE questions SET passage_id=1 WHERE id=?", (question["id"],))
    db.conn.execute("INSERT INTO conversions (question_id, status, created_at) VALUES (?, 'accepted', 0)", (question["id"],))
    db.set_setting("convert_instructions", "be brief")
    db.conn.commit()
    db.conn.close()

    db = Database(path)
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master")}
    assert not tables & {"card_passages", "conversions", "cards_passage"}
    assert "passage_id" not in {r["name"] for r in db.conn.execute("PRAGMA table_info(questions)")}
    assert db.get_setting("convert_instructions") == ""
    kept = db.question(question["id"])
    assert kept["group_id"] == group and kept["reps"] == 1 and len(db.attempts(question["id"])) == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()
    Database(path).conn.close()  # opening it again is a no-op


def test_old_question_kinds_become_types(tmp_path):
    """Groups were once basic, reverse and quiz (any answered question; self-checks too, before
    that), and short answers "free": each becomes its type, keeping ids, schedules, attempts,
    times, variants and the questions it had (a reverse side deleted before isn't made)."""
    path = tmp_path / "t.db"
    db = Database(path)
    deck = db.ensure_deck("D")

    def old(kind, fields, ords=(0,), variant=""):
        """A group and its questions as they were stored: data from the fields as question_data made it then."""
        group = db.conn.execute("INSERT INTO question_groups (kind, fields, tags, sort_text, search_text, created_at, "
                                "updated_at) VALUES (?,?,' t ','old','old',1,2)", (kind, json.dumps(fields))).lastrowid
        for ord in ords:
            if kind == "quiz":
                data = dict(fields)
            else:
                front, back = (fields["front"], fields["back"]) if ord == 0 else (fields["back"], fields["front"])
                data = {"type": "flashcard", "question": front, "answer": back, "explanation": "", "html": True}
            db.conn.execute("INSERT INTO questions (group_id, ord, deck_id, data, variant, due, reps, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,5,2,1,3)", (group, ord, deck, json.dumps(data), variant))
        return group

    with db.conn:
        basic = old("basic", {"front": "F", "back": "B", "extra": ""})
        reverse = old("reverse", {"front": "Fr", "back": "Bk", "extra": ""}, ords=(0, 1))
        one_side = old("reverse", {"front": "Lone", "back": "side", "extra": ""})  # its reverse question was deleted
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
    attempted = db.group_questions(free)[0]["id"]
    with db.conn:
        db.conn.execute("INSERT INTO attempts (question_id, rating, source, attempted_at) VALUES (?, 3, 'review', 4)",
                        (attempted,))
    db.conn.close()

    for _ in range(2):  # and opening it again changes nothing
        db = Database(path)
        kind = lambda g: db.question_group(g)["kind"]
        fields = lambda g: json.loads(db.question_group(g)["fields"])
        data = lambda g, i=0: json.loads(db.group_questions(g)[i]["data"])
        assert [kind(g) for g in (basic, reverse, one_side, choice, tf, numeric, free, flash, skipped)] == [
            "self_check", "both_ways", "both_ways", "choice", "truefalse", "calculation", "short_answer", "self_check",
            "short_answer"]
        assert not db.conn.execute("SELECT 1 FROM question_groups WHERE kind NOT IN (%s)"
                                   % ",".join("?" * len(question_model.TYPES)), question_model.TYPES).fetchall()
        assert [q["ord"] for q in db.group_questions(reverse)] == [0, 1] and [q["ord"] for q in db.group_questions(one_side)] == [0]
        assert data(reverse, 1)["question"] == "Bk" and data(reverse, 1)["type"] == "self_check"
        assert fields(free)["type"] == data(free)["type"] == "short_answer" and fields(free)["varies"] is True
        assert json.loads(db.group_questions(free)[0]["variant"])["type"] == "short_answer"
        assert json.loads(db.group_questions(numeric)[0]["variant"])["type"] == "calculation"
        assert json.loads(db.group_questions(numeric)[0]["variant"])["question"] == "How many now?"
        assert json.loads(db.group_questions(skipped)[0]["variant"]) == {"skip": True}
        assert data(numeric) == {**fields(numeric), "type": "calculation"}
        assert fields(flash) == {"front": "Q", "back": "A", "extra": "E"} and data(flash)["type"] == "self_check"
        assert db.question_group(free)["sort_text"] == "Why tea?" and "Calm." in db.question_group(free)["search_text"]
        question = db.question(attempted)
        assert (question["reps"], question["due"], question["updated_at"]) == (2, 5, 3)
        assert [a["source"] for a in db.attempts(attempted)] == ["practice"]
        assert db.question_group(free)["updated_at"] == 2 and db.question_group(free)["tags"] == " t "
        assert search.question_ids(db, "type:short_answer") == [q["id"] for g in (free, skipped) for q in db.group_questions(g)]
        assert search.question_ids(db, "type:written") == search.question_ids(db, "type:short_answer")
        assert variants.wants_variant(db.question(attempted)) and not variants.wants_variant(db.group_questions(skipped)[0])
        db.conn.close()


# The schema as it was before questions, attempts and groups took their names (cards, reviews and
# card_notes, named as Anki names them), with its indexes: the tables whose names changed or that
# point at them.
PRE_RENAME_SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0, last_page INTEGER NOT NULL DEFAULT 0, zoom REAL NOT NULL DEFAULT 0,
    crop_margins INTEGER NOT NULL DEFAULT 0, show_toc INTEGER NOT NULL DEFAULT 1, right_panel TEXT NOT NULL DEFAULT 'chat',
    show_chat_list INTEGER NOT NULL DEFAULT 1, base_session_id TEXT, context_mode TEXT, source_url TEXT, finished_at REAL,
    toc TEXT, opened_at REAL NOT NULL, deck_id INTEGER REFERENCES decks(id) ON DELETE SET NULL);
CREATE TABLE chats (id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
    card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL, title TEXT NOT NULL, session_id TEXT,
    model TEXT NOT NULL DEFAULT '', throwaway INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, text TEXT NOT NULL, session_id TEXT, session_uuid TEXT, page_from INTEGER, page_to INTEGER,
    stopped INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
CREATE TABLE card_notes (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, fields TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
    sort_text TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL DEFAULT '', anki_id INTEGER, created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
CREATE TABLE cards (id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES card_notes(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL DEFAULT 0, deck_id INTEGER NOT NULL REFERENCES decks(id),
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL, message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    quiz INTEGER, question INTEGER, title TEXT NOT NULL DEFAULT '', data TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '',
    page INTEGER, stage INTEGER NOT NULL DEFAULT 0, fsrs TEXT NOT NULL DEFAULT '', due REAL NOT NULL,
    reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0,
    flag INTEGER NOT NULL DEFAULT 0, stability REAL, difficulty REAL, last_review REAL, anki_id INTEGER,
    created_at REAL NOT NULL, updated_at REAL NOT NULL DEFAULT 0, UNIQUE (message_id, quiz, question));
CREATE TABLE reviews (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL, source TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', verdict TEXT,
    reviewed_at REAL NOT NULL, duration INTEGER NOT NULL DEFAULT 0, was_new INTEGER NOT NULL DEFAULT 0);
CREATE TABLE deck_drafts (id INTEGER PRIMARY KEY, mode TEXT NOT NULL, request TEXT NOT NULL DEFAULT '{}',
    deck TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE draft_batches (id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES deck_drafts(id) ON DELETE CASCADE,
    session_id TEXT, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
CREATE TABLE draft_items (id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES deck_drafts(id) ON DELETE CASCADE,
    batch_id INTEGER REFERENCES draft_batches(id) ON DELETE SET NULL, number INTEGER NOT NULL,
    card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE, status TEXT NOT NULL, proposal TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '', checked INTEGER NOT NULL DEFAULT 1);
CREATE TABLE card_rewrites (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    original TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX cards_due ON cards (book_id, suspended, stage, due);
CREATE INDEX cards_deck ON cards (deck_id, suspended, stage, due);
CREATE INDEX cards_note ON cards (note_id);
CREATE INDEX reviews_card ON reviews (card_id);
CREATE INDEX reviews_time ON reviews (reviewed_at);
CREATE UNIQUE INDEX card_notes_anki ON card_notes (anki_id) WHERE anki_id IS NOT NULL;
CREATE UNIQUE INDEX cards_anki ON cards (anki_id) WHERE anki_id IS NOT NULL;
CREATE INDEX draft_items_draft ON draft_items (draft_id, number);
CREATE INDEX card_rewrites_card ON card_rewrites (card_id);
CREATE INDEX chats_book ON chats (book_id, throwaway);
CREATE INDEX chats_card ON chats (card_id);
"""


def test_pre_rename_database_moves_to_the_new_names(tmp_path):
    """A database from before the renames (cards, card_notes, reviews; Anki's type names; srs_
    and review_ settings) is copied aside once, then everything moves over: no row lost, no empty
    table left beside an old one, foreign keys pointing at the renamed tables."""
    import sqlite3

    path = tmp_path / "study-reader.db"
    conn = sqlite3.connect(path)
    conn.executescript(PRE_RENAME_SCHEMA)
    conn.executescript("""
        INSERT INTO books (id, hash, path, title, opened_at, deck_id) VALUES (1, 'h', '/a.pdf', 'A', 0, 1);
        INSERT INTO decks VALUES (1, 'A', 0);
        INSERT INTO chats (id, book_id, card_id, title, created_at, updated_at) VALUES (1, 1, 6, 'quiz', 0, 0);
        INSERT INTO messages (id, chat_id, role, text, created_at) VALUES (1, 1, 'assistant', 'a quiz', 0);
        INSERT INTO settings VALUES ('srs_new_per_day', '7'), ('srs_desired_retention', '0.85'),
            ('srs_parameters', '[1]'), ('srs_backfilled', '1'), ('review_panel', 'calc'), ('review_calc', '1'),
            ('review_chat_list', '0'), ('add_type', 'cloze'), ('practice_new_per_day', '9'), ('theme', 'dark');
    """)
    groups = [
        ("flashcard", {"front": "F", "back": "B", "extra": ""}, [{"type": "flashcard", "question": "F", "answer": "B"}]),
        ("reversed", {"front": "Fr", "back": "Bk", "extra": ""},
         [{"type": "flashcard", "question": "Fr", "answer": "Bk"}, {"type": "flashcard", "question": "Bk", "answer": "Fr"}]),
        ("cloze", {"text": "{{c1::a}}", "extra": ""}, [{"type": "cloze", "question": "[…]", "answer": "a"}]),
        ("occlusion", {"image": "i.png", "shapes": [{"ord": 1, "shape": "rect"}], "header": "", "extra": ""},
         [{"type": "occlusion", "question": "", "image": "i.png", "active": 1}]),
        ("numeric", {"type": "numeric", "question": "How many?", "answer": 3},
         [{"type": "numeric", "question": "How many?", "answer": 3}]),
        ("written", {"type": "written", "question": "Why?", "answer": "So."}, [{"type": "written", "question": "Why?", "answer": "So."}]),
        ("choice", {"type": "choice", "question": "Pick", "choices": ["a", "b"], "answer": 0},
         [{"type": "choice", "question": "Pick", "choices": ["a", "b"], "answer": 0}]),
    ]
    question_id = 0
    for group_id, (kind, fields, datas) in enumerate(groups, 1):
        conn.execute("INSERT INTO card_notes (id, kind, fields, tags, sort_text, created_at, updated_at, anki_id) "
                     "VALUES (?,?,?,' t ',?,1,2,?)", (group_id, kind, json.dumps(fields), kind, 100 + group_id))
        for ord, data in enumerate(datas):
            question_id += 1
            variant = json.dumps({**data, "answer": 4}) if kind == "numeric" else ""
            conn.execute("INSERT INTO cards (id, note_id, ord, deck_id, book_id, message_id, quiz, question, data, variant, "
                         "stage, due, reps, last_review, anki_id, created_at, updated_at) "
                         "VALUES (?,?,?,1,1,?,?,?,?,?,2,1.9e9,1,1.7e9,?,1,3)",
                         (question_id, group_id, ord, 1 if kind == "numeric" else None, 0 if kind == "numeric" else None,
                          0 if kind == "numeric" else None, json.dumps(data), variant, 200 + question_id))
    conn.executescript("""
        INSERT INTO reviews (card_id, rating, source, reviewed_at, was_new) VALUES (6, 3, 'chat', 1.6e9, 1),
            (6, 1, 'review', 1.7e9, 0), (1, 4, 'anki', 1.5e9, 1);
        INSERT INTO deck_drafts (id, mode, created_at) VALUES (1, 'improve', 0);
        INSERT INTO draft_items (draft_id, number, card_id, status, proposal) VALUES (1, 1, 7, 'proposed',
            '[{"kind": "written", "fields": {"type": "written", "question": "Why?", "answer": "So."}, "tags": []}]');
        INSERT INTO card_rewrites (card_id, original, created_at) VALUES (5, '{"note_id": 4, "kind": "occlusion", "fields": {}, '
            || '"ord": 0, "data": {"type": "occlusion", "question": ""}, "added": [2]}', 0);
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    backup = tmp_path / "study-reader.before-rename.db"
    old = sqlite3.connect(backup)
    assert old.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 8 and old.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    old.close()
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    assert not names & {"cards", "card_notes", "reviews", "card_rewrites", *db_module.OLD_INDEXES}
    assert {"questions_due", "questions_deck", "questions_group", "attempts_question", "attempts_time",
            "question_groups_anki", "questions_anki", "question_rewrites_question", "chats_question"} <= names
    count = lambda table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert (count("question_groups"), count("questions"), count("attempts"), count("question_rewrites"), count("draft_items")) \
        == (7, 8, 3, 1, 1)

    kinds = [db.question_group(i)["kind"] for i in range(1, 8)]
    assert kinds == ["self_check", "both_ways", "fill_in", "image_labels", "calculation", "short_answer", "choice"]
    assert [json.loads(db.question(i)["data"])["type"] for i in range(1, 9)] == [
        "self_check", "self_check", "self_check", "fill_in", "image_labels", "calculation", "short_answer", "choice"]
    assert json.loads(db.question_group(5)["fields"]) == {"type": "calculation", "question": "How many?", "answer": 3}
    assert json.loads(db.question(6)["variant"]) == {"type": "calculation", "question": "How many?", "answer": 4}
    calculation = db.question_for(1, 0, 0)
    assert calculation["id"] == 6 and calculation["group_id"] == 5 and calculation["position"] == 0 and calculation["last_attempt"] == 1.7e9
    assert (calculation["stage"], calculation["due"], calculation["reps"], calculation["updated_at"]) == (2, 1.9e9, 1, 3)
    assert db.question_group(1)["tags"] == " t " and db.question_group(1)["updated_at"] == 2
    assert db.group_by_anki_id(104)["id"] == 4 and db.question_by_anki_id(205)["id"] == 5
    assert [(a["rating"], a["source"], a["attempted_at"]) for a in db.attempts(6)] == [(3, "chat", 1.6e9), (1, "practice", 1.7e9)]
    assert db.attempts(1)[0]["source"] == "anki"
    assert db.chat(1)["question_id"] == 6 and db.question_chat(6)["id"] == 1
    item = db.draft_items(1)[0]
    assert item["question_id"] == 7 and json.loads(item["proposal"])[0]["kind"] == "short_answer"
    assert json.loads(item["proposal"])[0]["fields"]["type"] == "short_answer"
    original = json.loads(db.question_rewrite(5)["original"])
    assert original == {"group_id": 4, "kind": "image_labels", "fields": {}, "ord": 0,
                        "data": {"type": "image_labels", "question": ""}, "added": [2]}
    settings = dict(db.conn.execute("SELECT key, value FROM settings").fetchall())
    assert settings == {"practice_new_per_day": "9", "practice_desired_retention": "0.85", "practice_parameters": "[1]",
                        "practice_backfilled": "1", "practice_panel": "calc", "practice_calc": "1",
                        "practice_chat_list": "0", "add_type": "fill_in", "theme": "dark"}
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert search.question_ids(db, "type:cloze") == search.question_ids(db, "type:fill_in") == [4]

    # The foreign keys moved with the tables: deleting a question takes its attempts, chats let go.
    db.delete_question(6)
    assert db.attempts(6) == [] and db.chat(1)["question_id"] is None
    db.conn.close()
    stamp = backup.stat().st_mtime_ns
    db = Database(path)  # again: nothing to do, and the backup is kept as it was
    assert backup.stat().st_mtime_ns == stamp and db.question(5)["group_id"] == 4
    db.conn.close()


# -- scheduling ----------------------------------------------------------------------------
def test_parameters_retention_forget_and_due_dates(db):
    deck = db.ensure_deck("D")
    group = db.add_question_group("self_check", {"front": "f", "back": "b"}, deck, now=NOW)
    (question,) = db.group_questions(group)
    assert practice.desired_retention(db) == 0.9 and len(practice.parameters(db)) == 21
    db.set_setting(practice.RETENTION_SETTING, "0.8")
    practice.set_parameters(db, list(practice.parameters(None))[:19])  # FSRS-5's 19 weights are extended
    assert practice.parameters(db)[19:] == (0.0, 0.5) and practice.scheduler(db).desired_retention == 0.8
    with pytest.raises(ValueError):
        practice.set_parameters(db, [1.0] * 7)

    practice.attempt(db, question["id"], 3, now=NOW, duration=4200)
    practice.attempt(db, question["id"], 3, now=db.question(question["id"])["due"])
    attempted = db.question(question["id"])
    assert attempted["stage"] == 2 and attempted["stability"] and attempted["last_attempt"]
    assert db.attempts(question["id"])[0]["duration"] == 4200 and practice.retrievability(db, attempted, attempted["due"]) > 0.5

    assert practice.set_due(db, [question["id"]], 3, now=NOW) == 1
    assert practice.day_start(NOW) + 3 * 86400 <= db.question(question["id"])["due"] < practice.next_day(NOW) + 3 * 86400
    practice.forget(db, [question["id"]], now=NOW)
    forgotten = db.question(question["id"])
    assert (forgotten["stage"], forgotten["reps"], forgotten["fsrs"]) == (0, 0, "") and len(db.attempts(question["id"])) == 2
    assert practice.set_due(db, [question["id"]], 1, now=NOW) == 0  # new questions stay new


def test_deck_counts_and_deck_scoped_queue(db):
    a, b = db.ensure_deck("Top::A"), db.ensure_deck("Top::B")
    db.add_question_group("self_check", {"front": "1", "back": "x"}, a, now=NOW)
    db.add_question_group("fill_in", {"text": "{{c1::x}} {{c2::y}}"}, b, now=NOW)
    counts = practice.deck_counts(db, NOW)
    top = db.deck_by_name("Top")["id"]
    assert counts[top]["new"] == 3 and counts[a]["new"] == 1 and counts[b]["total"] == 2
    assert practice.counts(db, None, NOW, deck=b)["new"] == 2
    question = practice.next_question(db, None, NOW, deck=a)
    assert question["deck_id"] == a


def test_self_checks_in_quizzes_are_self_checks(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    chat = db.create_chat(book, "Self-checks")
    reply = db.add_message(chat, "assistant", '```quiz Self-checks\n[{"type": "self_check", "question": "F", '
                           '"answer": "B", "explanation": "E"}]\n```')
    (question_id,) = practice.add_quiz_questions(db, book, reply, db.message(reply)["text"], now=NOW)
    group = db.question_group(db.question(question_id)["group_id"])
    assert group["kind"] == "self_check" and json.loads(group["fields"]) == {"front": "F", "back": "B", "extra": "E"}
    assert practice.question(db.question(question_id))["type"] == "self_check" and db.question_for(reply, 0, 0)["id"] == question_id
    with pytest.raises(question_model.QuestionError):
        db.add_question_group("short_answer", {"type": "self_check", "question": "F", "answer": "B"}, db.ensure_deck("D"))


# -- practice of questions from no book-------------------------------------------------------
def test_practicing_fill_in_and_bookless_short_answers(db, chat_panel):
    from study_reader.practice_view import PracticePanel

    _, worker, _, _ = chat_panel
    deck = db.ensure_deck("Anki::Physics")
    db.add_question_group("fill_in", {"text": "{{c1::Gauss}}'s law"}, deck, now=NOW - 10)
    db.add_question_group("short_answer", {"type": "short_answer", "question": "Why?", "answer": "Because.", "explanation": ""}, deck, now=NOW)
    db.ensure_deck("Elsewhere")
    panel = PracticePanel(db)
    shown = []
    panel._js = lambda fn, *args: shown.append((fn, args))
    panel.choose_deck(db.deck_by_name("Anki")["id"])
    overview = shown[-1][1][0]
    assert overview["title"] == "Anki" and [d["path"] for d in overview["decks"]] == ["Anki", "Anki::Physics"]

    panel.start()
    shown_question = shown[-1][1][0]
    assert shown_question["question"]["type"] == "fill_in" and shown_question["deck"] == "Anki::Physics" and shown_question["page"] is None
    panel.check(panel.question_id, {})  # revealed: self-rated, nothing graded
    assert shown[-1][1][0]["previews"] and shown[-1][1][0]["answer"]["verdict"] is None
    panel.rate(panel.question_id, 3)

    short = panel.question_id
    panel.check(short, {"text": ""})  # shown without writing an answer
    assert shown[-1][1][0]["previews"] and shown[-1][1][0]["answer"]["verdict"] is None
    panel.rate(short, 3)
    assert db.attempts(short)[0]["verdict"] is None and not worker.jobs


# -- the browser ---------------------------------------------------------------------------
def test_browser_saves_after_a_pause(db, chat_panel):
    """The save timer firing saves: a single-shot timer is no longer active in its timeout."""
    from PySide6.QtTest import QTest
    from study_reader.browser import QuestionBrowser

    group = db.add_question_group("self_check", {"front": "Q?", "back": "A"}, db.ensure_deck("Physics"))
    browser = QuestionBrowser(db)
    browser.table.selectRow(0)
    assert browser.editor.group_id == group
    browser.editor.fields["back"].setPlainText("changed")
    QTest.qWait(browser._save_timer.interval() + 200)
    assert json.loads(db.question_group(group)["fields"])["back"] == "changed"
    assert browser.editor_status.text().startswith("Saved")


def test_browser_search_edit_and_bulk_actions(db, chat_panel, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from study_reader.browser import QuestionBrowser, QuestionInfoDialog, find_replace

    _, worker, _, _ = chat_panel
    physics, maths = db.ensure_deck("Physics"), db.ensure_deck("Maths")
    gauss = db.add_question_group("self_check", {"front": "Gauss's law relates?", "back": "flux and charge"}, physics, tags=["em"])
    db.add_question_group("fill_in", {"text": "{{c1::Stokes}} and {{c2::Green}}"}, maths)
    browser = QuestionBrowser(db)
    changes = []
    browser.questionsChanged.connect(lambda: changes.append(1))

    browser.set_search("deck:Physics")
    assert len(browser.model.ids) == 1 and browser.model.data(browser.model.index(0, 0)) == "Gauss's law relates?"
    browser.set_search("")
    assert len(browser.model.ids) == 3
    browser.set_search("deck:Physics (")
    assert "expected" in browser.status.text() and len(browser.model.ids) == 3  # the last good search stays

    browser.set_search("deck:Physics")
    browser.table.selectRow(0)
    assert browser.editor.group_id == gauss and browser.editor.fields["front"].toPlainText() == "Gauss's law relates?"
    browser.editor.fields["back"].setPlainText("electric flux and enclosed charge")
    browser.save_question()
    assert json.loads(db.question_group(gauss)["fields"])["back"] == "electric flux and enclosed charge" and changes

    question_id = browser.model.ids[0]
    browser.toggle_suspend()
    assert db.question(question_id)["suspended"] == 1
    browser.set_flag(2)
    assert db.question(question_id)["flag"] == 2
    browser.set_flag(2)
    assert db.question(question_id)["flag"] == 0
    monkeypatch.setattr(browser, "_ask_tags", lambda *a: ["exam"])
    browser.add_tags()
    assert db.question_group(gauss)["tags"].split() == ["em", "exam"]
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Science::Physics", True))
    browser.rename_deck(physics)
    assert db.deck(physics)["name"] == "Science::Physics"
    browser.set_search("tag:exam")
    browser.table.selectAll()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    practice.attempt(db, question_id, 3)
    browser.forget()
    assert db.question(question_id)["stage"] == 0
    QuestionInfoDialog(db, question_id).close()

    assert find_replace(db, [gauss], "FLUX", "field") == (1, 0)
    assert json.loads(db.question_group(gauss)["fields"])["back"] == "electric field and enclosed charge"

    # Questions made from the same text: named by their place, and an edit or tags reach them all.
    browser.set_search("Stokes")
    browser.table.selectRow(1)
    ord = db.question(browser.model.ids[1])["ord"]
    assert browser.question_line.text() == f"Maths · Blank {ord + 1} of 2"
    browser.editor.fields["text"].setPlainText("{{c1::Stokes}} and {{c2::Green}}'s theorems")
    browser.save_question()
    assert "its other fill-in-the-blank question too" in browser.editor_status.text()
    asked = []
    monkeypatch.setattr(browser, "_ask_tags", lambda title, label: asked.append(label) or ["vector"])
    browser.add_tags()
    assert asked == ["Tags to add to 1 question (and 1 more made from the same text):"]
    assert browser.status.text() == "Added tags to 1 question (and 1 more made from the same text)."
    browser.table.selectAll()
    browser.delete()
    assert db.count_questions(deck_id=maths) == 0
    browser.sidebar.refresh()
    sections = {browser.sidebar.topLevelItem(i).text(0): browser.sidebar.topLevelItem(i)
                for i in range(browser.sidebar.topLevelItemCount())}
    types = sections["Types"]
    assert [types.child(i).text(0) for i in range(types.childCount())] == [
        question_model.TYPE_LABELS[t] for t in question_model.TYPES]
    assert types.child(0).data(0, Qt.ItemDataRole.UserRole) == "type:self_check"
    menus = [a.text() for a in browser.menuBar().actions()]
    assert "&Notes" not in menus and "&Questions" in menus
    browser.close()


def test_add_dialog_makes_questions(db, chat_panel):
    from study_reader.question_editor import AddDialog

    dialog = AddDialog(db)
    added = []
    dialog.added.connect(added.append)
    dialog.deck.setCurrentText("New::Deck")
    assert [dialog.type.itemData(i) for i in range(dialog.type.count())] == [
        "self_check", "both_ways", "fill_in", "choice", "truefalse", "calculation", "short_answer"]
    dialog.type.setCurrentIndex(dialog.type.findData("fill_in"))
    dialog.editor.fields["text"].setPlainText("The {{c1::mitochondria}} is the {{c2::powerhouse}}")
    dialog.editor.tags.setText("bio")
    dialog.add()
    assert len(db.group_questions(added[0])) == 2 and db.deck_by_name("New::Deck")
    assert dialog.editor.fields["text"].toPlainText() == "" and dialog.editor.tags.text() == "bio"

    dialog.type.setCurrentIndex(dialog.type.findData("choice"))
    dialog.editor.q_question.setPlainText("Pick B")
    dialog.editor.q_choices.setPlainText("A\nB\nC")
    dialog.editor.q_correct.setValue(2)
    dialog.add()
    q = json.loads(db.question_group(added[1])["fields"])
    assert q["type"] == "choice" and q["answer"] == 1 and q["choices"] == ["A", "B", "C"]
    assert db.question_group(added[1])["kind"] == "choice" and db.get_setting("add_type") == "choice"

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


def test_main_window_questions_page(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    deck = db.ensure_deck("Physics::Optics")
    db.add_question_group("self_check", {"front": "f", "back": "b"}, deck)
    win = MainWindow(db)
    win.show()
    assert win.nav.buttons["practice"].badge == "1"
    browser = win.show_browser()  # the Questions page
    assert browser is win.show_browser() and len(browser.model.ids) == 1
    assert win.center.currentWidget() is win.questions_page and browser.isVisible() and not browser.isWindow()
    assert win.nav.buttons["questions"].isChecked() and not win.web_action.isEnabled()  # its Ctrl+Shift+O wins
    browser.practiceDeckRequested.emit(deck)
    assert win.page() == "practice" and win.practice.picks == [{"deck": deck}]
    assert win.web_action.isEnabled()
    win.practice.start()
    win.practice.act(win.practice.question_id, "edit")
    assert win.page() == "questions" and browser.search_box.text().startswith("cid:")
    win.show_stats(deck)
    assert win.page() == "stats" and win.stats_window.isVisible() and not win.stats_window.isWindow()
    assert win.stats_window.scope.currentData() == ("deck", deck)
    browser.close()
    win.close()


# -- regressions ---------------------------------------------------------------------------
def test_edits_never_lose_attempted_questions_or_revive_deleted_ones(db):
    deck = db.ensure_deck("D")
    group = db.add_question_group("fill_in", {"text": "{{c1::a}} {{c2::b}} {{c3::c}}"}, deck, now=NOW)
    c1, c2, c3 = db.group_questions(group)
    practice.attempt(db, c2["id"], 3, now=NOW)
    # Retyping c2 and c3 (an autosave mid-edit has neither): the attempted question stays, the new one goes.
    db.update_question_group(group, {"text": "{{c1::a}} b c"})
    assert {c["id"] for c in db.group_questions(group)} == {c1["id"], c2["id"]}
    db.update_question_group(group, {"text": "{{c1::a}} {{c2::B}} {{c3::c}}"})
    questions = {c["ord"]: c for c in db.group_questions(group)}
    assert questions[1]["id"] == c2["id"] and "B" in questions[1]["data"] and len(db.attempts(c2["id"])) == 1 and 2 in questions

    # A deleted question isn't brought back by editing its group.
    db.delete_questions([c1["id"]])
    db.update_question_group(group, {"text": "{{c1::a}} {{c2::B}} {{c3::c}} fixed typo"})
    assert 0 not in {c["ord"] for c in db.group_questions(group)}


def test_forgotten_questions_count_as_new_and_outlive_their_chat(tmp_path):
    db, book_id, chat, reply = setup(tmp_path)
    ids = practice.add_quiz_questions(db, book_id, reply, QUIZ, now=NOW)
    db.set_setting(practice.NEW_PER_DAY_SETTING, "1")
    practice.attempt(db, ids[0], 3, now=NOW - 3 * 86400)
    practice.forget(db, [ids[0]], now=NOW)
    practice.attempt(db, ids[0], 3, now=NOW)  # introduced again today: uses up the allowance
    assert practice.new_left_today(db, NOW) == 0
    db.delete_messages_from(chat, reply)
    assert [c["id"] for c in db.questions()] == [ids[0]]


def test_removing_a_tag_removes_its_children(db):
    group = db.add_question_group("self_check", {"front": "f"}, db.ensure_deck("D"), tags=["A", "A::B", "C"])
    db.rename_tag("A", "")
    assert db.question_group(group)["tags"] == " C "


def test_browser_columns_fit_the_table(db, chat_panel):
    from study_reader.browser import COLUMNS_SETTING, DEFAULT_COLUMNS, WIDTHS_SETTING, QuestionBrowser

    db.add_question_group("self_check", {"front": "Gauss's law relates?", "back": "flux and charge"}, db.ensure_deck("Physics"))
    browser = QuestionBrowser(db)
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


def test_browser_reads_the_old_last_attempt_column_id(db, chat_panel):
    from study_reader.browser import COLUMNS_SETTING, SORT_SETTING, WIDTHS_SETTING, QuestionBrowser

    db.set_setting(COLUMNS_SETTING, json.dumps(["question", "reviewed"]))
    db.set_setting(WIDTHS_SETTING, json.dumps({"reviewed": 120}))
    db.set_setting(SORT_SETTING, "reviewed:asc")
    browser = QuestionBrowser(db)
    assert browser.model.columns == ["question", "attempted"] and browser._widths == {"attempted": 120}
    assert browser.sort == "attempted" and not browser.descending
    browser.close()


def test_clicking_a_row_shows_that_question(db, chat_panel):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from study_reader.browser import QuestionBrowser

    deck = db.ensure_deck("D")
    for i in range(4):
        db.add_question_group("self_check", {"front": f"front {i}", "back": "b"}, deck)
    browser = QuestionBrowser(db)
    browser.show()
    browser.set_search("")
    for row in (2, 0, 3):
        rect = browser.table.visualRect(browser.model.index(row, 0))
        QTest.mouseClick(browser.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        assert browser.editor.fields["front"].toPlainText() == browser.model.data(browser.model.index(row, 0))
    # Ctrl+click adds to the selection and shows the question clicked.
    rect = browser.table.visualRect(browser.model.index(1, 0))
    QTest.mouseClick(browser.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, rect.center())
    assert len(browser.selected_ids()) == 2 and browser.editor.fields["front"].toPlainText() == browser.model.data(browser.model.index(1, 0))
    browser.close()


def test_switching_a_questions_type_in_the_browser(db, chat_panel):
    from study_reader.browser import QuestionBrowser

    deck = db.ensure_deck("D")
    short = db.add_question_group("short_answer", {"type": "short_answer", "question": "Rinse time in seconds?", "answer": "41"}, deck)
    choice = db.add_question_group("choice", {"type": "choice", "question": "Pick", "choices": ["tea", "milk"], "answer": 1}, deck)
    browser = QuestionBrowser(db)
    browser.show()
    browser.set_search("")
    e = browser.editor
    fields = lambda group: json.loads(db.question_group(group)["fields"])

    def show(group):
        browser.table.selectRow(next(r for r, c in enumerate(browser.model.ids) if db.question(c)["group_id"] == group))
        assert e.group_id == group

    def switch(t):
        e.switch_type(t)
        browser.save_question()
        assert e.type_box.currentData() == t

    kind = lambda group: db.question_group(group)["kind"]
    # The answer comes along, so the new type saves at once.
    show(short)
    switch("calculation")
    assert kind(short) == fields(short)["type"] == "calculation" and fields(short)["answer"] == 41
    switch("short_answer")
    assert kind(short) == fields(short)["type"] == "short_answer" and fields(short)["answer"] == "41"
    switch("self_check")  # the question and answer are its front and back
    assert kind(short) == "self_check" and fields(short) == {"front": "Rinse time in seconds?", "back": "41", "extra": ""}
    assert json.loads(db.group_questions(short)[0]["data"])["type"] == "self_check"
    switch("both_ways")
    assert len(db.group_questions(short)) == 2 and "both sides" in browser.editor_status.text()
    switch("short_answer")
    assert kind(short) == "short_answer" and fields(short)["question"] == "Rinse time in seconds?"
    assert len(db.group_questions(short)) == 1  # the reverse side, never attempted, goes
    show(choice)
    switch("short_answer")
    assert fields(choice)["answer"] == "milk"
    switch("truefalse")
    switch("choice")
    assert fields(choice)["choices"] == ["tea", "milk"]  # what was entered is still there

    # A type whose answer is missing isn't saved yet, but the switch isn't lost on leaving the question.
    show(short)
    e.q_answer.setPlainText("Because tea.")
    switch("choice")
    assert "Not saved" in browser.editor_status.text() and fields(short)["type"] == "short_answer"
    show(choice)
    show(short)
    assert e.type_box.currentData() == "choice" and "Not saved" in browser.editor_status.text()
    assert e.q_choices.toPlainText().strip() == "Because tea."
    e.q_choices.setPlainText("Because tea.\nBecause milk.")
    browser.save_question()
    assert fields(short)["type"] == "choice" and fields(short)["choices"] == ["Because tea.", "Because milk."]
    show(choice)
    show(short)
    assert browser.editor_status.text() == "" and e.type_box.currentData() == "choice"
    browser.close()
