from study_reader import srs, stats

NOW = 1_800_000_000.0
DAY = 86400
TODAY = srs.day_start(NOW) + 3600  # an hour into today's review day, before NOW


def card(db, deck, front="Q", book_id=None, **schedule):
    note = db.add_card_note("basic", {"front": front, "back": "A"}, db.ensure_deck(deck), book_id=book_id, now=NOW - 40 * DAY)
    (c,) = db.note_cards(note)
    if schedule:
        db.update_card(c["id"], **schedule)
    return c["id"]


def review(db, card_id, rating, at, duration=0):
    db.record_review(card_id, rating, "review", "", None, at, duration)


def test_empty(db):
    s = stats.compute(db, now=NOW)
    assert s["counts"]["total"] == 0 and s["today"]["reviews"] == 0 and s["today"]["pass_rate"] is None
    assert s["calendar"]["streak"] == 0 and s["calendar"]["days"] == [] and s["estimated"]["average_retrievability"] is None
    assert len(s["forecast"]) == 30 and len(s["reviews"]) == 30 and len(s["added"]) == 30
    assert s["retention"]["month"]["all"] == {"passed": 0, "total": 0, "rate": None}


def test_counts_forecast_and_distributions(db):
    card(db, "A")  # new
    card(db, "A", stage=1, due=NOW + 600, last_review=NOW - 60, stability=0.5, difficulty=5)
    card(db, "A", stage=3, due=NOW - 100, last_review=NOW - 3 * DAY, stability=2, difficulty=8)
    card(db, "A", stage=2, due=NOW + 5 * DAY, last_review=NOW - 5 * DAY, stability=10, difficulty=3)  # young: 10 d
    card(db, "A", stage=2, due=TODAY - 10 * DAY, last_review=TODAY - 40 * DAY, stability=40, difficulty=2.5)  # mature, overdue
    card(db, "A", stage=2, due=NOW + 100 * DAY, last_review=NOW - DAY, stability=90, suspended=1)
    s = stats.compute(db, now=NOW)
    assert s["counts"] == {"new": 1, "learning": 1, "relearning": 1, "young": 1, "mature": 1, "suspended": 1, "total": 6}
    f = [d["count"] for d in s["forecast"]]
    assert f[0] == 3 and f[5] == 1 and sum(f) == 4  # learning + relearning + overdue today; young in 5 days
    assert sum(b["count"] for b in s["distributions"]["interval"]) == 3  # review and relearning cards
    assert sum(b["count"] for b in s["distributions"]["stability"]) == 4
    assert [b["count"] for b in s["distributions"]["difficulty"]][1:3] == [1, 1]  # 2.5 -> "2–3", 3 -> "3–4"
    est = s["estimated"]
    assert est["scheduled"] == 4 and 0 < est["average_retrievability"] < 1
    assert s["added"][-11]["count"] == 0 and sum(d["count"] for d in s["added"]) == 0  # all added 40 days ago


def test_today_reviews_and_retention(db):
    a = card(db, "A")
    b = card(db, "A")
    # a: first review 30 days ago, then young review 25 days ago (pass), mature review today (fail).
    review(db, a, 3, TODAY - 30 * DAY, 4000)
    review(db, a, 3, TODAY - 25 * DAY, 2000)
    review(db, a, 1, TODAY + 60, 60000)
    # b: first review today, and again the same day (not counted for retention).
    review(db, b, 1, TODAY + 120, 3000)
    review(db, b, 3, TODAY + 600, 3000)
    s = stats.compute(db, now=NOW)
    t = s["today"]
    assert (t["reviews"], t["again"], t["passed"], t["new"]) == (3, 2, 1, 1)
    assert t["minutes"] == 1.1 and abs(t["pass_rate"] - 1 / 3) < 1e-9
    r = s["retention"]
    assert r["today"]["mature"] == {"passed": 0, "total": 1, "rate": 0.0} and r["today"]["young"]["total"] == 0
    assert r["month"]["young"] == {"passed": 1, "total": 1, "rate": 1.0} and r["month"]["all"]["total"] == 2
    assert r["week"]["all"]["total"] == 1
    last = s["reviews"][-1]
    assert (last["first"], last["again"], last["passed"]) == (1, 1, 1) and last["minutes"] == 1.1
    assert s["reviews"][-26]["passed"] == 1 and s["settings"]["total_reviews"] == 5


def test_streaks(db):
    a = card(db, "A")
    for days_ago in (0, 1, 2, 5, 6, 7, 8, 30):
        review(db, a, 3, TODAY - days_ago * DAY)
    cal = stats.compute(db, now=NOW)["calendar"]
    assert cal["streak"] == 3 and cal["longest"] == 4 and cal["studied"] == 8 and cal["span"] == 31
    assert cal["end"] == stats.day_of(NOW).isoformat() and len(cal["days"]) == 8
    # Not reviewed yet today: the streak still counts up to yesterday.
    db.conn.execute("DELETE FROM reviews WHERE reviewed_at>=?", (srs.day_start(NOW),))
    assert stats.compute(db, now=NOW)["calendar"]["streak"] == 2


def test_scope_by_deck_and_book(db):
    book = db.upsert_book("h", "/a.pdf", "A book", 3)["id"]
    a = card(db, "Math::Calc")
    card(db, "Math")
    card(db, "Physics")
    card(db, "Physics", book_id=book)
    review(db, a, 3, TODAY)
    math = db.deck_by_name("Math")["id"]
    assert stats.compute(db, deck_id=math, now=NOW)["counts"]["total"] == 2
    assert stats.compute(db, deck_id=db.deck_by_name("Math::Calc")["id"], now=NOW)["today"]["reviews"] == 1
    assert stats.compute(db, deck_id=db.deck_by_name("Physics")["id"], now=NOW)["today"]["reviews"] == 0
    assert stats.compute(db, book_id=book, now=NOW)["counts"]["total"] == 1
    assert stats.compute(db, now=NOW)["counts"]["total"] == 4


def test_stats_window(db, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.stats_view import StatsWindow

    QApplication.instance() or QApplication([])
    book = db.upsert_book("h", "/a.pdf", "A book", 3)["id"]
    card(db, "Math::Calc", book_id=book)
    win = StatsWindow(db)
    calls = []
    win._js = lambda fn, *args: calls.append((fn, args))
    labels = [win.scope.itemText(i) for i in range(win.scope.count())]
    assert labels == ["All decks", "Math", "    Calc", "Book: A book"]
    win.show_scope(deck_id=db.deck_by_name("Math")["id"])
    assert win.scope.currentText() == "Math" and calls[-1][0] == "showStats" and calls[-1][1][0]["counts"]["total"] == 1
    win.show_scope(book_id=book)
    assert win.scope.currentText() == "Book: A book"
    win.show_scope()
    win.refresh()
    assert win.scope.currentIndex() == 0 and calls[-1][1][0]["counts"]["total"] == 1
    win.close()
