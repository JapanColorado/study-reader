"""Spaced repetition: every quiz question Claude writes becomes a card, scheduled with FSRS, as do
the cards made in the card browser or migrated from Anki. A quiz question's first answer in the
chat counts as its first review; after that it comes back in the Review panel. Qt-free."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State

from . import quiz
from .db import Database

VERDICT_RATINGS = {"correct": 3, "partial": 2, "incorrect": 1}
NEW_PER_DAY_SETTING = "srs_new_per_day"
DEFAULT_NEW_PER_DAY = 20
BACKFILL_SETTING = "srs_backfilled"
RETENTION_SETTING = "srs_desired_retention"
PARAMETERS_SETTING = "srs_parameters"  # JSON list of FSRS-6 weights ('' for the defaults)
DEFAULT_RETENTION = 0.9
# Like Anki, a day of reviews runs from 4 am to 4 am.
DAY_STARTS_AT_HOUR = 4
# With nothing else left, cards in learning this soon are shown early rather than waited for.
LEARN_AHEAD = 20 * 60

_scheduler = Scheduler()
_schedulers: dict[tuple, Scheduler] = {}


def dt(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


def day_start(now: float) -> float:
    """When the review day holding `now` began, in local time."""
    local = datetime.fromtimestamp(now)
    start = local.replace(hour=DAY_STARTS_AT_HOUR, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.timestamp()


def next_day(now: float) -> float:
    return (datetime.fromtimestamp(day_start(now)) + timedelta(days=1)).timestamp()


def new_per_day(db: Database) -> int:
    try:
        return max(int(db.get_setting(NEW_PER_DAY_SETTING, str(DEFAULT_NEW_PER_DAY))), 0)
    except ValueError:
        return DEFAULT_NEW_PER_DAY


def desired_retention(db: Database | None) -> float:
    try:
        r = float(db.get_setting(RETENTION_SETTING, str(DEFAULT_RETENTION))) if db else DEFAULT_RETENTION
    except ValueError:
        return DEFAULT_RETENTION
    return min(max(r, 0.7), 0.99)


def parameters(db: Database | None) -> tuple[float, ...]:
    """The FSRS weights in use: optimized or migrated ones if there are any, else the defaults."""
    raw = db.get_setting(PARAMETERS_SETTING) if db else ""
    if raw:
        try:
            return fsrs6_parameters(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return tuple(_scheduler.parameters)


def fsrs6_parameters(weights) -> tuple[float, ...]:
    """FSRS-6's 21 weights from 21, or from FSRS-5's 19 (which FSRS-6 extends with w19 = 0 and
    the old fixed decay w20 = 0.5)."""
    w = [float(x) for x in weights]
    if len(w) == 19:
        w += [0.0, 0.5]
    if len(w) != 21:
        raise ValueError(f"expected 19 or 21 FSRS weights, got {len(w)}")
    Scheduler(parameters=w)  # raises if out of bounds
    return tuple(w)


def decay(db: Database | None) -> float:
    """FSRS-6's forgetting-curve decay (its last weight) in use."""
    return parameters(db)[20]


def recall(stability, last_review, now: float, decay: float) -> float | None:
    """Retrievability on FSRS-6's forgetting curve from a card's stored stability and last
    review (None while it has neither)."""
    if not stability or stability <= 0 or last_review is None:
        return None
    t = max(now - last_review, 0) / 86400
    return (1 + (0.9 ** (-1 / decay) - 1) * t / stability) ** -decay


def scheduler(db: Database | None = None) -> Scheduler:
    """A scheduler with the user's parameters and desired retention."""
    key = (parameters(db), desired_retention(db))
    if key not in _schedulers:
        _schedulers[key] = Scheduler(parameters=key[0], desired_retention=key[1])
    return _schedulers[key]


