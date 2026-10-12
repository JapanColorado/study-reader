"""Claude Conversion: Claude (Sonnet) rewrites review cards, e.g. ones migrated from Anki, into
the question types that fit them best (a free response Claude grades, a numeric problem, a
multiple-choice question, or a sharper flashcard), or says to keep them. Quiz questions never
become flashcards, and a flashcard proposed for a card that already says the same is kept.
Each rewrite is a proposal the user accepts or rejects; an accepted one replaces the card's
content (a flashcard as a Basic note, anything else as a quiz note) and keeps its schedule and
review history. Proposals live in the conversions table. Qt-free."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import cardmodel, quiz
from . import claude_client as cc
from .db import Database, media_dir

MODEL = "sonnet"
BATCH_SIZE = 8  # cards per request
IMAGE_BATCH_SIZE = 3  # cards per request when they have images
MAX_IMAGES = 6  # attached per request
MAX_IMAGE_BYTES = 3_000_000
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}

SYSTEM_PROMPT = (
    "You improve spaced-repetition cards for a study app. Most came from Anki as plain flashcards "
    "or cloze deletions. The app can also show questions the learner answers before seeing the "
    "answer: \"free\" (a written answer that you grade later against a model answer and rubric), "
    "\"numeric\" (a number, typed as math, checked within a tolerance), \"choice\" (multiple "
    "choice) and \"truefalse\", besides \"flashcard\" (recall, then self-rate). For each card, "
    "decide whether another type would make the learner think harder about what the card is "
    "really testing, and if so rewrite it. A card that already makes the learner answer "
    "(choice, truefalse, numeric, free) never becomes a flashcard. Reply with JSON only."
)

INSTRUCTIONS = """Rewrite each card below as the question type that best fits it, or keep it.

Choosing:
- "free" for explaining, deriving, comparing or applying an idea: the learner writes an answer
  that is graded against your model answer and rubric. Prefer it for conceptual cards.
- "numeric" when the card asks for a single quantity that can be computed. Pose the problem so
  it needs the idea, not memorized arithmetic.
- "choice" when there are plausible confusions to tell apart (2-5 choices, plausible wrong ones).
- "truefalse" for a crisp claim that people commonly get wrong.
- "flashcard" for pure recall worth memorizing verbatim (a definition, a formula, a name), and
  only for cards that are flashcards, cloze or image occlusion now: improve a vague front or an
  overloaded back. Never turn a "choice", "truefalse", "numeric" or "free" card into a flashcard;
  keep it, or rewrite it as another of those types if that is clearly better.
- "keep" when the card already works well as it is (good cloze, recall and quiz cards usually
  do), when a rewrite would only reword it, or when it depends on an image or context you can't
  see.
Never add facts that aren't in the card or standard, correct knowledge of its subject. Keep the
card's scope: one card in, one question out. Each question must stand alone (name the concept;
no "the passage" or "above").

Each converted question is a JSON object with "type", "question" (Markdown, $LaTeX$ for math),
"explanation" (shown after answering: why the answer is right, briefly), and by type: "choice":
"choices" (strings) and "answer" (0-based index); "truefalse": "answer" true/false; "numeric":
"answer" (a number or expression like "3/8" or "sqrt(2)/2"), optional "tolerance" (absolute)
and "unit"; "free": "answer" (model answer) and "rubric" (what a full-credit answer must
contain); "flashcard": "answer" (the back, Markdown).

Reply with one JSON object and nothing else:
{"cards": [{"id": <card id>, "action": "keep", "reason": "<a few words>"},
           {"id": <card id>, "action": "convert", "reason": "<a few words>", "question": {...}}]}
