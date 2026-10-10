"""Spaced repetition: every quiz question Claude writes becomes a card, scheduled with FSRS.
The first answer in the chat counts as a card's first review; after that it comes back in the
Review panel. Qt-free."""

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
# Like Anki, a day of reviews runs from 4 am to 4 am.
DAY_STARTS_AT_HOUR = 4
# With nothing else left, cards in learning this soon are shown early rather than waited for.
LEARN_AHEAD = 20 * 60

_scheduler = Scheduler()


def _dt(t: float) -> datetime:
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
    return Card(due=_dt(now))


def review(
    db: Database, card_id: int, rating: int, now: float | None = None, response: dict | None = None,
    verdict: str | None = None, source: str = "review",
) -> None:
    """Schedule a card after it was answered and rated (1 again … 4 easy)."""
    now = time.time() if now is None else now
    card = db.card(card_id)
    if card is None:
        return
    fsrs_card, _ = _scheduler.review_card(_fsrs_card(card, now), Rating(rating), _dt(now))
    lapse = rating == 1 and card["stage"] == State.Review.value
    db.record_review(
        card_id, rating, source, json.dumps(response) if response else "", verdict, now,
        fsrs=fsrs_card.to_json(), stage=fsrs_card.state.value, due=fsrs_card.due.timestamp(),
        reps=card["reps"] + 1, lapses=card["lapses"] + lapse,
    )


def answered_in_chat(
    db: Database, message_id: int, quiz_index: int, question_index: int, verdict: str | None,
    response: dict, now: float | None = None,
) -> bool:
    """A quiz question answered (and graded) in the chat: its card's first review, if it has
    had none. Later answers (retakes) don't change the schedule. True if it was recorded."""
    card = db.card_for(message_id, quiz_index, question_index)
    rating = response.get("rating") if "rating" in response else suggested_rating(verdict)
    if card is None or not is_new(card) or rating not in quiz.RATINGS:
        return False
    review(db, card["id"], rating, now, response, verdict, source="chat")
    return True


def previews(card, now: float | None = None) -> dict[int, str]:
    """For each rating, when the card would come back: {1: "1m", 3: "10m", …}."""
    now = time.time() if now is None else now
    out = {}
    for rating in quiz.RATINGS:
        after, _ = _scheduler.review_card(_fsrs_card(card, now), Rating(rating), _dt(now))
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
DUE = "suspended=0 AND ((stage=2 AND due<?) OR (stage IN (1,3) AND due<=?))"
NEW = "suspended=0 AND stage=0"


def new_left_today(db: Database, now: float) -> int:
    return max(new_per_day(db) - db.new_cards_reviewed_since(day_start(now)), 0)


def counts(db: Database, book_id: int | None, now: float | None = None) -> dict:
    """What there is to review in a book (or all books, with None): due cards, new cards that
    can be introduced today, and the total."""
    now = time.time() if now is None else now
    new = db.count_cards(book_id, NEW)
    return {
        "due": db.count_cards(book_id, DUE, (next_day(now), now)),
        "new": min(new, new_left_today(db, now)),
        "new_total": new,
        "total": db.count_cards(book_id),
        "suspended": db.count_cards(book_id, "suspended=1"),
    }


def next_card(db: Database, book_id: int | None, now: float | None = None, skip=()):
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
        for card in db.cards(book_id, where, args, order=order, limit=len(skip) + 1):
            if card["id"] not in skip:
                return card
            fallback = fallback or card
    return fallback


def next_due(db: Database, book_id: int | None, now: float | None = None) -> float | None:
    """When the next card that isn't due yet will be."""
    now = time.time() if now is None else now
    row = db.cards(book_id, "suspended=0 AND stage>0 AND due>?", (now,), order="due", limit=1)
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