# -- cards ---------------------------------------------------------------------------------
def add_quiz_cards(db: Database, book_id: int, message_id: int, text: str, now: float | None = None) -> list[int]:
    """Cards for every question of the quizzes in a reply (those it doesn't have yet)."""
    now = time.time() if now is None else now
    page = _asked_page(db, message_id)
    added = []
    for qi, q in enumerate(quiz.quizzes(text)):
        for i, question in enumerate(q.get("questions", [])):
            p = question["page"] - 1 if question.get("page") else page
            card_id = db.add_card(book_id, message_id, qi, i, q["title"], json.dumps(question), p, now)
            if card_id is not None:
                added.append(card_id)
    return added


def _asked_page(db: Database, message_id: int) -> int | None:
    """The page of the passage the quiz was asked about, if any."""
    row = db.conn.execute(
        "SELECT s.page FROM messages q JOIN messages r ON r.chat_id=q.chat_id AND r.id=? "
        "JOIN snippets s ON s.message_id=q.id WHERE q.role='user' AND q.id<r.id ORDER BY q.id DESC, s.id LIMIT 1",
        (message_id,),
    ).fetchone()
    return row["page"] if row else None


def question(card) -> dict:
    return json.loads(card["data"])


def is_new(card) -> bool:
    return card["reps"] == 0


def suggested_rating(verdict: str | None) -> int | None:
    return VERDICT_RATINGS.get(verdict)


# -- scheduling ----------------------------------------------------------------------------
def _fsrs_card(card, now: float) -> Card:
    if card["fsrs"]:
        return Card.from_json(card["fsrs"])
    return Card(due=dt(now))


def review(
    db: Database, card_id: int, rating: int, now: float | None = None, response: dict | None = None,
    verdict: str | None = None, source: str = "review", duration: int = 0,
) -> None:
    """Schedule a card after it was answered and rated (1 again … 4 easy). duration: ms spent on it."""
    now = time.time() if now is None else now
    card = db.card(card_id)
    if card is None:
        return
    fsrs_card, _ = scheduler(db).review_card(_fsrs_card(card, now), Rating(rating), dt(now))
    lapse = rating == 1 and card["stage"] == State.Review.value
    db.record_review(
        card_id, rating, source, json.dumps(response) if response else "", verdict, now, duration,
        was_new=card["stage"] == 0, **schedule_columns(fsrs_card), reps=card["reps"] + 1, lapses=card["lapses"] + lapse,
    )


def schedule_columns(fsrs_card: Card) -> dict:
    """The cards-table columns that hold an fsrs.Card's schedule."""
    return {
        "fsrs": fsrs_card.to_json(), "stage": fsrs_card.state.value, "due": fsrs_card.due.timestamp(),
        "stability": fsrs_card.stability, "difficulty": fsrs_card.difficulty,
        "last_review": fsrs_card.last_review.timestamp() if fsrs_card.last_review else None,
    }


def new_schedule_columns(due: float) -> dict:
    """The cards-table columns of a card that is new (again)."""
    return {"stage": 0, "fsrs": "", "due": due, "reps": 0, "lapses": 0, "stability": None, "difficulty": None,
            "last_review": None}


def forget(db: Database, card_ids, now: float | None = None) -> None:
    """Make cards new again (their review log stays)."""
    now = time.time() if now is None else now
    db.update_cards(card_ids, **new_schedule_columns(now), updated_at=now)


def set_due(db: Database, card_ids, first: int, last: int | None = None, now: float | None = None) -> int:
    """Make cards due in `first` days (0 = today), or spread at random over first..last days.
    New cards are left in the new queue. Returns how many cards were rescheduled."""
    import random
    now = time.time() if now is None else now
    moved = 0
    for card_id in card_ids:
        card = db.card(card_id)
        if card is None or card["stage"] == 0:
            continue
        days = random.randint(first, last) if last is not None and last > first else first
        due = day_start(now) + days * 86400 + (now - day_start(now) if days == 0 else 0)
        fsrs_card = _fsrs_card(card, now)
        fsrs_card.due = dt(due)
        if fsrs_card.state != State.Review:  # learning steps don't fit a date: graduate it
            fsrs_card.state, fsrs_card.step = State.Review, None
        db.update_card(card_id, **schedule_columns(fsrs_card), updated_at=now)
        moved += 1
    return moved


