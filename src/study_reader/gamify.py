"""Rewards for practicing, to make daily practice something to look forward to: XP and levels, a
combo that raises the XP multiplier, a daily goal, a streak that streak freezes can carry over a
missed day, achievements, and a summary of each session. Nothing here touches scheduling, and XP
never depends on the rating, so it never pays to rate a question better than it went.

The attempt log gives the streak, the calendar and today's count; XP, freezes and what was
unlocked live in one JSON setting (STATE_SETTING), made from the log the first time. Qt-free."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timedelta

from . import practice
from .db import Database
from .stats import DAY_EDGE, MATURE_DAYS, day_of, day_spans

ENABLED_SETTING = "gamify"  # "0" hides the rewards (they are still kept)
GOAL_SETTING = "daily_goal"
STATE_SETTING = "gamify_state"
DEFAULT_GOAL = 20
BASE_XP = 10  # per attempt, whatever its rating
NEW_XP = 5  # extra for a question's first attempt
COMBO_STEP = 5  # every this many questions in a row add 0.1 to the XP multiplier …
MAX_MULTIPLIER = 2.0  # … up to this
COMBO_PAUSE = 120  # seconds on one question that break the combo (the user probably stepped away)
MILESTONE_EVERY = 25  # questions in a session
QUICK_QUESTIONS = 5  # a quick session: this many questions …
QUICK_SECONDS = 5 * 60  # … or this long
FREEZE_EVERY = 7  # streak days that earn a streak freeze
MAX_FREEZES = 2
START_FREEZES = 1
COMEBACK_DAYS = 3  # days away after which practice is a comeback
HEAT_WEEKS = 18

LEVEL_TITLES = (
    (1, "Novice"), (5, "Apprentice"), (10, "Student"), (20, "Scholar"), (35, "Sage"), (50, "Polymath"),
    (75, "Luminary"), (100, "Grandmaster"),
)

# id, icon, name, what it takes, and the context value (see attempted()) that must reach the threshold.
ACHIEVEMENTS = (
    ("first", "🌱", "First steps", "Answer your first question", "attempts", 1),
    ("attempts_100", "💯", "Centurion", "Answer 100 questions", "attempts", 100),
    ("attempts_1000", "📚", "Bookworm", "Answer 1,000 questions", "attempts", 1000),
    ("attempts_10000", "🏛️", "Library of Alexandria", "Answer 10,000 questions", "attempts", 10000),
    ("streak_3", "🔥", "Warming up", "Reach a 3-day streak", "streak", 3),
    ("streak_7", "🗓️", "A week of it", "Reach a 7-day streak", "streak", 7),
    ("streak_30", "🌙", "Month of memory", "Reach a 30-day streak", "streak", 30),
    ("streak_100", "💎", "Centennial", "Reach a 100-day streak", "streak", 100),
    ("streak_365", "👑", "Year of learning", "Reach a 365-day streak", "streak", 365),
    ("goal_1", "🎯", "On target", "Meet your daily goal", "goals", 1),
    ("goal_30", "🏹", "Sharpshooter", "Meet your daily goal on 30 days", "goals", 30),
    ("combo_25", "⚡", "In the flow", "Answer 25 questions in a row without a long pause", "combo", 25),
    ("combo_100", "🌊", "Deep focus", "Answer 100 questions in a row without a long pause", "combo", 100),
    ("marathon", "🏃", "Marathon", "Answer 100 questions in one session", "session", 100),
    ("caught_up", "🧹", "Inbox zero", "Practice every question that's due", "caught_up", 1),
    ("quick", "🍪", "Snack-sized", "Finish a quick session", "quick", 1),
    ("mature_1", "🌳", "Taking root", "Get a question to a 3-week interval", "mature", 1),
    ("mature_100", "🌲", "Forest", "Have 100 questions at 3-week intervals or longer", "mature", 100),
    ("level_10", "⭐", "Rising star", "Reach level 10", "level", 10),
    ("level_25", "🌟", "Shining", "Reach level 25", "level", 25),
    ("night_owl", "🦉", "Night owl", "Practice after midnight", "night_owl", 1),
    ("early_bird", "🐦", "Early bird", "Practice before 7 am", "early_bird", 1),
    ("comeback", "🤝", "Welcome back", f"Come back after {COMEBACK_DAYS} or more days away", "comeback", 1),
)
# Achievement ids as they were once stored (in gamify_state, and in phone achievement events).
ACHIEVEMENT_ALIASES = {"reviews_100": "attempts_100", "reviews_1000": "attempts_1000",
                       "reviews_10000": "attempts_10000"}


def achievement_key(key: str) -> str:
    """An achievement id as it is now, from one perhaps stored under an older name."""
    return ACHIEVEMENT_ALIASES.get(key, key)


def enabled(db: Database) -> bool:
    return db.get_setting(ENABLED_SETTING, "1") != "0"


def daily_goal(db: Database) -> int:
    try:
        return max(int(db.get_setting(GOAL_SETTING, str(DEFAULT_GOAL))), 1)
    except ValueError:
        return DEFAULT_GOAL


# -- XP --------------------------------------------------------------------------------------
def level_xp(level: int) -> int:
    """The XP it takes to reach a level: 100 more for level 2, then 50 more for each level after."""
    return 100 * (level - 1) + 25 * (level - 1) * (level - 2)


def level(xp: int) -> dict:
    n = 1
    while level_xp(n + 1) <= xp:
        n += 1
    title = [t for at, t in LEVEL_TITLES if at <= n][-1]
    return {"level": n, "title": title, "xp": xp, "into": xp - level_xp(n), "need": level_xp(n + 1) - level_xp(n)}


def multiplier(combo: int) -> float:
    return min(1 + (combo // COMBO_STEP) / 10, MAX_MULTIPLIER)


# -- state -----------------------------------------------------------------------------------
def _defaults() -> dict:
    return {"xp": 0, "freezes": START_FREEZES, "frozen": [], "freeze_earned": "", "goals": 0, "goal_day": "",
            "achievements": {}}


def load(db: Database, now: float | None = None, logged: bool = False) -> dict:
    """The stored state, made from the attempt log the first time (achievements earned before
    then are unlocked quietly). logged: the last attempt in the log is one about to be counted."""
    raw = db.get_setting(STATE_SETTING)
    if raw:
        try:
            state = {**_defaults(), **json.loads(raw)}
        except ValueError:
            pass
        else:
            unlocked = state["achievements"]
            if any(k in ACHIEVEMENT_ALIASES for k in unlocked):
                renamed: dict = {}
                for k, at in unlocked.items():
                    k = achievement_key(k)
                    renamed[k] = min(at, renamed.get(k, at))
                state["achievements"] = renamed
            return state
    now = time.time() if now is None else now
    state = _defaults()
    n, new = db.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(was_new), 0) FROM attempts WHERE id<?",
        ((db.conn.execute("SELECT MAX(id) FROM attempts").fetchone()[0] or 0) + (not logged),),
    ).fetchone()
    state["xp"] = BASE_XP * n + NEW_XP * new
    days = practice_days(db)
    _unlock(state, {"attempts": n, "streak": _longest(days), "level": level(state["xp"])["level"],
                    "mature": _mature(db)}, now)
    _save(db, state)
    return state


def _save(db: Database, state: dict) -> None:
    db.set_setting(STATE_SETTING, json.dumps(state))


def practice_days(db: Database) -> dict[date, int]:
    """Attempts per practice day (days roll over at 4 am, as in practice.py)."""
    # Each day's attempts are counted through the attempts_time index; only the moments day_spans
    # can't place (a clock change's days, the very end of a day) are read and given to day_of.
    # Working out every attempt's local date, in SQL or here, scanned the whole log.
    lo, hi = db.conn.execute("SELECT MIN(attempted_at), MAX(attempted_at) FROM attempts").fetchone()
    if lo is None:
        return {}
    days: dict[date, int] = {}
    odd: list[tuple[float, float]] = []  # ranges of moments to place one by one
    spans = day_spans(lo, hi)
    for day, start, end, steady in spans:
        if not steady:
            odd.append((start, end))
            continue
        n, edge = db.conn.execute("SELECT COUNT(*), COALESCE(SUM(attempted_at>=?), 0) FROM attempts "
                                  "WHERE attempted_at>=? AND attempted_at<?", (end - DAY_EDGE, start, end)).fetchone()
        if n > edge:
            days[day] = n - edge
        if edge:
            odd.append((end - DAY_EDGE, end))
    odd += [(float("-inf"), spans[0][1]), (spans[-1][2], float("inf"))]
    for start, end in odd:
        for (t,) in db.conn.execute("SELECT attempted_at FROM attempts WHERE attempted_at>=? AND attempted_at<?",
                                    (start, end)):
            d = day_of(t)
            days[d] = days.get(d, 0) + 1
    return dict(sorted(days.items()))


def _attempts_on(db: Database, day: date) -> int:
    """practice_days(db).get(day, 0), reading only that day's attempts (and a few hours either side,
    for clock changes)."""
    start = datetime.combine(day, clock(practice.DAY_STARTS_AT_HOUR)).timestamp()
    rows = db.conn.execute("SELECT attempted_at FROM attempts WHERE attempted_at>=? AND attempted_at<?",
                           (start - 6 * 3600, start + 30 * 3600))
    return sum(day_of(t) == day for (t,) in rows)


def _mature(db: Database) -> int:
    return db.conn.execute(
        "SELECT COUNT(*) FROM questions WHERE stage=2 AND last_attempt IS NOT NULL AND due-last_attempt>=?",
        (MATURE_DAYS * 86400,),
    ).fetchone()[0]


def _interval(question) -> float:
    """A question's interval in days (0 unless it is learned)."""
    if question["stage"] != 2 or question["last_attempt"] is None:
        return 0
    return (question["due"] - question["last_attempt"]) / 86400


