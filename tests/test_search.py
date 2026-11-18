import pytest

from study_reader import search, practice
from study_reader.db import Database

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def deck(tmp_path):
    """A small collection: {name: question id} for each question, and the db."""
    db = Database(tmp_path / "t.db")
    physics = db.ensure_deck("Physics::Electrostatics")
    math = db.ensure_deck("Mathematics::MATH 253 Calculus III")
    book_id = db.upsert_book("h", "/a.pdf", "The Zorblatt Manual", 3)["id"]
    questions = {}

    def add(name, kind, fields, deck_id, tags=(), book=None, created=NOW - 10 * DAY):
        group = db.add_question_group(kind, fields, deck_id, tags=tags, book_id=book, now=created)
        for q in db.group_questions(group):
            questions[f"{name}{q['ord'] or ''}"] = q["id"]

    add("coulomb", "self_check", {"front": "Coulomb's law", "back": "F = kq1q2/r^2"}, physics,
        tags=["physics::em", "exam"])
    add("gauss", "fill_in", {"text": "Gauss: flux = {{c1::Q/eps0}} and {{c2::closed surface}}"}, physics,
        tags=["physics"])
    add("curl", "both_ways", {"front": "curl of a gradient", "back": "zero"}, math, tags=["Calc_3"], created=NOW)
    add("zorb", "choice", {"type": "choice", "question": "How many legs has a zorblatt?", "choices": ["5", "7"],
                         "answer": 1}, math, book=book_id)
    add("star", "self_check", {"front": "What is 5*3 in 100% terms?", "back": "15"}, math)
    return db, questions, book_id


def find(db, query, now=NOW):
    return set(search.question_ids(db, query, now=now))


def ids(questions, *names):
    return {questions[n] for n in names}


def test_empty_and_text(deck):
    db, questions, _ = deck
    assert search.compile("") == ("1", []) and find(db, "") == set(questions.values())
    assert find(db, "coulomb") == ids(questions, "coulomb")
    assert find(db, "COULOMB") == ids(questions, "coulomb")  # case-insensitive
    assert find(db, "gau*flux") == ids(questions, "gauss", "gauss1")
    assert find(db, "z_ro") == ids(questions, "curl", "curl1")  # _ is one character
    assert find(db, r"5\*3") == ids(questions, "star")
    assert find(db, "100%") == ids(questions, "star") and find(db, "1%0") == set()
    assert find(db, '"closed surface"') == ids(questions, "gauss", "gauss1")
    assert find(db, "closed surface") == ids(questions, "gauss", "gauss1")


def test_boolean_logic(deck):
    db, questions, _ = deck
    assert find(db, "coulomb OR curl") == ids(questions, "coulomb", "curl", "curl1")
    assert find(db, "coulomb or curl") == find(db, "coulomb OR curl")
    assert find(db, "-coulomb") == set(questions.values()) - ids(questions, "coulomb")
    assert find(db, "deck:physics -(coulomb or flux)") == set()
    assert find(db, "(coulomb or curl) deck:math*") == ids(questions, "curl", "curl1")
    assert find(db, "coulomb and law") == ids(questions, "coulomb")
    assert find(db, '"or"') == ids(questions, "zorb")  # a quoted "or" is a word (in "zorblatt")


def test_deck_and_tag(deck):
    db, questions, _ = deck
    physics = ids(questions, "coulomb", "gauss", "gauss1")
    assert find(db, "deck:Physics") == physics  # includes subdecks
    assert find(db, "deck:physics::electrostatics") == physics
    assert find(db, "deck:phys") == set() and find(db, "deck:phys*") == physics
    assert find(db, 'deck:"Mathematics::MATH 253 Calculus III"') == set(questions.values()) - physics
    assert find(db, '"deck:mathematics::math 253 calculus iii"') == set(questions.values()) - physics
    assert find(db, "deck:*") == set(questions.values())
    assert find(db, "tag:physics") == physics  # and its children
    assert find(db, "tag:physics::em") == ids(questions, "coulomb")
    assert find(db, "tag:phys") == set() and find(db, "tag:ph*s") == physics
    assert find(db, "tag:calc_3") == ids(questions, "curl", "curl1")
    assert find(db, "tag:none") == ids(questions, "zorb", "star")
    assert find(db, "-tag:exam tag:physics") == ids(questions, "gauss", "gauss1")