def retrievability(db: Database | None, card, now: float | None = None) -> float | None:
    """How likely the card is to be recalled now (None while new)."""
    if not card["fsrs"]:
        return None
    now = time.time() if now is None else now
    return scheduler(db).get_card_retrievability(Card.from_json(card["fsrs"]), dt(now))


def answered_in_chat(
    db: Database, message_id: int, quiz_index: int, question_index: int, verdict: str | None,
    response: dict, now: float | None = None,
) -> bool:
    """A quiz question answered (and graded) in the chat: its card's first review, if it has
    had none. Later answers (retakes) don't change the schedule. True if it was recorded."""
    card = db.card_for(message_id, quiz_index, question_index)
    rating = response.get("rating") if "rating" in response else suggested_rating(verdict)
    if card is None or db.reviews(card["id"]) or rating not in quiz.RATINGS:
        return False
    review(db, card["id"], rating, now, response, verdict, source="chat")
    return True


def previews(card, now: float | None = None, db: Database | None = None) -> dict[int, str]:
    """For each rating, when the card would come back: {1: "1m", 3: "10m", …}."""
    now = time.time() if now is None else now
    sched = scheduler(db)
    out = {}
    for rating in quiz.RATINGS:
        after, _ = sched.review_card(_fsrs_card(card, now), Rating(rating), dt(now))
        out[rating] = format_interval(after.due.timestamp() - now)
    return out


