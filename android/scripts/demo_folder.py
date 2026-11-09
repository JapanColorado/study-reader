"""A desktop database with a few cards of every kind, and the sync folder it exports, for trying
the app on an emulator or phone without touching your real data:

    PYTHONPATH=../src <desktop python> scripts/demo_folder.py <dir>
    adb push <dir>/sync/. /sdcard/Documents/study-sync/      # then pick that folder in the app

After reviewing on the device, pull phones/ back and apply it to the demo database:
    adb pull /sdcard/Documents/study-sync/phones <dir>/sync/
    PYTHONPATH=../src <desktop python> scripts/demo_folder.py <dir> --apply
"""

import json
import os
import sys
import time
from pathlib import Path

base = Path(sys.argv[1]).resolve()
os.environ["STUDY_READER_DATA"] = str(base / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from study_reader import phone_sync, srs  # noqa: E402
from study_reader.db import Database, media_dir  # noqa: E402

db_path = base / "desktop.db"
if "--apply" in sys.argv:
    db = Database(db_path)
    result = phone_sync.sync(db, base / "sync")
    print(f"applied {result.applied} events from {result.batches} batches; reviewed {sorted(set(result.reviewed))}; "
          f"errors {result.errors}")
    for r in db.conn.execute("SELECT card_id, rating, source, reviewed_at, was_new FROM reviews ORDER BY id DESC LIMIT 10"):
        print(dict(r))
    print("suspended/flagged:", [dict(r) for r in db.conn.execute("SELECT id, suspended, flag, due FROM cards WHERE suspended=1 OR flag>0")])
    print("rewards:", db.get_setting("gamify_state"))
    sys.exit()

base.mkdir(parents=True, exist_ok=True)
db = Database(db_path)
now = time.time()
book = db.upsert_book("demo", "/demo.pdf", "The Zorblatt Manual", 40)["id"]
chat = db.create_chat(book, "Quiz")
db.add_message(chat, "user", "Quiz me", [])
quiz = {"questions": [
    {"type": "choice", "question": "How many legs does a **zorblatt** have?", "choices": ["Six", "Seven", "$\\frac{15}{2}$"],
     "answer": 1, "page": 2, "explanation": "Page 2 says seven."},
    {"type": "truefalse", "question": "A zorblatt may be stored near a window.", "answer": False, "page": 3},
    {"type": "numeric", "question": "A rinse takes $41$ s per leg. How long for all seven legs, in minutes?",
     "answer": "287/60", "unit": "min", "page": 2},
    {"type": "written", "question": "Why is a zorblatt rinsed in cold tea?", "answer": "To calibrate it.",
     "rubric": "mentions calibration", "page": 2},
    {"type": "flashcard", "question": "What is $\\int_0^1 x^2\\,dx$?", "answer": "$\\tfrac13$", "page": 30},
]}
text = f"```quiz Zorblatts\n{json.dumps(quiz)}\n```"
reply = db.add_message(chat, "assistant", text)
srs.add_quiz_cards(db, book, reply, text, now=now - 3600)
srs.learn_toc(db, book, [[1, "Chapter 1 Basics", 0], [2, "1.1 Legs", 1], [1, "Chapter 2 Storage", 20]])
deck = db.ensure_deck("Languages::Japanese")
db.add_card_note("flashcard", {"front": "猫", "back": "cat (ねこ)"}, deck, now=now - 7200)
db.add_card_note("cloze", {"text": "The capital of Japan is {{c1::Tokyo}}."}, deck, now=now - 7100)
# An image, to check media.
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000010000000100806000000"
    "1ff3ff610000001f49444154789c63f8cfc0f09f8114c0c4c080034818"
    "0d1a3468d0a0e18a0100f29f1ff16fd1ab9b0000000049454e44ae426082")
(media_dir() / "dot.png").write_bytes(png)
db.add_card_note("flashcard", {"front": "What color is this?<br><img src=\"dot.png\" width=\"64\">", "back": "Orange"},
                 deck, now=now - 7000)
# One card already learned and due now.
first = db.conn.execute("SELECT id FROM cards ORDER BY id LIMIT 1").fetchone()[0]
srs.review(db, first, 3, now=now - 3 * 86400)
srs.review(db, first, 3, now=now - 86400 - 600)
(base / "sync").mkdir(exist_ok=True)
result = phone_sync.sync(db, base / "sync", force=True)
print(f"exported {db.conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0]} cards to {base / 'sync'}")
