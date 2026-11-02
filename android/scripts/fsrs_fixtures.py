"""Writes core/src/test/resources/fsrs_cases.json: what py-fsrs (the desktop's scheduler) does
on random review sequences, for FsrsTest to check the Kotlin port against.

Run with the desktop's Python, which has fsrs installed:
    ../.pixi/envs/default/bin/python scripts/fsrs_fixtures.py      (from android/)

Times are integer microseconds since the epoch (UTC), as py-fsrs keeps them. Sequences with
fuzzing on record the random() draws each review made, so the Kotlin side can replay them.
"""

from __future__ import annotations

import json
import math
import os
import random
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fsrs
import fsrs.scheduler as fsrs_scheduler
from fsrs import Card, Rating, Scheduler, State

HERE = Path(__file__).resolve().parent
# The desktop app's source (STUDY_READER_SRC overrides, for a copy of android/ elsewhere).
sys.path.insert(0, os.environ.get("STUDY_READER_SRC", str(HERE.parent.parent / "src")))
from study_reader.srs import fsrs6_parameters  # noqa: E402

OUT = HERE.parent / "core" / "src" / "test" / "resources" / "fsrs_cases.json"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SECOND = 1_000_000
DAY = 86400 * SECOND

DEFAULT = list(fsrs_scheduler.DEFAULT_PARAMETERS)
# The desktop's own optimized set (initial stabilities fitted, the rest default).
CUSTOM = [0.317163, 1.934545, 3.450644, 12.410649] + DEFAULT[4:]
# A fully custom FSRS-6 set (non-zero w19, a different decay).
CUSTOM_FULL = [0.2172, 1.1771, 3.2602, 16.1507, 7.0114, 0.57, 2.0966, 0.0069, 1.5261, 0.112, 1.0178,
               1.849, 0.1133, 0.3127, 2.2934, 0.2191, 3.0004, 0.7536, 0.3332, 0.1437, 0.2]
# FSRS-5's 19 weights, which fsrs6_parameters extends with w19 = 0, w20 = 0.5.
FSRS5 = [0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575, 0.1192, 1.01925,
         1.9395, 0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655, 0.6621]
PARAMETER_SETS = {"default": DEFAULT, "custom": CUSTOM, "custom_full": CUSTOM_FULL,
                  "fsrs5": list(fsrs6_parameters(FSRS5))}


def at(us: int) -> datetime:
    return EPOCH + timedelta(microseconds=us)


def micros(d: datetime) -> int:
    return (d - EPOCH) // timedelta(microseconds=1)