with exactly one entry per card, in order. It is JSON: escape quotes and double every LaTeX
backslash ("$\\\\frac{a}{b}$")."""


class ConvertError(ValueError):
    pass


# -- requests ------------------------------------------------------------------------------
def card_payload(db: Database, card) -> dict:
    """What Claude sees of a card: where it is, and its two sides."""
    q = json.loads(card["data"])
    note = db.card_note(card["note_id"])
    deck = db.deck(card["deck_id"])
    out = {"id": card["id"], "deck": deck["name"] if deck else "", "tags": note["tags"].split() if note else [],
           "type": q.get("type")}
    if q.get("type") == "cloze":
        out["front"] = _plain_cloze(q.get("question", ""))
        out["back"] = _plain_cloze(q.get("answer", ""))
    elif q.get("type") == "occlusion":
        out["front"] = q.get("question") or "(image occlusion)"
        out["back"] = f"(a masked region of the image {q.get('image', '')})"
    else:
        out["front"] = q.get("question", "")
        out["back"] = q.get("answer", "")
    for key in ("choices", "explanation", "rubric", "unit"):
        if q.get(key) not in (None, "", []):
            out[key] = q[key]
    return out


def _plain_cloze(text: str) -> str:
    """A cloze side with its deletion marked by brackets: [...] or [hint] hidden, [answer] shown."""
    def mark(m: re.Match) -> str:
        inner = m.group(1)
        return inner if inner.startswith("[") or inner.startswith(r"\text{[") else f"[{inner}]"
    text = re.sub(r'<span class="cloze">(.*?)</span>', mark, text, flags=re.S)
    return re.sub(r"\\htmlClass\{cloze\}\{(.*?)\}(?=[^}]|$)", mark, text)


def images(card) -> list[Path]:
    """Image files a card shows, that can be attached for Claude to see."""
    q = json.loads(card["data"])
    names = cardmodel.media_files("basic", {"front": q.get("question", ""), "back": str(q.get("answer", "")),
                                            "extra": q.get("explanation", "")})
    if q.get("type") == "occlusion" and q.get("image"):
        names.append(q["image"])
    out = []
    for name in names:
        path = media_dir() / name
        if "/" not in name and path.suffix.lower() in IMAGE_TYPES and path.is_file() \
                and path.stat().st_size <= MAX_IMAGE_BYTES:
            out.append(path)
    return out


def batches(db: Database, card_ids) -> list[list[int]]:
    """Cards grouped into requests: smaller groups for cards with images."""
    plain, pictured = [], []
    for card_id in card_ids:
        card = db.card(card_id)
        if card is not None:
            (pictured if images(card) else plain).append(card_id)
    return ([plain[i:i + BATCH_SIZE] for i in range(0, len(plain), BATCH_SIZE)]
            + [pictured[i:i + IMAGE_BATCH_SIZE] for i in range(0, len(pictured), IMAGE_BATCH_SIZE)])


def blocks(db: Database, card_ids: list[int], extra_instructions: str = "") -> list[dict]:
    """The request for one batch: instructions, the cards as JSON, and their images."""
    cards = [db.card(i) for i in card_ids]
    content: list[dict] = [{"type": "text", "text": INSTRUCTIONS}]
    if extra_instructions.strip():
        content.append({"type": "text", "text": f"The learner adds:\n<learner_instructions>\n"
                        f"{extra_instructions.strip()}\n</learner_instructions>"})
    attached = 0
    for card in cards:
        for path in images(card):
            if attached >= MAX_IMAGES:
                break
            content.append({"type": "text", "text": f"Image {path.name} (card {card['id']}):"})
            content.append(cc.image_block(path, IMAGE_TYPES[path.suffix.lower()]))
            attached += 1
    payload = json.dumps([card_payload(db, c) for c in cards], ensure_ascii=False, indent=1)
    content.append({"type": "text", "text": f"<cards>\n{payload}\n</cards>"})
    return content


# -- replies -------------------------------------------------------------------------------
def parse(text: str, card_ids: list[int]) -> dict[int, dict]:
    """{card id: {"action": "keep" | "convert" | "error", "question": dict | None, "reason": str}}
    for every card of the batch (cards missing from the reply are errors)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ConvertError("Claude's reply had no JSON in it")
    try:
        data = quiz.loads(m.group(0))
    except ValueError as e:
        raise ConvertError(f"Claude's reply wasn't valid JSON ({e})") from None
    entries = data.get("cards") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ConvertError("Claude's reply had no list of cards")
    out: dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            card_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if card_id not in card_ids:
            continue
        reason = str(entry.get("reason") or "")
        if str(entry.get("action", "")).lower() == "keep" or not entry.get("question"):
            out[card_id] = {"action": "keep", "question": None, "reason": reason}
            continue
        try:
            question = quiz.validate_question(entry["question"])
        except (ValueError, TypeError, KeyError) as e:
            out[card_id] = {"action": "error", "question": None, "reason": f"unusable question: {e}"}
            continue
        out[card_id] = {"action": "convert", "question": question, "reason": reason}
    for card_id in card_ids:
        out.setdefault(card_id, {"action": "error", "question": None, "reason": "Claude left this card out"})
    return out


