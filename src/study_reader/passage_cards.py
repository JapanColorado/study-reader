"""Review cards Claude makes straight from a highlighted passage or screenshot, without a chat:
the request, reading Claude's reply (a quiz fence whose questions may also be cloze notes), and
adding the cards to the book's deck as new cards. Qt-free; card_maker.CardMaker sends it."""

from __future__ import annotations

import json
import time

from . import cardmodel, quiz, srs
from .db import Database

CLOZE_TYPES = ("cloze", "cloze_deletion")


def request(snippet: dict) -> str:
    """What to ask Claude for cards on a passage (or screenshot) as the reader emits it."""
    what = "the screenshot" if snippet["kind"] == "image" else "the selected passage"
    return (
        f"Make spaced-repetition review cards from {what} (page {snippet['page'] + 1}). They go straight "
        "into my review deck: I won't see your reply, only the cards, so reply with nothing but one quiz "
        "fence, titled after what the passage is about, whose questions are the cards. Follow the quiz "
        "rules for what is worth a card, how many to make and each card standing alone: one card for each "
        "distinct definition, result, step or idea worth remembering. If nothing in it is worth "
        "remembering, give an empty \"questions\" list. Pick each "
        "card's type to suit what it tests. Besides the quiz types you may write a cloze card: "
        "{\"type\": \"cloze\", \"text\": a sentence or two with the key term, value or formula hidden as "
        "{{c1::hidden text}} (or {{c1::hidden text::hint}}), \"explanation\": optional, shown with the "
        "answer, plus \"page\" and \"tags\" as for questions}. Cloze suits a definition, formula or fact "
        "best recalled in its context; deletions numbered c1, c2, ... each make a separate card from the "
        "same text, while ones sharing a number are hidden together. Use the rest of the book for context "
        "and notation, but test what this passage teaches."
    )


def cards(text: str) -> tuple[str, list[dict]]:
    """(title, cards) from Claude's reply. Each card is {kind, fields, page (1-based or None),
    tags, section}; questions that can't be read are left out. Raises ValueError if the reply
    holds no readable quiz fence at all."""
    m = quiz.FENCE.search(text)
    if m is None:
        raise ValueError("Claude's reply had no cards in it")
    data = quiz.loads(m["body"])
    items = data if isinstance(data, list) else data.get("questions")
    if not isinstance(items, list):
        raise ValueError("Claude's reply had no list of cards")
    title = m["title"].strip() or (str(data.get("title") or "") if isinstance(data, dict) else "") or "Cards"
    out = []
    for raw in items:
        if isinstance(raw, dict) and (card := _card(raw)) is not None:
            out.append(card)
    return title, out


def _card(raw: dict) -> dict | None:
    kind = str(raw.get("type", "")).lower().replace("-", "_").replace(" ", "_")
    page = raw.get("page")
    page = int(page) if isinstance(page, (int, float)) and not isinstance(page, bool) and page >= 1 else None
    section = raw["section"].strip() if isinstance(raw.get("section"), str) and raw["section"].strip() else None
    if kind in CLOZE_TYPES:
        extra = raw.get("explanation") or raw.get("extra") or ""
        fields = {"text": str(raw.get("text") or raw.get("question") or ""),
                  "extra": "\n".join(map(str, extra)) if isinstance(extra, list) else str(extra)}
        if not cardmodel.card_ords("cloze", fields):
            return None
        raw_tags = raw.get("tags")
        tags = [cardmodel.clean_tag(t) for t in (raw_tags.split() if isinstance(raw_tags, str) else raw_tags or [])
                if isinstance(t, str)]
        return {"kind": "cloze", "fields": fields, "page": page, "tags": [t for t in dict.fromkeys(tags) if t],
                "section": section}
    try:
        q = quiz.parse_question(raw)
    except (ValueError, TypeError, KeyError):
        return None
    tags = q.pop("tags", [])
    q.pop("section", None)
    q.pop("revises", None)
    kind, fields = cardmodel.note_for_question(q)
    return {"kind": kind, "fields": fields, "page": page, "tags": tags, "section": section}


def add_cards(db: Database, book_id: int, snippet: dict, text: str, now: float | None = None) -> list[int]:
    """Add the cards in Claude's reply on a snippet (page, kind, text, rects) to the book's deck
    as new cards, tagged with their concept tags and the section they draw on (the one named,
    else that of their page, else of the snippet's), and remember the snippet as their passage,
    which stays highlighted. Returns the ids of the cards added. Raises ValueError like cards()."""
    now = time.time() if now is None else now
    page = snippet["page"]
    title, found = cards(text)
    book = db.book(book_id)
    toc = json.loads(book["toc"]) if book and book["toc"] else []
    added = []
    with db.conn:
        deck_id = db.book_deck(book_id)
        for card in found:
            p = card["page"] - 1 if card["page"] else page
            tag = srs.section_tag(book["title"], toc, p, p, card["section"]) if book else None
            tags = [*card["tags"], tag] if tag else card["tags"]
            try:
                note_id = db.add_card_note(card["kind"], card["fields"], deck_id, tags, book_id=book_id, page=p,
                                           now=now, title=title)
            except cardmodel.NoteError:
                continue
            added += [r[0] for r in db.conn.execute("SELECT id FROM cards WHERE note_id=? ORDER BY ord", (note_id,))]
        if added:
            db.add_card_passage(book_id, page, snippet["kind"], snippet.get("text", ""), snippet.get("rects", []), added)
    return added
