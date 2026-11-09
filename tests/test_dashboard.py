import time
from datetime import datetime

from study_reader import covers, dashboard, srs
from study_reader.db import Database

NOW = datetime(2026, 11, 2, 15, 0).timestamp()  # a Monday afternoon


def _card(db, deck, book_id=None, due=None):
    """A basic note's card; with `due`, a review card falling due then."""
    note = db.add_card_note("flashcard", {"front": "q", "back": "a"}, deck, book_id=book_id, now=NOW - 86400 * 30)
    card_id = db.note_cards(note)[0]["id"]
    if due is not None:
        db.update_card(card_id, stage=2, due=due, last_review=NOW - 86400)
    return card_id


def test_counts_per_book_and_deck(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h1", str(tmp_path / "a.pdf"), "Optics", 10)["id"]
    own = db.book_deck(book)
    _card(db, own, book, due=NOW - 3600)  # due
    _card(db, own, book)  # new
    _card(db, own, book, due=NOW + 86400 * 3)  # later
    physics = db.ensure_deck("Physics::Waves")
    _card(db, physics, due=NOW - 60)

    assert dashboard.book_counts(db, NOW) == {book: {"due": 1, "new": 1, "total": 3}}
    rows = {r["path"]: r for r in dashboard.deck_rows(db, NOW)}
    # A book's own deck has the book's list instead; user decks show with their parents.
    assert set(rows) == {"Physics", "Physics::Waves"}
    assert rows["Physics::Waves"]["parent"] == rows["Physics"]["id"] and rows["Physics"]["due"] == 1

    db.set_setting(srs.NEW_PER_DAY_SETTING, "0")  # today's allowance caps new cards
    assert dashboard.book_counts(db, NOW)[book]["new"] == 0


def test_reading_lists_unfinished_books_newest_first(tmp_path):
    db = Database(tmp_path / "t.db")
    ids = [db.upsert_book(f"h{i}", f"/x/{i}.pdf", f"Book {i}", 100)["id"] for i in range(6)]
    for i, book_id in enumerate(ids):
        db.update_book(book_id, opened_at=NOW - 1000 * (6 - i), last_page=49)
    db.set_finished(ids[5], True)
    reading = dashboard.reading(db, NOW)
    assert [b["id"] for b in reading] == [ids[4], ids[3], ids[2], ids[1]]
    assert reading[0]["page"] == 50 and reading[0]["progress"] == 0.5 and reading[0]["due"] == 0


def test_forecast_and_heat(tmp_path):
    db = Database(tmp_path / "t.db")
    deck = db.ensure_deck("D")
    _card(db, deck, due=NOW - 60)  # due today: not in the forecast
    tomorrow = srs.next_day(NOW) + 3600 * 10
    _card(db, deck, due=tomorrow)
    _card(db, deck, due=tomorrow + 86400 * 2)
    late = _card(db, deck, due=tomorrow + 86400 * 30)  # beyond it
    forecast = dashboard.forecast(db, NOW)
    assert len(forecast) == dashboard.FORECAST_DAYS and forecast[0]["date"] == "2026-11-03"
    assert [f["count"] for f in forecast[:4]] == [1, 0, 1, 0] and sum(f["count"] for f in forecast) == 2

    db.record_review(late, 3, "review", "", None, NOW - 3600)
    db.record_review(late, 3, "review", "", None, NOW - 86400 * 400)  # too long ago
    heat = dashboard.heat(db, NOW)
    assert heat["end"] == "2026-11-02" and heat["total"] == 1 and heat["active"] == 1

    home = dashboard.home(db, NOW)
    assert home["greeting"] == "Good afternoon" and home["due"] == 1 and home["ready"]
    assert home["date"] == "Monday, 2 November"


def test_covers_draw_the_first_page(pdf_path, epub_path, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    path = covers.cover({"hash": "abc", "path": pdf_path})
    assert path and path.exists() and path.read_bytes()[:4] == b"\x89PNG"
    assert covers.cached("abc") == path
    assert covers.cover({"hash": "epub", "path": epub_path})
    assert covers.cover({"hash": "gone", "path": str(tmp_path / "missing.pdf")}) is None
    assert covers.cached("gone") is None