def test_kinds_types_books_flags(deck):
    db, questions, book_id = deck
    assert find(db, "type:fill_in") == ids(questions, "gauss", "gauss1")
    assert find(db, "type:self_check") == ids(questions, "coulomb", "star")  # one-sided only
    assert find(db, "type:both_ways") == ids(questions, "curl", "curl1")
    assert find(db, "type:choice") == ids(questions, "zorb") and find(db, "type:short_answer") == set()
    assert find(db, "type:self-check") == find(db, "type:self_check") and find(db, "type:fill-in") == find(db, "type:fill_in")
    # Older names still work: note: for type:, the names types had (flashcard, reversed, cloze,
    # occlusion, numeric, written), the kinds basic, reverse and quiz before them, and free.
    assert find(db, "type:cloze") == find(db, "note:cloze") == find(db, "type:fill_in")
    assert find(db, "type:flashcard") == find(db, "type:self_check") and find(db, "type:reversed") == find(db, "type:both_ways")
    assert find(db, "type:occlusion") == find(db, "type:numeric") == find(db, "type:written") == set()
    assert find(db, "note:basic") == find(db, "type:self_check") and find(db, "note:reverse") == find(db, "type:both_ways")
    assert find(db, "note:quiz") == ids(questions, "zorb") and find(db, "type:free") == set()
    assert find(db, "type:self_check or type:both_ways") == ids(questions, "coulomb", "star", "curl", "curl1")
    assert find(db, "book:zorblatt") == ids(questions, "zorb") and find(db, "book:any") == ids(questions, "zorb")
    assert find(db, "book:none") == set(questions.values()) - ids(questions, "zorb")
    assert find(db, "-book:zorblatt") == set(questions.values()) - ids(questions, "zorb")
    db.update_question(questions["star"], flag=3)
    assert find(db, "flag:3") == ids(questions, "star")
    assert find(db, "-flag:0") == ids(questions, "star")
    assert find(db, f"cid:{questions['star']},{questions['zorb']}") == ids(questions, "star", "zorb")
    group = db.question(questions["gauss"])["group_id"]
    assert find(db, f"nid:{group}") == ids(questions, "gauss", "gauss1")
    assert find(db, f"qid:{questions['star']}") == ids(questions, "star")  # cid: and nid: are the old names
    assert find(db, f"gid:{group}") == ids(questions, "gauss", "gauss1")


def test_states_and_due(deck):
    db, questions, _ = deck
    db.update_question(questions["coulomb"], stage=2, due=NOW + 3 * DAY, last_attempt=NOW - 7 * DAY, stability=12.0,
                   difficulty=5.5, reps=4, lapses=1)
    db.update_question(questions["gauss"], stage=2, due=NOW - DAY, last_attempt=NOW - 60 * DAY, stability=40.0,
                   difficulty=1.0, reps=2)
    db.update_question(questions["curl"], stage=1, due=NOW - 60, last_attempt=NOW - 600, stability=0.5, difficulty=8.0,
                   reps=1)
    db.update_question(questions["star"], suspended=1)
    assert find(db, "is:new") == ids(questions, "gauss1", "curl1", "zorb", "star")
    assert find(db, "is:learned") == find(db, "is:review") == ids(questions, "coulomb", "gauss")  # review: the old name
    assert find(db, "is:learn") == ids(questions, "curl")
    assert find(db, "is:due") == ids(questions, "gauss", "curl")
    assert find(db, "is:suspended") == ids(questions, "star")
    assert find(db, "prop:ivl=10") == ids(questions, "coulomb")
    assert find(db, "prop:ivl>=29") == ids(questions, "gauss")
    assert find(db, "prop:due=3") == ids(questions, "coulomb")
    assert find(db, "prop:due<0") == ids(questions, "gauss")  # curl is due earlier today: day 0
    assert find(db, "prop:due<=0") == ids(questions, "gauss", "curl")
    assert find(db, "prop:due>0") == ids(questions, "coulomb")
    assert find(db, "prop:due!=3") == ids(questions, "gauss", "curl")
    assert find(db, "prop:reps>1") == ids(questions, "coulomb", "gauss")
    assert find(db, "prop:lapses=1") == ids(questions, "coulomb")
    assert find(db, "prop:s>10") == ids(questions, "coulomb", "gauss")
    assert find(db, "prop:d<0.1") == ids(questions, "gauss")  # Anki's 0..1 scale: (difficulty - 1) / 9
    assert find(db, "prop:d>0.5") == ids(questions, "curl")
    # Retrievability is 0.9 when the time since the last attempt equals the stability:
    # coulomb is 7 days into 12, gauss 60 into 40.
    assert find(db, "prop:r>0.9") == ids(questions, "coulomb", "curl")
    assert find(db, "prop:r<0.9") == ids(questions, "gauss")


