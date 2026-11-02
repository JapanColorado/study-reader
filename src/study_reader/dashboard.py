"""What the Home page, the Library and the Review page show about books and decks (Qt-free):
review counts per book and per deck, the books being read, recent review activity and the
reviews coming up."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from . import gamify, srs
from .db import Database
from .stats import day_of

READING_SHOWN = 4  # books on Home's "Continue reading"
FORECAST_DAYS = 14  # days ahead on Home's forecast (from tomorrow)
HEAT_WEEKS = 26  # weeks of review activity on Home


def book_counts(db: Database, now: float | None = None) -> dict[int, dict]:
    """For every book with cards: {book id: {due, new, total}} (new: what today's allowance
    leaves, as in srs.counts)."""
    now = time.time() if now is None else now
    left = srs.new_left_today(db, now)
    rows = db.conn.execute(
        f"SELECT book_id, SUM({srs.DUE}) AS due, SUM({srs.NEW}) AS new, COUNT(*) AS total "
        "FROM cards WHERE book_id IS NOT NULL GROUP BY book_id",
        (srs.next_day(now), now),
    )
    return {r["book_id"]: {"due": r["due"] or 0, "new": min(r["new"] or 0, left), "total": r["total"]} for r in rows}


def deck_rows(db: Database, now: float | None = None) -> list[dict]:
    """The decks to pick from for review, parents before their children: each with its name, full
    path, parent deck id and counts (its subdecks' included). Books' own decks are left out (books
    have their own list) unless other decks sit under them; so are decks without cards."""
    now = time.time() if now is None else now
    counts = srs.deck_counts(db, now)
    book_decks = {r[0] for r in db.conn.execute("SELECT deck_id FROM books WHERE deck_id IS NOT NULL")}
    all_decks = [d for d in db.decks() if counts[d["id"]]["total"]]
    shown = {d["name"] for d in all_decks if d["id"] not in book_decks}
    keep = {"::".join(n.split("::")[:i]) for n in shown for i in range(1, n.count("::") + 2)}
    decks = [d for d in all_decks if d["name"] in keep]
    by_name = {d["name"]: d["id"] for d in decks}
    left = srs.new_left_today(db, now)
    return [{
        "id": d["id"], "name": d["name"].rsplit("::", 1)[-1], "path": d["name"],
        "parent": by_name.get(d["name"].rsplit("::", 1)[0]) if "::" in d["name"] else None,
        "due": counts[d["id"]]["due"], "new": min(counts[d["id"]]["new"], left), "total": counts[d["id"]]["total"],
    } for d in decks]


def progress(book) -> float:
    """How far through a book its reader is, 0..1."""
    if book["finished_at"] is not None:
        return 1.0
    return min((book["last_page"] + 1) / max(book["page_count"], 1), 1.0)


def reading(db: Database, now: float | None = None, limit: int = READING_SHOWN) -> list[dict]:
    """The books most recently opened and not finished, newest first, with their review counts."""
    counts = book_counts(db, now)
    out = []
    for b in db.conn.execute(
        "SELECT * FROM books WHERE finished_at IS NULL ORDER BY opened_at DESC LIMIT ?", (limit,)
    ):
        c = counts.get(b["id"], {"due": 0, "new": 0, "total": 0})
        out.append({
            "id": b["id"], "title": b["title"], "path": b["path"], "hash": b["hash"], "web": bool(b["source_url"]),
            "page": b["last_page"] + 1, "pages": b["page_count"], "progress": progress(b),
            "opened": b["opened_at"], **c,
        })
    return out


def heat(db: Database, now: float | None = None, weeks: int = HEAT_WEEKS) -> dict:
    """Reviews per review day over the last `weeks` weeks (as gamify's calendar), with the days
    streak freezes covered when rewards are on."""
    now = time.time() if now is None else now
    today = day_of(now)
    start = today - timedelta(days=weeks * 7 - 1)
    days = gamify.review_days(db)
    frozen = gamify.load(db, now)["frozen"] if gamify.enabled(db) else []
    return {
        "start": start.isoformat(), "end": today.isoformat(),
        "days": [{"date": d.isoformat(), "count": n} for d, n in sorted(days.items()) if d >= start],
        "frozen": [d for d in frozen if d >= start.isoformat()],
        "total": sum(n for d, n in days.items() if d >= start),
        "active": sum(1 for d in days if d >= start),
    }


def forecast(db: Database, now: float | None = None, days: int = FORECAST_DAYS) -> list[dict]:
    """Cards falling due on each of the next `days` days, from tomorrow (today's are on the
    review card itself). Suspended and new cards don't count."""
    now = time.time() if now is None else now
    today = day_of(now)
    counts = [0] * days
    end = srs.next_day(now) + days * 86400 + 3600  # an hour of slack for daylight-saving days
    for (due,) in db.conn.execute(
        "SELECT due FROM cards WHERE suspended=0 AND stage>0 AND due>=? AND due<?", (srs.next_day(now), end),
    ):
        ahead = (day_of(due) - today).days
        if 1 <= ahead <= days:
            counts[ahead - 1] += 1
    return [{"date": (today + timedelta(days=i + 1)).isoformat(), "count": n} for i, n in enumerate(counts)]


def greeting(now: float) -> str:
    hour = datetime.fromtimestamp(now).hour
    return "Good morning" if 4 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"


def home(db: Database, now: float | None = None) -> dict:
    """Everything the Home page shows."""
    now = time.time() if now is None else now
    c = srs.counts(db, None, now)
    upcoming = srs.next_due(db, None, now)
    game = gamify.status(db, now) if gamify.enabled(db) else None
    if game:
        game = {k: v for k, v in game.items() if k not in ("heat", "achievements")}
    return {
        "greeting": greeting(now),
        "date": datetime.fromtimestamp(now).strftime("%A, %-d %B"),
        **c,
        "ready": srs.next_card(db, None, now) is not None,
        "next": f"in {srs.format_interval(upcoming - now)}" if upcoming else "",
        "new_per_day": srs.new_per_day(db),
        "quick": {"cards": gamify.QUICK_CARDS, "minutes": gamify.QUICK_SECONDS // 60},
        "game": game,
        "reading": reading(db, now),
        "books": db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "decks": deck_rows(db, now),
        "heat": heat(db, now),
        "forecast": forecast(db, now),
    }
