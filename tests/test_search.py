import pytest

from study_reader import search, srs
from study_reader.db import Database

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def deck(tmp_path):
    """A small collection: {name: card id} for each card, and the db."""
    db = Database(tmp_path / "t.db")
    physics = db.ensure_deck("Physics::Electrostatics")
    math = db.ensure_deck("Mathematics::MATH 253 Calculus III")
    book_id = db.upsert_book("h", "/a.pdf", "The Zorblatt Manual", 3)["id"]
    cards = {}

    def add(name, kind, fields, deck_id, tags=(), book=None, created=NOW - 10 * DAY):
        note = db.add_card_note(kind, fields, deck_id, tags=tags, book_id=book, now=created)
        for c in db.note_cards(note):
            cards[f"{name}{c['ord'] or ''}"] = c["id"]

    add("coulomb", "basic", {"front": "Coulomb's law", "back": "F = kq1q2/r^2"}, physics,
        tags=["physics::em", "exam"])
    add("gauss", "cloze", {"text": "Gauss: flux = {{c1::Q/eps0}} and {{c2::closed surface}}"}, physics,
        tags=["physics"])
    add("curl", "reverse", {"front": "curl of a gradient", "back": "zero"}, math, tags=["Calc_3"], created=NOW)
    add("zorb", "quiz", {"type": "choice", "question": "How many legs has a zorblatt?", "choices": ["5", "7"],
                         "answer": 1}, math, book=book_id)
    add("star", "basic", {"front": "What is 5*3 in 100% terms?", "back": "15"}, math)
    return db, cards, book_id


def find(db, query, now=NOW):
    return set(search.card_ids(db, query, now=now))


def ids(cards, *names):
    return {cards[n] for n in names}


def test_empty_and_text(deck):
    db, cards, _ = deck
    assert search.compile("") == ("1", []) and find(db, "") == set(cards.values())
    assert find(db, "coulomb") == ids(cards, "coulomb")
    assert find(db, "COULOMB") == ids(cards, "coulomb")  # case-insensitive
    assert find(db, "gau*flux") == ids(cards, "gauss", "gauss1")
    assert find(db, "z_ro") == ids(cards, "curl", "curl1")  # _ is one character
    assert find(db, r"5\*3") == ids(cards, "star")
    assert find(db, "100%") == ids(cards, "star") and find(db, "1%0") == set()
    assert find(db, '"closed surface"') == ids(cards, "gauss", "gauss1")
    assert find(db, "closed surface") == ids(cards, "gauss", "gauss1")


def test_boolean_logic(deck):
    db, cards, _ = deck
    assert find(db, "coulomb OR curl") == ids(cards, "coulomb", "curl", "curl1")
    assert find(db, "coulomb or curl") == find(db, "coulomb OR curl")
    assert find(db, "-coulomb") == set(cards.values()) - ids(cards, "coulomb")
    assert find(db, "deck:physics -(coulomb or flux)") == set()
    assert find(db, "(coulomb or curl) deck:math*") == ids(cards, "curl", "curl1")
    assert find(db, "coulomb and law") == ids(cards, "coulomb")
    assert find(db, '"or"') == ids(cards, "zorb")  # a quoted "or" is a word (in "zorblatt")


def test_deck_and_tag(deck):
    db, cards, _ = deck
    physics = ids(cards, "coulomb", "gauss", "gauss1")
    assert find(db, "deck:Physics") == physics  # includes subdecks
    assert find(db, "deck:physics::electrostatics") == physics
    assert find(db, "deck:phys") == set() and find(db, "deck:phys*") == physics
    assert find(db, 'deck:"Mathematics::MATH 253 Calculus III"') == set(cards.values()) - physics
    assert find(db, '"deck:mathematics::math 253 calculus iii"') == set(cards.values()) - physics
    assert find(db, "deck:*") == set(cards.values())
    assert find(db, "tag:physics") == physics  # and its children
    assert find(db, "tag:physics::em") == ids(cards, "coulomb")
    assert find(db, "tag:phys") == set() and find(db, "tag:ph*s") == physics
    assert find(db, "tag:calc_3") == ids(cards, "curl", "curl1")
    assert find(db, "tag:none") == ids(cards, "zorb", "star")
    assert find(db, "-tag:exam tag:physics") == ids(cards, "gauss", "gauss1")


def test_kinds_types_books_flags(deck):
    db, cards, book_id = deck
    assert find(db, "note:cloze") == ids(cards, "gauss", "gauss1")
    assert find(db, "type:flashcard") == ids(cards, "coulomb", "curl", "curl1", "star")
    assert find(db, "type:choice") == ids(cards, "zorb")
    assert find(db, "book:zorblatt") == ids(cards, "zorb") and find(db, "book:any") == ids(cards, "zorb")
    assert find(db, "book:none") == set(cards.values()) - ids(cards, "zorb")
    assert find(db, "-book:zorblatt") == set(cards.values()) - ids(cards, "zorb")
    db.update_card(cards["star"], flag=3)
    assert find(db, "flag:3") == ids(cards, "star")
    assert find(db, "-flag:0") == ids(cards, "star")
    assert find(db, f"cid:{cards['star']},{cards['zorb']}") == ids(cards, "star", "zorb")
    note = db.card(cards["gauss"])["note_id"]
    assert find(db, f"nid:{note}") == ids(cards, "gauss", "gauss1")