def format_interval(seconds: float) -> str:
    seconds = max(seconds, 0)
    for unit, size in (("y", 365 * 86400), ("mo", 30 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size or unit == "m":
            n = seconds / size
            if unit == "y":
                return f"{n:.1f}".rstrip("0").rstrip(".") + "y"
            return f"{max(round(n), 1)}{unit}"
    return ""


# -- the queue -----------------------------------------------------------------------------
# Due cards; args (next_day(now), now). due_sql("c.") is the same over a table alias.
def due_sql(alias: str = "") -> str:
    return f"{alias}suspended=0 AND (({alias}stage=2 AND {alias}due<?) OR ({alias}stage IN (1,3) AND {alias}due<=?))"


DUE = due_sql()
NEW = "suspended=0 AND stage=0"


def new_left_today(db: Database, now: float) -> int:
    return max(new_per_day(db) - db.new_cards_reviewed_since(day_start(now)), 0)


def counts(db: Database, book_id: int | None, now: float | None = None, deck: int | None = None) -> dict:
    """What there is to review in a book or deck (or everywhere, with neither): due cards, new
    cards that can be introduced today, and the total."""
    now = time.time() if now is None else now
    new = db.count_cards(book_id, NEW, deck_id=deck)
    return {
        "due": db.count_cards(book_id, DUE, (next_day(now), now), deck_id=deck),
        "new": min(new, new_left_today(db, now)),
        "new_total": new,
        "total": db.count_cards(book_id, deck_id=deck),
        "suspended": db.count_cards(book_id, "suspended=1", deck_id=deck),
    }


def deck_counts(db: Database, now: float | None = None) -> dict[int, dict]:
    """For every deck, its cards and those of the decks under it: {deck id: {due, new, total}}
    (new: cards waiting, not capped by today's allowance)."""
    now = time.time() if now is None else now
    own = {r["deck_id"]: r for r in db.conn.execute(
        f"SELECT deck_id, SUM({DUE}) AS due, SUM({NEW}) AS new, COUNT(*) AS total FROM cards GROUP BY deck_id",
        (next_day(now), now),
    )}
    decks = db.decks()
    out = {d["id"]: {"due": 0, "new": 0, "total": 0} for d in decks}
    by_name = {d["name"]: d["id"] for d in decks}
    for d in decks:
        row = own.get(d["id"])
        if not row:
            continue
        parts = d["name"].split("::")
        for i in range(1, len(parts) + 1):
            target = by_name.get("::".join(parts[:i]))
            if target is not None:
                for k in ("due", "new", "total"):
                    out[target][k] += row[k] or 0
    return out


def next_card(db: Database, book_id: int | None, now: float | None = None, skip=(), deck: int | None = None):
    """The card to review next: due learning and review cards first (most overdue first),
    then new ones while today's allowance lasts, then learning cards due within LEARN_AHEAD.
    skip: ids of cards not to show again yet (just answered, or skipped), unless nothing else is left."""
    now = time.time() if now is None else now
    tiers = [(DUE, (next_day(now), now), "due, id")]
    if new_left_today(db, now):
        tiers.append((NEW, (), "created_at, id"))
    tiers.append(("suspended=0 AND stage IN (1,3) AND due<=?", (now + LEARN_AHEAD,), "due, id"))
    fallback = None
    for where, args, order in tiers:
        for card in db.cards(book_id, where, args, order=order, limit=len(skip) + 1, deck_id=deck):
            if card["id"] not in skip:
                return card
            fallback = fallback or card
    return fallback


def next_due(db: Database, book_id: int | None, now: float | None = None, deck: int | None = None) -> float | None:
    """When the next card that isn't due yet will be."""
    now = time.time() if now is None else now
    row = db.cards(book_id, "suspended=0 AND stage>0 AND due>?", (now,), order="due", limit=1, deck_id=deck)
    return row[0]["due"] if row else None


# -- existing quizzes ----------------------------------------------------------------------
def backfill(db: Database) -> int:
    """Once: cards for the quizzes in replies written before cards existed, with the answers
    already given counted as first reviews (at the time they were given)."""
    if db.get_setting(BACKFILL_SETTING) == "1":
        return 0
    added = 0
    rows = db.conn.execute(
        "SELECT m.id, m.text, m.created_at, c.book_id, c.id AS chat_id FROM messages m "
        "JOIN chats c ON c.id=m.chat_id WHERE m.role='assistant' AND m.text LIKE '%quiz%' ORDER BY m.id"
    ).fetchall()
    for m in rows:
        if not add_quiz_cards(db, m["book_id"], m["id"], m["text"], now=m["created_at"]):
            continue
        added += 1
        for a in db.quiz_answers(m["chat_id"]).get(m["id"], []):
            if a["status"] == "done":
                answered_in_chat(db, m["id"], a["quiz"], a["question"], a["verdict"], json.loads(a["response"]),
                                 now=a["created_at"])
    db.set_setting(BACKFILL_SETTING, "1")
    return added


# -- parameters ----------------------------------------------------------------------------
MIN_OPTIMIZE_REVIEWS = 400  # below this, FSRS's defaults are a better bet than a fit


def optimizable_reviews(db: Database) -> int:
    """Reviews of cards past their first review, which is what the optimizer learns from."""
    return db.conn.execute(
        "SELECT COUNT(*) FROM reviews r JOIN (SELECT card_id, MIN(reviewed_at) AS first FROM reviews GROUP BY card_id) "
        "f USING (card_id) WHERE r.reviewed_at>f.first"
    ).fetchone()[0]


def review_logs(db: Database) -> list:
    """The whole review log as fsrs.ReviewLog objects, for fit() (which may run off the GUI thread)."""
    from fsrs import ReviewLog

    return [
        ReviewLog(card_id=r["card_id"], rating=Rating(r["rating"]), review_datetime=dt(r["reviewed_at"]),
                  review_duration=r["duration"] or None)
        for r in db.conn.execute("SELECT card_id, rating, reviewed_at, duration FROM reviews ORDER BY reviewed_at, id")
    ]


def fit(logs: list) -> list[float]:
    """FSRS weights fitted to a review log (needs the optimizer's PyTorch)."""
    from fsrs import Optimizer

    return [float(w) for w in Optimizer(logs).compute_optimal_parameters()]


def set_parameters(db: Database, weights) -> None:
    """Use these FSRS weights from now on ([] or None: the defaults). Cards keep their memory
    state; the new weights shape the intervals of their next reviews."""
    db.set_setting(PARAMETERS_SETTING, json.dumps(list(fsrs6_parameters(weights))) if weights else "")