class Draws:
    """Stands in for scheduler.random: deterministic draws, logged per review."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.log: list[float] = []

    def __call__(self) -> float:
        x = self.rng.random()
        self.log.append(x)
        return x


def gap(rng: random.Random, card: Card, now: int, whole: bool) -> int:
    """Microseconds to the next review: seconds to months, around/before/after due, same day."""
    to_due = micros(card.due) - now
    k = rng.random()
    if k < 0.2:
        g = rng.randint(1, 900) * SECOND
    elif k < 0.32:
        g = rng.randint(900, 86399) * SECOND
    elif k < 0.47 and to_due > 0:
        g = to_due + rng.randint(-3600, 3600) * SECOND
    elif k < 0.55 and to_due > 0:
        g = to_due
    elif k < 0.65 and to_due > 0:
        g = int(to_due * rng.uniform(0.05, 0.8))
    elif k < 0.8 and to_due > 0:
        g = int(to_due * rng.uniform(1.1, 4)) + rng.randint(0, 30) * DAY
    elif k < 0.9:
        g = int(rng.uniform(20, 500) * DAY)
    elif k < 0.97:  # whole days, give or take a microsecond: the .days boundaries
        g = rng.randint(1, 60) * DAY + rng.choice([-1, 0, 1])
    else:  # before the last review (clocks disagree)
        g = -rng.randint(1, 7200) * SECOND
    g += rng.randint(0, SECOND - 1) if not whole and k < 0.97 else 0
    if whole:
        g -= g % SECOND
    return max(g, -7200 * SECOND) or SECOND


def probes(rng: random.Random, card: Card, now: int) -> list[int]:
    due = micros(card.due)
    return [now, now + DAY - 1, due, now + int(rng.uniform(-2, 400) * DAY)]


def scheduler_dict(params, retention, learning=(60, 600), relearning=(600,), maximum=36500, fuzz=False):
    return {"parameters": list(params), "desired_retention": retention, "learning_steps": list(learning),
            "relearning_steps": list(relearning), "maximum_interval": maximum, "enable_fuzzing": fuzz}


def make_scheduler(d) -> Scheduler:
    return Scheduler(
        parameters=d["parameters"], desired_retention=d["desired_retention"],
        learning_steps=[timedelta(seconds=s) for s in d["learning_steps"]],
        relearning_steps=[timedelta(seconds=s) for s in d["relearning_steps"]],
        maximum_interval=d["maximum_interval"], enable_fuzzing=d["enable_fuzzing"],
    )


def sequence(name, sched_d, seed, length, start_card: Card | None = None, start: int | None = None):
    rng = random.Random(seed)
    sched = make_scheduler(sched_d)
    draws = Draws(seed + 1)
    fsrs_scheduler.random = draws
    whole = rng.random() < 0.25
    now = start if start is not None else rng.randint(1_700_000_000, 1_830_000_000) * SECOND
    if not whole and start is None:
        now += rng.randint(0, SECOND - 1)
    card = start_card or Card(card_id=rng.randint(1_600_000_000_000, 1_800_000_000_000), due=at(now))
    out = {"name": name, "scheduler": sched_d, "card": card.to_json(),
           "retrievability": [[t, sched.get_card_retrievability(card, at(t))] for t in (now, now + DAY)],
           "steps": []}
    for _ in range(length):
        rating = rng.choices([1, 2, 3, 4], weights=[0.17, 0.13, 0.55, 0.15])[0]
        draws.log = []
        card, _log = sched.review_card(card, Rating(rating), at(now))
        out["steps"].append({
            "rating": rating, "time": now, "random": draws.log, "card": card.to_json(),
            "retrievability": [[t, sched.get_card_retrievability(card, at(t))] for t in probes(rng, card, now)],
        })
        now += gap(rng, card, now, whole)
    return out


def stray_card(seed: int, state: State, step: int, steps: int) -> Card:
    """A card in learning or relearning, at a step reached with more steps than the scheduler
    that gets it next has."""
    rng = random.Random(seed)
    sched = Scheduler(learning_steps=[timedelta(minutes=m) for m in (1, 10, 60, 300)][:steps],
                      relearning_steps=[timedelta(minutes=m) for m in (10, 60, 300)][:steps], enable_fuzzing=False)
    now = rng.randint(1_700_000_000, 1_830_000_000) * SECOND + rng.randint(0, SECOND - 1)
    card = Card(card_id=rng.randint(1_600_000_000_000, 1_800_000_000_000), due=at(now))
    if state == State.Relearning:
        card, _ = sched.review_card(card, Rating.Easy, at(now))
        now += 5 * DAY
        card, _ = sched.review_card(card, Rating.Again, at(now))
    for _ in range(step):
        now += 600 * SECOND
        card, _ = sched.review_card(card, Rating.Good, at(now))
    assert card.state == state and card.step == step, (card.state, card.step)
    return card


def sequences() -> list[dict]:
    out = []
    seed = 1000
    for pname, params in PARAMETER_SETS.items():
        for retention in (0.8, 0.9, 0.95):
            for i in range(8 if pname != "custom_full" else 5):
                seed += 7
                out.append(sequence(f"{pname} r={retention} #{i}", scheduler_dict(params, retention), seed, 24))
    for pname in ("default", "custom", "fsrs5"):
        for retention in (0.8, 0.9, 0.95):
            for i in range(4):
                seed += 7
                out.append(sequence(f"fuzz {pname} r={retention} #{i}", scheduler_dict(params_of(pname), retention, fuzz=True),
                                    seed, 30))
    edge = [
        ("no steps", dict(learning=(), relearning=())),
        ("no steps fuzz", dict(learning=(), relearning=(), fuzz=True)),
        ("one step", dict(learning=(300,), relearning=(600,))),
        ("one step, two relearning", dict(learning=(300,), relearning=(600, 3600))),
        ("three steps", dict(learning=(60, 600, 3600), relearning=(600, 1800, 7200))),
        ("odd steps", dict(learning=(7, 13), relearning=(1, 3))),
        ("max 30 fuzz", dict(maximum=30, fuzz=True)),
        ("max 365 fuzz", dict(maximum=365, fuzz=True)),
        ("max 5 fuzz", dict(maximum=5, fuzz=True)),
        ("max 2 fuzz", dict(maximum=2, fuzz=True)),
        ("max 1", dict(maximum=1)),
    ]
    for name, kw in edge:
        for i in range(5):
            seed += 7
            out.append(sequence(f"{name} #{i}", scheduler_dict(CUSTOM, 0.9, **kw), seed, 20))
    # Cards whose step is past the scheduler's last step (it had more steps when they got there).
    for i, (state, step, steps, kw) in enumerate([
        (State.Learning, 2, 4, {}), (State.Learning, 3, 4, {}), (State.Learning, 1, 2, dict(learning=(300,))),
        (State.Learning, 3, 4, dict(learning=(60, 600, 3600))), (State.Relearning, 1, 2, {}),
        (State.Relearning, 2, 3, {}), (State.Relearning, 2, 3, dict(relearning=(600, 3600))),
        (State.Learning, 2, 4, dict(learning=())), (State.Relearning, 1, 2, dict(relearning=())),
    ]):
        for rating_seed in range(6):
            seed += 7
            card = stray_card(seed, state, step, steps)
            out.append(sequence(f"stray {state.name} step {step} {kw} #{rating_seed}",
                                scheduler_dict(DEFAULT, 0.9, **kw), seed, 6, start_card=card,
                                start=micros(card.last_review) + rng_gap(seed)))
    return out


def rng_gap(seed: int) -> int:
    return random.Random(seed).choice([30 * SECOND, 20 * 60 * SECOND, 2 * DAY + 17])


def params_of(name: str) -> list[float]:
    return PARAMETER_SETS[name]


def odd_doubles(rng: random.Random) -> list[float]:
    fixed = [1e-05, 0.0001, 0.00012345, 0.001, 1e16, 1234567890123456.0, 12345678901234567.0, 1e22, 1e23,
             9.999999999999999e22, 0.1, 0.30000000000000004, 2.5, 100.0, 5e-324, 2.2250738585072014e-308,
             1.7976931348623157e308, 3.450644, 1 / 3, 2 / 3 * 1e-7, 1.2345678901234567e-5, 123456789.0,
             4.35, 0.5, 1.0, 36500.0, 2.0 ** 53, 2.0 ** 53 + 2.0, 2 ** -1074 * 3, -0.25, -1e-300, 8.41e21]
    out = list(fixed)
    for _ in range(300):
        exp = rng.choice([rng.randint(1023 - 30, 1023 + 30), rng.randint(1, 2046)])
        bits = (exp << 52) | rng.getrandbits(52)
        out.append(struct.unpack("<d", struct.pack("<Q", bits))[0])
    for _ in range(100):
        out.append(round(rng.uniform(0, 50), rng.randint(1, 8)))
    return out


def roundtrips() -> list[str]:
    rng = random.Random(7)
    out = []
    times = [0, 1, 100_000, 999_999, 500_000]
    for i, x in enumerate(odd_doubles(rng)):
        base = rng.randint(1_700_000_000, 1_830_000_000) * SECOND
        due = base + times[i % len(times)] + rng.choice([0, 0, rng.randint(0, 10**12)]) // SECOND * SECOND
        state = [State.Learning, State.Review, State.Relearning][i % 3]
        card = Card(card_id=rng.randint(1, 2 ** 53), state=state, step=None if state == State.Review else i % 4,
                    stability=abs(x) if i % 2 else None if i % 7 == 0 else x, difficulty=rng.uniform(1, 10),
                    due=at(due), last_review=None if i % 11 == 0 else at(base - rng.randint(0, 10**11)))
        out.append(card.to_json())
    # Dates far from now, and before the epoch.
    for d in ["2000-02-29T00:00:00+00:00", "1969-12-31T23:59:59.500000+00:00", "2126-01-01T12:34:56.000001+00:00",
              "1999-12-31T23:59:59.999999+00:00", "0999-01-02T03:04:05+00:00"]:
        out.append(Card(card_id=5, state=State.Review, stability=12.5, difficulty=5.0,
                        due=datetime.fromisoformat(d), last_review=datetime.fromisoformat(d)).to_json())
    return out


def from_json_cases() -> list[list[str]]:
    """[input, Card.from_json(input).to_json()]: inputs py normalizes (int floats, zero stability,
    a learning card without a step, other offsets and spellings py reads)."""
    inputs = [
        '{"card_id": 1, "state": 1, "step": null, "stability": null, "difficulty": null, "due": "2026-10-26T19:06:47.296319+00:00", "last_review": null}',
        '{"card_id": 2, "state": 2, "step": null, "stability": 3, "difficulty": 7, "due": "2026-10-26T19:06:47+00:00", "last_review": "2026-10-20T19:06:47+00:00"}',
        '{"card_id": 3, "state": 2, "step": null, "stability": 0, "difficulty": 0.0, "due": "2026-10-26T19:06:47+00:00", "last_review": "2026-10-20T19:06:47+00:00"}',
        '{"card_id": 4, "state": 3, "step": 0, "stability": 1.5, "difficulty": 2.5, "due": "2026-10-26T19:06:47Z", "last_review": "2026-10-20T19:06:47.5Z"}',
        '{"card_id": 5, "state": 2, "step": null, "stability": 1.5e2, "difficulty": 25E-1, "due": "2026-10-26T19:06:47.123+00:00", "last_review": "2026-10-20T19:06:47.000000+00:00"}',
        '{"card_id": "6", "state": "2", "step": null, "stability": "1.25", "difficulty": "2", "due": "2026-10-26T19:06:47+00:00", "last_review": ""}',
        '{"last_review": null, "due": "2026-10-26T19:06:47+00:00", "difficulty": null, "stability": null, "step": 3, "state": 1, "card_id": 7}',
    ]
    return [[s, Card.from_json(s).to_json()] for s in inputs]


def fsrs6_cases() -> list[dict]:
    weights = [DEFAULT, CUSTOM, CUSTOM_FULL, FSRS5, DEFAULT[:20], DEFAULT[:18], DEFAULT + [0.1], [],
               [0.0] + DEFAULT[1:], DEFAULT[:20] + [0.9], DEFAULT[:20] + [0.1], DEFAULT[:20] + [0.8],
               FSRS5[:4] + [11.0] + FSRS5[5:], [100.0, 100.0, 100.0, 100.0] + DEFAULT[4:],
               [100.00001] + DEFAULT[1:], list(fsrs_scheduler.LOWER_BOUNDS_PARAMETERS),
               list(fsrs_scheduler.UPPER_BOUNDS_PARAMETERS), [float("nan")] + DEFAULT[1:], [-0.5] + FSRS5[1:]]
    out = []
    for w in weights:
        try:
            out.append({"weights": w, "result": list(fsrs6_parameters(w))})
        except ValueError:
            out.append({"weights": w, "error": True})
    return out


def finite(x):
    if isinstance(x, float) and not math.isfinite(x):
        return "NaN"
    if isinstance(x, list):
        return [finite(v) for v in x]
    if isinstance(x, dict):
        return {k: finite(v) for k, v in x.items()}
    return x


def main() -> None:
    seqs = sequences()
    data = {
        "about": "py-fsrs results for FsrsTest; made by android/scripts/fsrs_fixtures.py",
        "fsrs_version": fsrs.__version__ if hasattr(fsrs, "__version__") else "6.3.2",
        "sequences": seqs,
        "roundtrip": roundtrips(),
        "from_json": from_json_cases(),
        "fsrs6_parameters": finite(fsrs6_cases()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n")
    steps = sum(len(s["steps"]) for s in seqs)
    fuzzed = sum(len(st["random"]) for s in seqs for st in s["steps"])
    print(f"{len(seqs)} sequences, {steps} reviews ({fuzzed} fuzzed), {OUT.stat().st_size // 1024} KiB -> {OUT}")


if __name__ == "__main__":
    main()