# -- streaks ---------------------------------------------------------------------------------
def _frozen(state: dict) -> set[date]:
    return {date.fromisoformat(d) for d in state["frozen"]}


def streak(days: dict[date, int], frozen: set[date], today: date) -> int:
    """Days practiced in a row up to today (or yesterday, if today has none yet). Frozen days
    bridge the run without adding to it."""
    covered = days.keys() | frozen
    d = today if today in days else today - timedelta(days=1)
    n = 0
    while d in covered:
        n += d in days
        d -= timedelta(days=1)
    return n


def _longest(days: dict[date, int]) -> int:
    longest = run = 0
    last = None
    for d in sorted(days):
        run = run + 1 if last is not None and (d - last).days == 1 else 1
        longest = max(longest, run)
        last = d
    return longest


def _settle(state: dict, days: dict[date, int], today: date) -> bool:
    """Spend streak freezes on the days missed just before today, if there are enough to carry
    the streak over all of them. True if any were spent."""
    covered = days.keys() | _frozen(state)
    missed = []
    d = today - timedelta(days=1)
    while d not in covered and len(missed) <= state["freezes"]:
        missed.append(d)
        d -= timedelta(days=1)
    if not missed or len(missed) > state["freezes"]:
        return False
    state["freezes"] -= len(missed)
    state["frozen"] += [m.isoformat() for m in missed]
    return True