def test_states_and_due(deck):
    db, cards, _ = deck
    db.update_card(cards["coulomb"], stage=2, due=NOW + 3 * DAY, last_review=NOW - 7 * DAY, stability=12.0,
                   difficulty=5.5, reps=4, lapses=1)
    db.update_card(cards["gauss"], stage=2, due=NOW - DAY, last_review=NOW - 60 * DAY, stability=40.0,
                   difficulty=1.0, reps=2)
    db.update_card(cards["curl"], stage=1, due=NOW - 60, last_review=NOW - 600, stability=0.5, difficulty=8.0,
                   reps=1)
    db.update_card(cards["star"], suspended=1)
    assert find(db, "is:new") == ids(cards, "gauss1", "curl1", "zorb", "star")
    assert find(db, "is:review") == ids(cards, "coulomb", "gauss")
    assert find(db, "is:learn") == ids(cards, "curl")
    assert find(db, "is:due") == ids(cards, "gauss", "curl")
    assert find(db, "is:suspended") == ids(cards, "star")
    assert find(db, "prop:ivl=10") == ids(cards, "coulomb")
    assert find(db, "prop:ivl>=29") == ids(cards, "gauss")
    assert find(db, "prop:due=3") == ids(cards, "coulomb")
    assert find(db, "prop:due<0") == ids(cards, "gauss")  # curl is due earlier today: day 0
    assert find(db, "prop:due<=0") == ids(cards, "gauss", "curl")
    assert find(db, "prop:due>0") == ids(cards, "coulomb")
    assert find(db, "prop:due!=3") == ids(cards, "gauss", "curl")
    assert find(db, "prop:reps>1") == ids(cards, "coulomb", "gauss")
    assert find(db, "prop:lapses=1") == ids(cards, "coulomb")
    assert find(db, "prop:s>10") == ids(cards, "coulomb", "gauss")
    assert find(db, "prop:d<0.1") == ids(cards, "gauss")  # Anki's 0..1 scale: (difficulty - 1) / 9
    assert find(db, "prop:d>0.5") == ids(cards, "curl")
    # Retrievability is 0.9 when the time since the last review equals the stability:
    # coulomb is 7 days into 12, gauss 60 into 40.
    assert find(db, "prop:r>0.9") == ids(cards, "coulomb", "curl")
    assert find(db, "prop:r<0.9") == ids(cards, "gauss")


def test_dates(deck):
    db, cards, _ = deck
    db.record_review(cards["coulomb"], 1, "review", "", None, NOW - 60)
    db.record_review(cards["gauss"], 3, "review", "", None, NOW - 5 * DAY)
    db.record_review(cards["gauss"], 3, "review", "", None, NOW - 60)
    assert find(db, "added:1") == ids(cards, "curl", "curl1")
    assert find(db, "added:11") == set(cards.values())
    assert find(db, "rated:1") == ids(cards, "coulomb", "gauss")
    assert find(db, "rated:1:1") == ids(cards, "coulomb")
    assert find(db, "rated:7:3") == ids(cards, "gauss")
    assert find(db, "introduced:1") == ids(cards, "coulomb")
    assert find(db, "introduced:7") == ids(cards, "coulomb", "gauss")
    db.update_card_note(db.card(cards["star"])["note_id"], tags=["fresh"], now=NOW)
    assert find(db, "edited:1") == ids(cards, "curl", "curl1", "star")


def test_regex(deck):
    db, cards, _ = deck
    assert find(db, r"re:legs?\b") == ids(cards, "zorb")
    assert find(db, "re:^coulomb") == ids(cards, "coulomb")


def test_sorting(deck):
    db, cards, _ = deck
    db.update_card(cards["coulomb"], stage=2, due=NOW + 3 * DAY, last_review=NOW - DAY, stability=3.0)
    db.update_card(cards["gauss"], stage=2, due=NOW + DAY, last_review=NOW - DAY, stability=30.0)
    by_due = search.card_ids(db, "", sort="due", now=NOW)
    assert by_due[:2] == [cards["gauss"], cards["coulomb"]]  # scheduled first, then new by when added
    assert by_due[-2:] == [cards["curl"], cards["curl1"]]
    assert search.card_ids(db, "deck:physics", sort="question", now=NOW)[0] == cards["coulomb"]
    assert search.card_ids(db, "", sort="added", descending=True, now=NOW)[0] == cards["curl1"]
    by_r = search.card_ids(db, "is:review", sort="retrievability", now=NOW)
    assert by_r == [cards["coulomb"], cards["gauss"]]  # lower stability, lower retrievability
    for key in search.SORTS:
        assert len(search.card_ids(db, "", sort=key, now=NOW)) == len(cards)


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
    decay = srs.parameters(db)[20]
    search.register_functions(db.conn, decay)
    # By definition R = 0.9 when t equals the stability.
    r = db.conn.execute("SELECT retrievability(10.0, ?, ?)", (NOW - 10 * DAY, NOW)).fetchone()[0]
    assert abs(r - 0.9) < 1e-9
    assert db.conn.execute("SELECT retrievability(NULL, 1, 2)").fetchone()[0] is None
