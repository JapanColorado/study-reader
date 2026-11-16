"""Practice: every quiz question Claude writes goes into practice, scheduled with FSRS to come
back right before it would be forgotten, as do the questions made in the question browser or
migrated from Anki. A quiz question's first answer in the chat counts as its first attempt; after
that it comes back on the Practice page. Qt-free."""

from __future__ import annotations

import functools
import json
import re
import time
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State

from . import question_model, quiz
from .db import Database, last_page

VERDICT_RATINGS = {"correct": 3, "partial": 2, "incorrect": 1}
NEW_PER_DAY_SETTING = "practice_new_per_day"
DEFAULT_NEW_PER_DAY = 20
BACKFILL_SETTING = "practice_backfilled"
RETENTION_SETTING = "practice_desired_retention"
PARAMETERS_SETTING = "practice_parameters"  # JSON list of FSRS-6 weights ('' for the defaults)
DEFAULT_RETENTION = 0.9
# Like Anki, a day of practice runs from 4 am to 4 am.
DAY_STARTS_AT_HOUR = 4
# With nothing else left, questions in learning this soon are shown early rather than waited for.
LEARN_AHEAD = 20 * 60

_scheduler = Scheduler()
_schedulers: dict[tuple, Scheduler] = {}


def dt(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


def day_start(now: float) -> float:
    """When the practice day holding `now` began, in local time."""
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


def recall(stability, last_attempt, now: float, decay: float) -> float | None:
    """Retrievability on FSRS-6's forgetting curve from a question's stored stability and last
    attempt (None while it has neither)."""
    if not stability or stability <= 0 or last_attempt is None:
        return None
    t = max(now - last_attempt, 0) / 86400
    return (1 + (0.9 ** (-1 / decay) - 1) * t / stability) ** -decay


def scheduler(db: Database | None = None) -> Scheduler:
    """A scheduler with the user's parameters and desired retention."""
    key = (parameters(db), desired_retention(db))
    if key not in _schedulers:
        _schedulers[key] = Scheduler(parameters=key[0], desired_retention=key[1])
    return _schedulers[key]


# -- questions ------------------------------------------------------------------------------
def add_quiz_questions(db: Database, book_id: int | None, message_id: int, text: str, now: float | None = None,
                       deck_id: int | None = None) -> list[int]:
    """Put every question of the quizzes in a reply into practice (those not there yet), in
    deck_id (else the book's deck), tagged with the tags Claude gives and the section of the book
    they draw on: the one Claude names (for exercises at a chapter's end), else that of the page
    Claude gives, else the section holding all the pages the quiz was asked about.

    A quiz that revises an earlier one (quiz.revised_quiz) takes over the questions it carries
    over, which keep their schedules and history with the new content; the earlier quiz's other
    questions go unless they were attempted. Returns the ids of the questions added or taken over."""
    now = time.time() if now is None else now
    first, last = _asked_pages(db, message_id)
    book = db.book(book_id)
    toc = json.loads(book["toc"]) if book and book["toc"] else []
    added = []
    for qi, q in enumerate(quiz.quizzes(text)):
        old = _revised(db, message_id, q["revises"]) if q.get("revises") and "questions" in q else None
        for i, content in enumerate(q.get("questions", [])):
            content = dict(content)
            tags = content.pop("tags", [])
            n = content.pop("revises", None)
            section = content.pop("section", None)
            p, to = (content["page"] - 1,) * 2 if content.get("page") else (first, last)
            tag = section_tag(book["title"], toc, p, to, section) if book else None
            tags = [*tags, tag] if tag else tags
            row = db.question_for(old[0], old[1], n - 1) if old and n else None
            if row is not None and db.question_for(message_id, qi, i) is None:
                db.relink_question(row["id"], message_id, qi, i, q["title"], p, now)
                kind, fields = question_model.question_fields(content)
                group = db.question_group(row["group_id"])
                db.update_question_group(row["group_id"], fields, [*group["tags"].split(), *tags], kind, now)
                added.append(row["id"])
                continue
            question_id = db.add_question(book_id, message_id, qi, i, q["title"], json.dumps(content), p, now,
                                          tags=tags, deck_id=deck_id)
            if question_id is not None:
                added.append(question_id)
        if old:
            db.drop_new_quiz_questions(*old)
    return added


def _revised(db: Database, message_id: int, title: str) -> tuple[int, int] | None:
    """The earlier quiz in the reply's chat that a quiz of it revises: (message id, index)."""
    rows = db.conn.execute(
        "SELECT m.id, m.text FROM messages m JOIN messages r ON r.chat_id=m.chat_id AND r.id=? "
        "WHERE m.role='assistant' AND m.id<r.id ORDER BY m.id", (message_id,),
    ).fetchall()
    return quiz.revised_quiz([(r["id"], i, q) for r in rows for i, q in enumerate(quiz.quizzes(r["text"]))], title)


def _asked_pages(db: Database, message_id: int) -> tuple[int | None, int | None]:
    """The first and last pages the quiz was asked about: the passage's, or the part of the
    book a quiz request covers; (None, None) if unknown."""
    row = db.conn.execute(
        "SELECT * FROM (SELECT q.id, s.id AS s, s.page AS first, s.page AS last, s.rects FROM messages q "
        "JOIN messages r ON r.chat_id=q.chat_id AND r.id=? JOIN snippets s ON s.message_id=q.id "
        "WHERE q.role='user' AND q.id<r.id "
        "UNION ALL SELECT q.id, 0, q.page_from, q.page_to, NULL FROM messages q "
        "JOIN messages r ON r.chat_id=q.chat_id AND r.id=? "
        "WHERE q.role='user' AND q.id<r.id AND q.page_from IS NOT NULL) ORDER BY id DESC, s LIMIT 1",
        (message_id, message_id),
    ).fetchone()
    if row is None:
        return None, None
    if row["rects"] is not None:  # a passage, perhaps selected across pages
        return row["first"], last_page(row["first"], json.loads(row["rects"]))
    return row["first"], row["last"]


# -- sections ------------------------------------------------------------------------------
# A quiz question is tagged with the section of its book it draws on, from the book's contents:
# Book::Chapter::Section (tag parts have no spaces or commas, see tag_part).
tag_part = question_model.tag_part


# End-of-chapter matter that some books' contents nest inside the chapter's last section.
BACK_MATTER = re.compile(r"(chapter )?(summary|review|exercises|problems|questions( and problems)?|review questions"
                         r"|key (terms|concepts|ideas))|challenge solution\b.*", re.I)


def section_path(toc, page: int | None) -> list[str]:
    """The titles of the contents entries holding a 0-based page, outermost first: the entry
    starting last at or before the page, and its parents."""
    if page is None:
        return []
    best = None
    for i, (_, _, p) in enumerate(toc):
        if p <= page and (best is None or p >= toc[best][2]):
            best = i
    return [] if best is None else _entry_path(toc, best)


def _entry_path(toc, i: int) -> list[str]:
    """The titles of a contents entry and its parents, outermost first. Back matter (a summary,
    exercises) nested in the last section of a chapter belongs to the chapter, unless the
    chapter's other sections have their own."""
    parents, level = [], toc[i][0]
    for j in range(i - 1, -1, -1):
        if toc[j][0] < level:
            parents.insert(0, j)
            level = toc[j][0]
    if len(parents) >= 2 and BACK_MATTER.fullmatch(_title_key(toc[i][1])):
        section, chapter = parents[-1], parents[-2]
        end = next((k for k in range(section + 1, len(toc)) if toc[k][0] <= toc[section][0]), len(toc))
        last = end == len(toc) or toc[end][0] <= toc[chapter][0]
        key = _title_key(toc[i][1])
        elsewhere = any(_title_key(t) == key and lvl == toc[i][0]
                        for lvl, t, _ in toc[chapter + 1:section])
        if last and not elsewhere:
            parents.pop()
    return [toc[j][1] for j in parents] + [toc[i][1]]


def _title_key(title: str) -> str:
    return " ".join(title.replace("\x00", "").split()).lower()


def find_section(toc, section: str, page: int | None = None) -> int | None:
    """The contents entry a section names, by its number ("6.4", "Chapter 6") or its title (with
    or without the number): of several, the last starting at or before the page."""
    key = _title_key(section).rstrip(".")
    if not key:
        return None
    number = re.compile(re.escape(key) + r"(?!\.?\w)")
    found = [i for i, (_, title, _) in enumerate(toc)
             if number.match(t := _title_key(title)) or re.sub(r"^[\d.]+\s+", "", t) == key]
    if not found:
        return None
    before = [i for i in found if page is not None and toc[i][2] <= page]
    return before[-1] if before else found[0]


def section_tag(book_title: str, toc, page: int | None, last: int | None = None,
                section: str | None = None) -> str | None:
    """The tag of the section holding a page (or all of pages page…last), or of the section
    named (find_section), if the contents have it."""
    named = find_section(toc, section, page) if section else None
    if named is not None:
        path = _entry_path(toc, named)
    else:
        path = section_path(toc, page)
        if last is not None and last != page:
            end = section_path(toc, last)
            n = 0
            while n < min(len(path), len(end)) and path[n] == end[n]:
                n += 1
            path = path[:n]
    parts = [tag_part(t) for t in path]
    parts = [p for p in parts if p]
    return "::".join([tag_part(book_title) or "Book", *parts]) if parts else None


def learn_toc(db: Database, book_id: int, toc) -> int:
    """Remember a book's contents. The first time they are known, the book's quiz questions made
    before get their section tags. Returns how many were tagged."""
    book = db.book(book_id)
    if book is None:
        return 0
    toc = [[lvl, title, page] for lvl, title, page in toc]
    first = book["toc"] is None
    db.update_book(book_id, toc=json.dumps(toc, ensure_ascii=False))
    if not first or not toc:
        return 0
    tagged = 0
    for row in db.questions(book_id, "quiz IS NOT NULL AND page IS NOT NULL"):
        tag = section_tag(book["title"], toc, row["page"])
        if tag:
            db.add_tags([row["group_id"]], [tag])
            tagged += 1
    return tagged


MAX_TAGS_IN_CONTEXT = 300


def concept_tags(db: Database) -> list[str]:
    """The user's tags other than section tags (and 'marked'), for Claude to reuse in quizzes."""
    roots = {section_root(b).lower() for b in db.conn.execute("SELECT title FROM books")}
    return [t for t in db.all_tags() if t.split("::")[0].lower() not in roots and t.lower() != "marked"]


def tags_context(db: Database) -> str:
    tags = concept_tags(db)[:MAX_TAGS_IN_CONTEXT]
    if not tags:
        return ""
    return ("Tags already on the user's practice questions (reuse these for quiz questions where they fit):\n"
            f"<existing_tags>{' '.join(tags)}</existing_tags>")


def section_root(book) -> str:
    return tag_part(book["title"]) or "Book"


def sections(db: Database, now: float | None = None) -> dict[int, list[dict]]:
    """For every book, the sections its questions are tagged with, as a tree in reading order:
    {book id: [{tag, name, parent (tag or None), due, new, total}]}. A section's counts include
    those of the sections in it; new: questions waiting, not capped by today's allowance."""
    now = time.time() if now is None else now
    roots = {b["id"]: section_root(b) for b in db.conn.execute("SELECT id, title FROM books")}
    rows = db.conn.execute(
        f"SELECT c.book_id, c.page, g.tags, {due_sql('c.')} AS due, c.suspended=0 AND c.stage=0 AS new "
        f"FROM questions c JOIN question_groups g ON g.id=c.group_id WHERE c.book_id IS NOT NULL AND g.tags!=''",
        (next_day(now), now),
    )
    nodes: dict[int, dict[str, dict]] = {}
    for r in rows:
        root = roots.get(r["book_id"])
        if root is None:
            continue
        prefix = root.lower() + "::"
        tags = set()
        for t in r["tags"].split():
            if t.lower().startswith(prefix):
                parts = t.split("::")
                tags.update("::".join(parts[:i]) for i in range(2, len(parts) + 1))
        book_nodes = nodes.setdefault(r["book_id"], {})
        for t in tags:
            n = book_nodes.setdefault(t, {"tag": t, "name": t.rsplit("::", 1)[-1].replace("_", " "),
                                          "parent": t.rsplit("::", 1)[0] if t.count("::") > 1 else None,
                                          "due": 0, "new": 0, "total": 0, "page": r["page"]})
            n["due"] += r["due"] or 0
            n["new"] += r["new"] or 0
            n["total"] += 1
            if r["page"] is not None and (n["page"] is None or r["page"] < n["page"]):
                n["page"] = r["page"]
    # Sections in the book's order: from where they start, else from their first question.
    for b, ns in nodes.items():
        starts = _section_starts(roots[b], db.book(b)["toc"])
        for n in ns.values():
            n["page"] = starts.get(n["tag"].lower(), n["page"])
    return {b: sorted(ns.values(), key=lambda n: (n["page"] is None, n["page"] or 0, n["tag"].count("::"), n["tag"]))
            for b, ns in nodes.items()}


@functools.lru_cache(maxsize=16)
def _section_starts(root: str, toc_json: str | None) -> dict[str, int]:
    """The first page of each section tag (lowercase) a book's contents make."""
    toc = json.loads(toc_json) if toc_json else []
    starts: dict[str, int] = {}
    for i, (_, _, page) in enumerate(toc):
        parts = [p for p in map(tag_part, _entry_path(toc, i)) if p]
        if parts:
            starts.setdefault("::".join([root, *parts]).lower(), page)
    return starts


def question(row) -> dict:
    """A question row's content, as quiz.py parses it."""
    return json.loads(row["data"])


def is_new(row) -> bool:
    return row["reps"] == 0


def suggested_rating(verdict: str | None) -> int | None:
    return VERDICT_RATINGS.get(verdict)


# -- scheduling ----------------------------------------------------------------------------
def _fsrs_card(row, now: float) -> Card:
    if row["fsrs"]:
        return Card.from_json(row["fsrs"])
    return Card(due=dt(now))


def attempt(
    db: Database, question_id: int, rating: int, now: float | None = None, response: dict | None = None,
    verdict: str | None = None, source: str = "practice", duration: int = 0,
) -> None:
    """Log an attempt at a question, answered and rated (1 missed … 4 easy), and schedule it.
    duration: ms spent on it."""
    now = time.time() if now is None else now
    row = db.question(question_id)
    if row is None:
        return
    fsrs_card, _ = scheduler(db).review_card(_fsrs_card(row, now), Rating(rating), dt(now))
    lapse = rating == 1 and row["stage"] == State.Review.value
    db.record_attempt(
        question_id, rating, source, json.dumps(response) if response else "", verdict, now, duration,
        was_new=row["stage"] == 0, **schedule_columns(fsrs_card), reps=row["reps"] + 1, lapses=row["lapses"] + lapse,
    )


def schedule_columns(fsrs_card: Card) -> dict:
    """The questions-table columns that hold an fsrs.Card's schedule."""
    return {
        "fsrs": fsrs_card.to_json(), "stage": fsrs_card.state.value, "due": fsrs_card.due.timestamp(),
        "stability": fsrs_card.stability, "difficulty": fsrs_card.difficulty,
        "last_attempt": fsrs_card.last_review.timestamp() if fsrs_card.last_review else None,
    }


def new_schedule_columns(due: float) -> dict:
    """The questions-table columns of a question that is new (again)."""
    return {"stage": 0, "fsrs": "", "due": due, "reps": 0, "lapses": 0, "stability": None, "difficulty": None,
            "last_attempt": None}


def forget(db: Database, question_ids, now: float | None = None) -> None:
    """Make questions new again (their attempts stay logged)."""
    now = time.time() if now is None else now
    db.update_questions(question_ids, **new_schedule_columns(now), updated_at=now)


def set_due(db: Database, question_ids, first: int, last: int | None = None, now: float | None = None) -> int:
    """Make questions due in `first` days (0 = today), or spread at random over first..last days.
    New questions are left in the new queue. Returns how many were rescheduled."""
    import random
    now = time.time() if now is None else now
    moved = 0
    for question_id in question_ids:
        row = db.question(question_id)
        if row is None or row["stage"] == 0:
            continue
        days = random.randint(first, last) if last is not None and last > first else first
        due = day_start(now) + days * 86400 + (now - day_start(now) if days == 0 else 0)
        fsrs_card = _fsrs_card(row, now)
        fsrs_card.due = dt(due)
        if fsrs_card.state != State.Review:  # learning steps don't fit a date: graduate it
            fsrs_card.state, fsrs_card.step = State.Review, None
        db.update_question(question_id, **schedule_columns(fsrs_card), updated_at=now)
        moved += 1
    return moved


def retrievability(db: Database | None, row, now: float | None = None) -> float | None:
    """How likely the question is to be recalled now (None while new)."""
    if not row["fsrs"]:
        return None
    now = time.time() if now is None else now
    return scheduler(db).get_card_retrievability(Card.from_json(row["fsrs"]), dt(now))


def answered_in_chat(
    db: Database, message_id: int, quiz_index: int, question_index: int, verdict: str | None,
    response: dict, now: float | None = None,
) -> bool:
    """A quiz question answered (and graded) in the chat: its first attempt, if it has had
    none. Later answers (retakes) don't change the schedule. True if it was recorded."""
    row = db.question_for(message_id, quiz_index, question_index)
    rating = response.get("rating") if "rating" in response else suggested_rating(verdict)
    if row is None or db.attempts(row["id"]) or rating not in quiz.RATINGS:
        return False
    attempt(db, row["id"], rating, now, response, verdict, source="chat")
    return True


def previews(row, now: float | None = None, db: Database | None = None) -> dict[int, str]:
    """For each rating, when the question would come back: {1: "1m", 3: "10m", …}."""
    now = time.time() if now is None else now
    sched = scheduler(db)
    out = {}
    for rating in quiz.RATINGS:
        after, _ = sched.review_card(_fsrs_card(row, now), Rating(rating), dt(now))
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
# Due questions; args (next_day(now), now). due_sql("c.") is the same over a table alias.
def due_sql(alias: str = "") -> str:
    return f"{alias}suspended=0 AND (({alias}stage=2 AND {alias}due<?) OR ({alias}stage IN (1,3) AND {alias}due<=?))"


DUE = due_sql()
NEW = "suspended=0 AND stage=0"


def new_left_today(db: Database, now: float) -> int:
    return max(new_per_day(db) - db.new_questions_attempted_since(day_start(now)), 0)


def counts(db: Database, book_id: int | None, now: float | None = None, deck: int | None = None,
           tag: str | None = None, picks=()) -> dict:
    """What there is to practice in a book, deck or tag (a section; or everywhere, with none), or
    in any of picks (Database._scope): due questions, new ones that can be introduced today, and
    the total."""
    now = time.time() if now is None else now
    new = db.count_questions(book_id, NEW, deck_id=deck, tag=tag, picks=picks)
    return {
        "due": db.count_questions(book_id, DUE, (next_day(now), now), deck_id=deck, tag=tag, picks=picks),
        "new": min(new, new_left_today(db, now)),
        "new_total": new,
        "total": db.count_questions(book_id, deck_id=deck, tag=tag, picks=picks),
        "suspended": db.count_questions(book_id, "suspended=1", deck_id=deck, tag=tag, picks=picks),
    }


def left_to_practice(db: Database, book_id: int | None, now: float | None = None, deck: int | None = None,
                     tag: str | None = None, exclude=(), picks=()) -> int:
    """How many questions are left to practice today (due, and new ones today's allowance
    allows), leaving out exclude (questions skipped this session)."""
    now = time.time() if now is None else now
    due = db.count_questions(book_id, *_not_in(DUE, (next_day(now), now), exclude), deck_id=deck, tag=tag, picks=picks)
    new = db.count_questions(book_id, *_not_in(NEW, (), exclude), deck_id=deck, tag=tag, picks=picks)
    return due + min(new, new_left_today(db, now))


def _not_in(where: str, args: tuple, exclude) -> tuple[str, tuple]:
    """where, also leaving out the questions with the ids in exclude."""
    exclude = tuple(exclude)
    if not exclude:
        return where, args
    return f"({where}) AND id NOT IN ({','.join('?' * len(exclude))})", (*args, *exclude)


def deck_counts(db: Database, now: float | None = None) -> dict[int, dict]:
    """For every deck, its questions and those of the decks under it: {deck id: {due, new, total}}
    (new: questions waiting, not capped by today's allowance)."""
    now = time.time() if now is None else now
    own = {r["deck_id"]: r for r in db.conn.execute(
        f"SELECT deck_id, SUM({DUE}) AS due, SUM({NEW}) AS new, COUNT(*) AS total FROM questions GROUP BY deck_id",
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


def next_question(db: Database, book_id: int | None, now: float | None = None, skip=(), deck: int | None = None,
                  tag: str | None = None, exclude=(), picks=()):
    """The question to practice next: due learning and learned questions first (most overdue
    first), then new ones while today's allowance lasts, then learning ones due within
    LEARN_AHEAD. skip: ids of questions not to show again yet (the one just answered), unless
    nothing else is left. exclude: ids of questions never to show (skipped this session)."""
    now = time.time() if now is None else now
    tiers = [(DUE, (next_day(now), now), "due, id")]
    if new_left_today(db, now):
        tiers.append((NEW, (), "created_at, id"))
    tiers.append(("suspended=0 AND stage IN (1,3) AND due<=?", (now + LEARN_AHEAD,), "due, id"))
    fallback = None
    for where, args, order in tiers:
        where, args = _not_in(where, args, exclude)
        for row in db.questions(book_id, where, args, order=order, limit=len(skip) + 1, deck_id=deck, tag=tag,
                                picks=picks):
            if row["id"] not in skip:
                return row
            fallback = fallback or row
    return fallback


def next_due(db: Database, book_id: int | None, now: float | None = None, deck: int | None = None,
             tag: str | None = None, picks=()) -> float | None:
    """When the next question that isn't due yet will be."""
    now = time.time() if now is None else now
    row = db.questions(book_id, "suspended=0 AND stage>0 AND due>?", (now,), order="due", limit=1, deck_id=deck,
                   tag=tag, picks=picks)
    return row[0]["due"] if row else None


# -- existing quizzes ----------------------------------------------------------------------
def backfill(db: Database) -> int:
    """Once: practice questions for the quizzes in replies written before practice existed, with
    the answers already given counted as first attempts (at the time they were given)."""
    if db.get_setting(BACKFILL_SETTING) == "1":
        return 0
    added = 0
    rows = db.conn.execute(
        "SELECT m.id, m.text, m.created_at, c.book_id, c.id AS chat_id FROM messages m "
        "JOIN chats c ON c.id=m.chat_id WHERE m.role='assistant' AND m.text LIKE '%quiz%' ORDER BY m.id"
    ).fetchall()
    for m in rows:
        if not add_quiz_questions(db, m["book_id"], m["id"], m["text"], now=m["created_at"]):
            continue
        added += 1
        for a in db.quiz_answers(m["chat_id"]).get(m["id"], []):
            if a["status"] == "done":
                answered_in_chat(db, m["id"], a["quiz"], a["question"], a["verdict"], json.loads(a["response"]),
                                 now=a["created_at"])
    db.set_setting(BACKFILL_SETTING, "1")
    return added


# -- parameters ----------------------------------------------------------------------------
MIN_OPTIMIZE_ATTEMPTS = 400  # below this, FSRS's defaults are a better bet than a fit


def optimizable_attempts(db: Database) -> int:
    """Attempts at questions past their first, which is what the optimizer learns from."""
    return db.conn.execute(
        "SELECT COUNT(*) FROM attempts r JOIN (SELECT question_id, MIN(attempted_at) AS first FROM attempts "
        "GROUP BY question_id) f USING (question_id) WHERE r.attempted_at>f.first"
    ).fetchone()[0]


def attempt_logs(db: Database) -> list:
    """Every attempt as an fsrs.ReviewLog, for fit() (which may run off the GUI thread)."""
    from fsrs import ReviewLog

    return [
        ReviewLog(card_id=r["question_id"], rating=Rating(r["rating"]), review_datetime=dt(r["attempted_at"]),
                  review_duration=r["duration"] or None)
        for r in db.conn.execute("SELECT question_id, rating, attempted_at, duration FROM attempts "
                                 "ORDER BY attempted_at, id")
    ]


def fit(logs: list) -> list[float]:
    """FSRS weights fitted to the attempts (needs the optimizer's PyTorch)."""
    from fsrs import Optimizer

    return [float(w) for w in Optimizer(logs).compute_optimal_parameters()]


def set_parameters(db: Database, weights) -> None:
    """Use these FSRS weights from now on ([] or None: the defaults). Questions keep their memory
    state; the new weights shape the intervals after their next attempts."""
    db.set_setting(PARAMETERS_SETTING, json.dumps(list(fsrs6_parameters(weights))) if weights else "")