# -- achievements ----------------------------------------------------------------------------
def _unlock(state: dict, ctx: dict, now: float) -> list[dict]:
    events = []
    for key, icon, name, desc, value, threshold in ACHIEVEMENTS:
        if key not in state["achievements"] and ctx.get(value, 0) >= threshold:
            state["achievements"][key] = now
            events.append({"kind": "achievement", "id": key, "icon": icon, "name": name, "desc": desc})
    return events


# -- events ----------------------------------------------------------------------------------
def attempted(db: Database, new: bool, combo_multiplier: float = 1.0, now: float | None = None, **ctx) -> list[dict]:
    """After an attempt was logged: its XP, and what it earned (a level, a streak freeze, the
    daily goal, achievements), as events for the page. ctx: the session's "combo" and "session"
    (questions answered)."""
    now = time.time() if now is None else now
    state = load(db, now, logged=True)
    days = practice_days(db)
    today = day_of(now)
    _settle(state, days, today)
    events = []
    before = level(state["xp"])["level"]
    gained = round((BASE_XP + NEW_XP * new) * combo_multiplier)
    state["xp"] += gained
    events.append({"kind": "xp", "xp": gained})
    after = level(state["xp"])
    if after["level"] > before:
        events.append({"kind": "level", **after})

    current = streak(days, _frozen(state), today)
    if current and current % FREEZE_EVERY == 0 and state["freeze_earned"] != today.isoformat() \
            and state["freezes"] < MAX_FREEZES:
        state["freezes"] += 1
        state["freeze_earned"] = today.isoformat()
        events.append({"kind": "freeze", "freezes": state["freezes"]})
    if days.get(today, 0) >= daily_goal(db):
        events += _goal_met(state, today)

    earlier = [d for d in days if d < today]
    comeback = days.get(today, 0) == 1 and bool(earlier) and (today - max(earlier)).days >= COMEBACK_DAYS
    if comeback:
        events.append({"kind": "welcome", "days": (today - max(earlier)).days})
    hour = datetime.fromtimestamp(now).hour
    events += _unlock(state, {
        **ctx, "attempts": sum(days.values()), "streak": current, "level": after["level"], "mature": _mature(db),
        "goals": state["goals"], "comeback": comeback, "night_owl": hour < practice.DAY_STARTS_AT_HOUR,
        "early_bird": practice.DAY_STARTS_AT_HOUR <= hour < 7,
    }, now)
    _save(db, state)
    return events


def _goal_met(state: dict, today: date) -> list[dict]:
    if state["goal_day"] == today.isoformat():
        return []
    state["goals"] += 1
    state["goal_day"] = today.isoformat()
    return [{"kind": "goal"}]


