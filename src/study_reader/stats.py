"""Review statistics for the Statistics window: what was reviewed and how well, what is coming
due, and how the cards' memory states are spread. Everything comes out JSON-ready for
stats.html. Qt-free."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from . import srs
from .db import SUBTREE, Database

MATURE_DAYS = 21  # Anki's line between young and mature cards
FORECAST_DAYS = 30
RECENT_DAYS = 30
CALENDAR_DAYS = 365
RETENTION_PERIODS = (("today", 1), ("week", 7), ("month", 30), ("year", 365))

# (label, upper bound in days) for intervals and stabilities; the last bucket is open-ended.
DAY_BUCKETS = (
    ("< 1d", 1), ("1–2d", 3), ("3–6d", 7), ("1–2w", 14), ("2–4w", 28), ("1–3mo", 91),
    ("3–6mo", 182), ("6–12mo", 365), ("1–2y", 730), ("2y+", float("inf")),
)
RETRIEVABILITY_BUCKETS = (
    ("< 50%", 0.5), ("50–70%", 0.7), ("70–80%", 0.8), ("80–85%", 0.85), ("85–90%", 0.9),
    ("90–95%", 0.95), ("95–100%", 1.01),
)


def day_of(t: float) -> date:
    """The review day a moment falls on (days roll over at 4 am local time, as in srs.py)."""
    return datetime.fromtimestamp(srs.day_start(t)).date()


def _scope(deck_id: int | None, book_id: int | None) -> tuple[str, tuple]:
    """SQL condition on cards (alias c) for the scope, with its arguments."""
    if deck_id is not None:
        return f"c.deck_id IN ({SUBTREE})", (deck_id,)
    if book_id is not None:
        return "c.book_id=?", (book_id,)
    return "1", ()


def _bucket(value: float, buckets) -> int:
    for i, (_, upper) in enumerate(buckets):
        if value < upper:
            return i
    return len(buckets) - 1


def _histogram(values, buckets) -> list[dict]:
    counts = [0] * len(buckets)
    for v in values:
        counts[_bucket(v, buckets)] += 1
    return [{"label": label, "count": n} for (label, _), n in zip(buckets, counts)]


def _rate(passed: int, total: int) -> dict:
    return {"passed": passed, "total": total, "rate": passed / total if total else None}


def compute(db: Database, deck_id: int | None = None, book_id: int | None = None, now: float | None = None) -> dict:
    """Statistics for a deck and the decks under it, a book's cards, or (neither) every card."""
    now = time.time() if now is None else now
    today = day_of(now)
    where, args = _scope(deck_id, book_id)
    decay = srs.decay(db)

    # -- cards -------------------------------------------------------------------------
    counts = {"new": 0, "learning": 0, "relearning": 0, "young": 0, "mature": 0, "suspended": 0, "total": 0}
    forecast = [0] * FORECAST_DAYS
    intervals, stabilities, difficulties, retrievabilities = [], [], [], []
    added: dict[date, int] = {}
    for c in db.conn.execute(
        f"SELECT stage, suspended, due, last_review, stability, difficulty, created_at FROM cards c WHERE {where}",
        args,
    ):
        counts["total"] += 1
        created = day_of(c["created_at"])
        if (today - created).days < RECENT_DAYS:
            added[created] = added.get(created, 0) + 1
        if c["suspended"]:
            counts["suspended"] += 1
            continue
        stage = c["stage"]
        if stage == 0:
            counts["new"] += 1
            continue
        interval = (c["due"] - c["last_review"]) / 86400 if c["last_review"] else None
        if stage == 1:
            counts["learning"] += 1
        elif stage == 3:
            counts["relearning"] += 1
        else:
            counts["mature" if interval is not None and interval >= MATURE_DAYS else "young"] += 1
        if stage in (2, 3) and interval is not None:
            intervals.append(interval)
        ahead = max((day_of(c["due"]) - today).days, 0)
        if ahead < FORECAST_DAYS:
            forecast[ahead] += 1
        if c["stability"]:
            stabilities.append(c["stability"])
            if c["last_review"]:
                retrievabilities.append(srs.recall(c["stability"], c["last_review"], now, decay))
        if c["difficulty"] is not None:
            difficulties.append(c["difficulty"])

    # -- reviews -----------------------------------------------------------------------
    calendar: dict[date, int] = {}
    recent = {(today - timedelta(days=i)): {"first": 0, "again": 0, "passed": 0, "minutes": 0.0}
              for i in range(RECENT_DAYS)}
    today_stats = {"reviews": 0, "minutes": 0.0, "again": 0, "passed": 0, "new": 0}
    retention = {name: {"young": [0, 0], "mature": [0, 0]} for name, _ in RETENTION_PERIODS}
    total_reviews = 0
    prev_card, prev_time = None, None
    for r in db.conn.execute(
        f"SELECT r.card_id, r.rating, r.reviewed_at, r.duration FROM reviews r JOIN cards c ON c.id=r.card_id "
        f"WHERE {where} ORDER BY r.card_id, r.reviewed_at, r.id",
        args,
    ):
        total_reviews += 1
        first = r["card_id"] != prev_card
        previous = None if first else prev_time
        prev_card, prev_time = r["card_id"], r["reviewed_at"]
        day = day_of(r["reviewed_at"])
        ago = (today - day).days
        passed = r["rating"] > 1
        if 0 <= ago < CALENDAR_DAYS:
            calendar[day] = calendar.get(day, 0) + 1
        if day in recent:
            bucket = recent[day]
            bucket["first" if first else "passed" if passed else "again"] += 1
            bucket["minutes"] += (r["duration"] or 0) / 60000
        if ago == 0:
            today_stats["reviews"] += 1
            today_stats["minutes"] += (r["duration"] or 0) / 60000
            today_stats["again" if r["rating"] == 1 else "passed"] += 1
            today_stats["new"] += first
        if previous is not None and day_of(previous) < day:
            kind = "young" if (r["reviewed_at"] - previous) / 86400 < MATURE_DAYS else "mature"
            for name, days in RETENTION_PERIODS:
                if 0 <= ago < days:
                    retention[name][kind][0] += passed
                    retention[name][kind][1] += 1

    # -- streaks -----------------------------------------------------------------------
    current = 0
    d = today if today in calendar else today - timedelta(days=1)
    while d in calendar:
        current += 1
        d -= timedelta(days=1)
    longest = run = 0
    last = None
    for d in sorted(calendar):
        run = run + 1 if last is not None and (d - last).days == 1 else 1
        longest = max(longest, run)
        last = d
    span = (today - min(calendar)).days + 1 if calendar else 0

    n = today_stats["reviews"]
    estimated = sum(retrievabilities)
    return {
        "today": {
            **today_stats, "minutes": round(today_stats["minutes"], 1),
            "pass_rate": today_stats["passed"] / n if n else None,
        },
        "counts": counts,
        "calendar": {
            "start": (today - timedelta(days=CALENDAR_DAYS - 1)).isoformat(),
            "end": today.isoformat(),
            "days": [{"date": d.isoformat(), "count": calendar[d]} for d in sorted(calendar)],
            "streak": current, "longest": longest,
            "studied": len(calendar), "span": span,
            "studied_pct": len(calendar) / span if span else None,
        },
        "reviews": [
            {"date": d.isoformat(), **{k: (round(v, 1) if k == "minutes" else v) for k, v in recent[d].items()}}
            for d in sorted(recent)
        ],
        "forecast": [{"day": i, "date": (today + timedelta(days=i)).isoformat(), "count": forecast[i]}
                     for i in range(FORECAST_DAYS)],
        "retention": {
            name: {
                "young": _rate(*v["young"]), "mature": _rate(*v["mature"]),
                "all": _rate(v["young"][0] + v["mature"][0], v["young"][1] + v["mature"][1]),
            }
            for name, v in retention.items()
        },
        "distributions": {
            "interval": _histogram(intervals, DAY_BUCKETS),
            "stability": _histogram(stabilities, DAY_BUCKETS),
            "difficulty": [
                {"label": f"{i}–{i + 1}", "count": sum(1 for x in difficulties if min(max(int(x), 1), 9) == i)}
                for i in range(1, 10)
            ],
            "retrievability": _histogram(retrievabilities, RETRIEVABILITY_BUCKETS),
        },
        "estimated": {
            "average_retrievability": estimated / len(retrievabilities) if retrievabilities else None,
            "remembered": round(estimated), "scheduled": len(retrievabilities),
        },
        "added": [{"date": (today - timedelta(days=i)).isoformat(), "count": added.get(today - timedelta(days=i), 0)}
                  for i in reversed(range(RECENT_DAYS))],
        "settings": {
            "desired_retention": srs.desired_retention(db),
            "custom_parameters": bool(db.get_setting(srs.PARAMETERS_SETTING)),
            "total_reviews": total_reviews,
            "optimizable_reviews": srs.optimizable_reviews(db),
            "min_optimize_reviews": srs.MIN_OPTIMIZE_REVIEWS,
        },
    }