# -- the conversions table -----------------------------------------------------------------
def start(db: Database, card_ids) -> dict[int, int]:
    """Mark cards as being converted: {card id: conversion id}."""
    return {card_id: db.add_conversion(card_id) for card_id in card_ids}


def record(db: Database, conversions: dict[int, int], text: str) -> None:
    """Store Claude's reply for a batch (conversions: card id -> conversion id)."""
    try:
        results = parse(text, list(conversions))
    except ConvertError as e:
        fail(db, conversions, str(e))
        return
    for card_id, conversion_id in conversions.items():
        r = results[card_id]
        card = db.card(card_id)
        why = _needless(json.loads(card["data"]), r["question"]) if card is not None and r["question"] else None
        if why:
            r = {"action": "keep", "question": None, "reason": f"{r['reason']} ({why})" if r["reason"] else why}
        status = {"keep": "kept", "convert": "proposed", "error": "error"}[r["action"]]
        db.update_conversion(conversion_id, status=status, reason=r["reason"],
                             proposal=json.dumps(r["question"], ensure_ascii=False) if r["question"] else "")


def _needless(current: dict, question: dict) -> str | None:
    """Why a proposed flashcard shouldn't replace the card's question, if it shouldn't: quiz
    questions don't become flashcards, and a flashcard saying the same needs no change."""
    if question.get("type") != "flashcard":
        return None
    if current.get("type") not in cardmodel.SELF_RATED:
        return "quiz questions stay quiz questions"
    if current.get("type") == "flashcard" and all(
        cardmodel.plain(str(current.get(k) or "")) == cardmodel.plain(str(question.get(k) or ""))
        for k in ("question", "answer")
    ):
        return "the flashcard would be the same"
    return None


def fail(db: Database, conversions: dict[int, int], message: str) -> None:
    for conversion_id in conversions.values():
        db.update_conversion(conversion_id, status="error", reason=message)


def accept(db: Database, conversion_id: int) -> bool:
    """Give the card its proposed content (as a note of its own: Basic for a flashcard, else a
    quiz note); it keeps its deck, tags, schedule and history. The old content is kept for revert()."""
    c = db.conversion(conversion_id)
    if c is None or c["status"] != "proposed" or not c["proposal"]:
        return False
    card = db.card(c["card_id"])
    note = db.card_note(card["note_id"]) if card else None
    if card is None or note is None:
        return False
    original = {"note_id": note["id"], "kind": note["kind"], "fields": json.loads(note["fields"]),
                "ord": card["ord"], "data": json.loads(card["data"])}
    db.move_card_to_note(card["id"], *cardmodel.note_for_question(json.loads(c["proposal"])))
    db.update_conversion(conversion_id, status="accepted", original=json.dumps(original, ensure_ascii=False))
    return True


def reject(db: Database, conversion_id: int) -> None:
    db.update_conversion(conversion_id, status="rejected")


def accepted_conversion(db: Database, card_id: int):
    """The card's last accepted conversion, if any (to undo it)."""
    return db.conn.execute(
        "SELECT * FROM conversions WHERE card_id=? AND status='accepted' ORDER BY id DESC LIMIT 1", (card_id,)
    ).fetchone()


def revert(db: Database, conversion_id: int) -> bool:
    """Undo an accepted conversion: the card shows what it did before (back on its old note
    if that note still exists and hasn't regained the card), keeping its schedule."""
    c = db.conversion(conversion_id)
    if c is None or c["status"] != "accepted" or not c["original"]:
        return False
    card = db.card(c["card_id"])
    if card is None:
        return False
    original = json.loads(c["original"])
    old = db.card_note(original["note_id"])
    taken = old is not None and any(x["ord"] == original["ord"] for x in db.note_cards(old["id"]))
    if old is not None and not taken:
        db.move_card(card["id"], old["id"], original["ord"])
    elif original["kind"] == "basic":
        db.move_card_to_note(card["id"], "basic", original["fields"])
    elif original["kind"] == "quiz":  # (a flashcard from before flashcards were Basic notes comes back as one)
        db.move_card_to_note(card["id"], *cardmodel.note_for_question(original["fields"]))
    else:  # a cloze or occlusion card whose note is gone: keep what it showed, as a flashcard
        q = original["data"]
        front = q.get("question", "")
        back = q.get("answer", "") if q.get("type") != "occlusion" else ""
        db.move_card_to_note(card["id"], "basic", {"front": front or "(card)", "back": back,
                                                   "extra": q.get("explanation", "")})
    db.update_conversion(conversion_id, status="rejected")
    return True