def finished(db: Database, caught_up: bool, quick: bool, now: float | None = None) -> list[dict]:
    """A session ended: by practicing everything there was (which meets the daily goal too), or
    by finishing a quick session."""
    now = time.time() if now is None else now
    state = load(db, now)
    events = []
    today = day_of(now)
    if caught_up and _attempts_on(db, today):
        events += _goal_met(state, today)
    events += _unlock(state, {"caught_up": caught_up, "quick": quick, "goals": state["goals"]}, now)
    _save(db, state)
    return events


def status(db: Database, now: float | None = None, days: dict[date, int] | None = None) -> dict:
    """For the practice overview: level and XP, the streak and freezes, today against the goal,
    the calendar of recent weeks, and the achievements. days: practice_days, if already read."""
    now = time.time() if now is None else now
    state = load(db, now)
    days = practice_days(db) if days is None else days
    today = day_of(now)
    if _settle(state, days, today):
        _save(db, state)
    current = streak(days, _frozen(state), today)
    goal = daily_goal(db)
    start = today - timedelta(days=HEAT_WEEKS * 7 - 1)
    return {
        **level(state["xp"]),
        "streak": current, "freezes": state["freezes"], "freeze_every": FREEZE_EVERY,
        "at_risk": current > 0 and today not in days,
        "lapsed": current == 0 and bool(days),
        "today": days.get(today, 0), "goal": goal,
        "goal_met": state["goal_day"] == today.isoformat() or days.get(today, 0) >= goal,
        "heat": {
            "start": start.isoformat(), "end": today.isoformat(),
            "days": [{"date": d.isoformat(), "count": n} for d, n in sorted(days.items()) if d >= start],
            "frozen": [d for d in state["frozen"] if d >= start.isoformat()],
        },
        "achievements": [
            {"id": key, "icon": icon, "name": name, "desc": desc, "unlocked": key in state["achievements"]}
            for key, icon, name, desc, _, _ in ACHIEVEMENTS
        ],
    }


# -- sessions --------------------------------------------------------------------------------
@dataclass
class Session:
    """One sitting on the Practice page: its combo, what it achieved, and how long it runs (limit
    "questions": QUICK_QUESTIONS questions; "time": QUICK_SECONDS; "": until nothing is left; the
    limit names are the page's). Times are time.monotonic()."""

    limit: str = ""
    started: float = field(default_factory=time.monotonic)
    attempted: int = 0
    passed: int = 0
    combo: int = 0
    best: int = 0
    xp: int = 0
    matured: int = 0
    stability: float = 0.0  # days of memory stability gained

    @property
    def multiplier(self) -> float:
        return multiplier(self.combo)

    def record(self, before, after, rating: int, seconds: float) -> list[dict]:
        """A question rated (its rows before and after the attempt), seconds after it was shown."""
        self.attempted += 1
        self.passed += rating > 1
        self.combo = 1 if seconds >= COMBO_PAUSE else self.combo + 1
        self.best = max(self.best, self.combo)
        self.matured += _interval(before) < MATURE_DAYS <= _interval(after)
        self.stability += max((after["stability"] or 0) - (before["stability"] or 0), 0)
        events = []
        if self.combo % COMBO_STEP == 0 and self.multiplier > multiplier(self.combo - 1):
            events.append({"kind": "combo", "combo": self.combo, "multiplier": self.multiplier})
        if self.attempted % MILESTONE_EVERY == 0:
            events.append({"kind": "milestone", "n": self.attempted})
        return events

    def over(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.limit == "questions":
            return self.attempted >= QUICK_QUESTIONS
        if self.limit == "time":
            return now - self.started >= QUICK_SECONDS
        return False

    def progress(self, remaining: int, now: float | None = None) -> dict:
        """For the progress bar: questions done of the total, or (timed) the seconds left."""
        now = time.monotonic() if now is None else now
        total = QUICK_QUESTIONS if self.limit == "questions" else self.attempted + remaining
        return {
            "limit": self.limit, "done": self.attempted, "total": total,
            "left": max(QUICK_SECONDS - (now - self.started), 0), "seconds": QUICK_SECONDS,
            "combo": self.combo, "multiplier": self.multiplier,
        }

    def summary(self, caught_up: bool, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        return {
            "attempted": self.attempted, "passed": self.passed, "seconds": round(now - self.started),
            "xp": self.xp, "best": self.best, "matured": self.matured, "stability": round(self.stability),
            "caught_up": caught_up, "quick": bool(self.limit) and self.over(now), "limit": self.limit,
        }
