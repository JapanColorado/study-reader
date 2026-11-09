"""Builds a desktop database with books, sections, decks and a review history, exports it with
phone_sync.export_snapshot, and records what the desktop's srs.py and gamify.py say about it, so
the Kotlin port can be checked against the real thing (QueueTest.kt).

Run with the desktop's Python (from android/):
    PYTHONPATH=../src /home/russell/Projects/study-reader/.pixi/envs/default/bin/python scripts/queue_fixtures.py
Writes core/src/test/resources/queue_snapshot.db and queue_cases.json. Times are local: run the
tests in the same time zone (TZ is pinned to UTC here and in the Gradle test task).
"""

import json
import os
import random
import sys
import tempfile
from pathlib import Path

os.environ["TZ"] = "UTC"
import time  # noqa: E402

time.tzset()

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
tmp = Path(tempfile.mkdtemp())
os.environ["STUDY_READER_DATA"] = str(tmp / "data")

from study_reader import gamify, phone_sync, srs  # noqa: E402
from study_reader.db import Database  # noqa: E402

OUT = HERE.parent / "core" / "src" / "test" / "resources"
NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
DAY = 86400
random.seed(7)

db = Database(tmp / "t.db")
db.set_setting(srs.NEW_PER_DAY_SETTING, "6")
db.set_setting(gamify.GOAL_SETTING, "5")

QUESTIONS = [
    {"type": "choice", "question": "How many legs?", "choices": ["Six", "Seven"], "answer": 1},
    {"type": "truefalse", "question": "Near a window?", "answer": False},
    {"type": "numeric", "question": "Rinse time?", "answer": "41", "unit": "s"},
    {"type": "written", "question": "Why rinse?", "answer": "To calibrate.", "rubric": "tea"},
    {"type": "flashcard", "question": "Front", "answer": "Back"},
]

books = []
for b, title in enumerate(["The Zorblatt Manual", "Marmalade: A History"]):
    book_id = db.upsert_book(f"h{b}", f"/{b}.pdf", title, 60)["id"]
    toc = [[1, "Chapter 1 Basics", 0], [2, "1.1 Legs", 2], [2, "1.2 Tea", 8], [1, "Chapter 2 Storage", 20],
           [2, "2.1 Windows", 22], [2, "2.2 Exercises", 30]]
    chat = db.create_chat(book_id, "Quiz")
    for n in range(12):
        db.add_message(chat, "user", "Quiz me", [])
        page = random.choice([1, 3, 9, 21, 23, 31, 45])
        q = dict(random.choice(QUESTIONS), page=page + 1)
        if q["type"] == "flashcard":
            q["question"] = f"Front {b}-{n}"
        text = f"```quiz Quiz {n}\n{json.dumps({'questions': [q, dict(QUESTIONS[0], page=page + 1)]})}\n```"
        reply = db.add_message(chat, "assistant", text)
        srs.add_quiz_cards(db, book_id, reply, text, now=NOW - (40 - n) * DAY)
    srs.learn_toc(db, book_id, toc)
    books.append(book_id)

# A user deck tree of flashcards, outside the books.
for name in ("Languages::Japanese::Kanji", "Languages::Japanese::Vocab", "Languages::Spanish", "Misc"):
    deck = db.ensure_deck(name)
    for i in range(5):
        db.add_card_note("flashcard", {"front": f"{name} {i}", "back": "x"}, deck, tags=["vocab"] if "Vocab" in name else [],
                         now=NOW - (30 - i) * DAY)

# A review history: some cards reviewed over the last weeks, a few suspended, one flagged.
ids = [r["id"] for r in db.conn.execute("SELECT id FROM cards ORDER BY id")]
random.shuffle(ids)
for card_id in ids[:45]:
    t = NOW - random.randint(5, 35) * DAY + random.randint(0, DAY)
    while t < NOW - 600:
        srs.review(db, card_id, random.choice([1, 2, 3, 3, 3, 4]), now=t, duration=3000)
        card = db.card(card_id)
        t = max(card["due"], t + 60) + random.choice([0, 3600, DAY])
for card_id in ids[45:48]:
    db.update_card(card_id, suspended=1)
db.update_card(ids[50], flag=2)
# Reviews today: two new introductions (they count against the cap) and a few more.
for card_id in ids[52:54]:
    srs.review(db, card_id, 3, now=NOW - 1800)
gamify.load(db, NOW)

snap_dir = tmp / "sync"
snap_dir.mkdir()
phone_sync.export_snapshot(db, snap_dir, NOW)
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "queue_snapshot.db").write_bytes((snap_dir / phone_sync.SNAPSHOT).read_bytes())


def row(r):
    return dict(r) if r is not None else None


scopes = [{"book": None, "deck": None, "tag": None}]
scopes += [{"book": b, "deck": None, "tag": None} for b in books]
scopes += [{"book": None, "deck": d["id"], "tag": None} for d in db.decks()]
for b, secs in srs.sections(db, NOW).items():
    scopes += [{"book": b, "deck": None, "tag": s["tag"]} for s in secs]

cases = {"now": NOW, "new_left_today": srs.new_left_today(db, NOW), "scopes": [], "previews": {}, "sections": {},
         "deck_counts": {str(k): v for k, v in srs.deck_counts(db, NOW).items()}}
for sc in scopes:
    args = dict(deck=sc["deck"], tag=sc["tag"])
    first = srs.next_card(db, sc["book"], NOW, **args)
    skip = [first["id"]] if first else []
    second = srs.next_card(db, sc["book"], NOW, skip=skip, **args)
    cases["scopes"].append({**sc, "counts": srs.counts(db, sc["book"], NOW, **args),
                            "next": first["id"] if first else None, "next_skipping": second["id"] if second else None,
                            "next_due": srs.next_due(db, sc["book"], NOW, **args)})
for b, secs in srs.sections(db, NOW).items():
    cases["sections"][str(b)] = secs
srs.scheduler(db).enable_fuzzing = False  # previews comparable with the Kotlin port (Srs(fuzz = false))
for card in db.conn.execute("SELECT * FROM cards WHERE stage>0 ORDER BY id").fetchall():
    cases["previews"][str(card["id"])] = {str(k): v for k, v in srs.previews(card, NOW, db).items()}
cases["format_interval"] = {str(s): srs.format_interval(s) for s in
                            (0, 30, 59, 60, 89, 90, 150, 3599, 3600, 5400, 86399, 86400, 129600, 30 * DAY, 45 * DAY,
                             364 * DAY, 365 * DAY, 400 * DAY, 3650 * DAY)}
status = gamify.status(db, NOW)
cases["gamify_status"] = status
cases["gamify_state"] = gamify.load(db, NOW)
(OUT / "queue_cases.json").write_text(json.dumps(cases, indent=1, sort_keys=True))
print(f"{len(cases['scopes'])} scopes, {len(cases['previews'])} previews written to {OUT}")