def test_dates(deck):
    db, questions, _ = deck
    db.record_attempt(questions["coulomb"], 1, "practice", "", None, NOW - 60)
    db.record_attempt(questions["gauss"], 3, "practice", "", None, NOW - 5 * DAY)
    db.record_attempt(questions["gauss"], 3, "practice", "", None, NOW - 60)
    assert find(db, "added:1") == ids(questions, "curl", "curl1")
    assert find(db, "added:11") == set(questions.values())
    assert find(db, "rated:1") == ids(questions, "coulomb", "gauss")
    assert find(db, "rated:1:1") == ids(questions, "coulomb")
    assert find(db, "rated:7:3") == ids(questions, "gauss")
    assert find(db, "introduced:1") == ids(questions, "coulomb")
    assert find(db, "introduced:7") == ids(questions, "coulomb", "gauss")
    db.update_question_group(db.question(questions["star"])["group_id"], tags=["fresh"], now=NOW)
    assert find(db, "edited:1") == ids(questions, "curl", "curl1", "star")


def test_regex(deck):
    db, questions, _ = deck
    assert find(db, r"re:legs?\b") == ids(questions, "zorb")
    assert find(db, "re:^coulomb") == ids(questions, "coulomb")


def test_sorting(deck):
    db, questions, _ = deck
    db.update_question(questions["coulomb"], stage=2, due=NOW + 3 * DAY, last_attempt=NOW - DAY, stability=3.0)
    db.update_question(questions["gauss"], stage=2, due=NOW + DAY, last_attempt=NOW - DAY, stability=30.0)
    by_due = search.question_ids(db, "", sort="due", now=NOW)
    assert by_due[:2] == [questions["gauss"], questions["coulomb"]]  # scheduled first, then new by when added
    assert by_due[-2:] == [questions["curl"], questions["curl1"]]
    assert search.question_ids(db, "deck:physics", sort="question", now=NOW)[0] == questions["coulomb"]
    assert search.question_ids(db, "", sort="added", descending=True, now=NOW)[0] == questions["curl1"]
    by_r = search.question_ids(db, "is:learned", sort="retrievability", now=NOW)
    assert by_r == [questions["coulomb"], questions["gauss"]]  # lower stability, lower retrievability
    for key in search.SORTS:
        assert len(search.question_ids(db, "", sort=key, now=NOW)) == len(questions)


def test_errors(deck):
    db, _, _ = deck
    for bad in ("(coulomb", "coulomb)", '"open', "foo:bar", "is:buried", "flag:9", "prop:ivl", "prop:x>1",
                "note:fancy", "type:essay", "added:0", "rated:1:5", "cid:a", "re:(", "()", "coulomb or", "a OR OR b"):
        with pytest.raises(search.SearchError):
            search.compile(bad, NOW)
    with pytest.raises(search.SearchError, match="foo"):
        search.compile("foo:bar", NOW)


def test_retrievability_function(deck):
    db, _, _ = deck
    decay = practice.parameters(db)[20]
    search.register_functions(db.conn, decay)
    # By definition R = 0.9 when t equals the stability.
    r = db.conn.execute("SELECT retrievability(10.0, ?, ?)", (NOW - 10 * DAY, NOW)).fetchone()[0]
    assert abs(r - 0.9) < 1e-9
    assert db.conn.execute("SELECT retrievability(NULL, 1, 2)").fetchone()[0] is None
