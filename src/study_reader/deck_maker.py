"""Make with Claude: Claude writes review cards into a draft that the learner looks over before
anything changes. There are two ways in, and both end in the same draft:
- new: a deck made from a description of what the learner wants, optionally drawing on notes
  they paste and files they attach (PDF, EPUB and text files are read here; images are attached).
  Claude decides how many cards the subject needs and names the deck.
- improve: existing cards (e.g. migrated from Anki) rewritten as the types that fit them best,
  split when they ask several things, or kept. This used to be Convert with Claude.
The learner ticks or unticks each proposed card and can ask for changes in follow-ups. A
follow-up resumes each request's Claude session (or, if the session is gone, starts over from
the request and the draft as it stands) and gets back only the changes: rewrite, split, remove,
add. Then the ticked new cards go into the draft's deck, and the ticked rewrites are applied.
A rewritten card keeps its schedule and history, and undo_rewrite puts it back.

The draft lives in the deck_drafts, draft_batches (one per request to Claude, each with its
session) and draft_items tables. There is at most one draft. Each item has a number that the
learner sees as #n and that Claude uses to refer to it. Qt-free."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import cardmodel, quiz, srs
from .db import Database, deck_name, media_dir

MODEL = "sonnet"
MODELS = [("Sonnet (recommended)", "sonnet"), ("Opus (best, slower)", "opus"), ("Haiku (fastest)", "haiku")]
DEFAULT_DECK = "Made with Claude"
BATCH_SIZE = 8  # existing cards per request
IMAGE_BATCH_SIZE = 3  # existing cards per request when they have images
MAX_SPLIT = 4  # cards one card may be split into
MAX_NEW = 60  # new cards Claude may add in one reply
MAX_CARD_IMAGES = 6  # images of existing cards attached per request
MAX_FILE_IMAGES = 10  # image files the learner may attach
MAX_IMAGE_BYTES = 3_000_000
MAX_SOURCE_CHARS = 300_000  # text read from the learner's files (about 100k tokens)
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
DOCUMENT_TYPES = (".pdf", ".epub", ".xps", ".fb2", ".mobi")
TEXT_TYPES = (".txt", ".md", ".markdown", ".tex", ".rst", ".org", ".csv", ".tsv", ".json", ".html", ".htm", ".xml")
# The types Claude may write (occlusion needs shapes drawn on an image).
MAKE_TYPES = ("flashcard", "reversed", "cloze", "choice", "truefalse", "numeric", "written")
ANSWERED = ("choice", "truefalse", "numeric")  # types the learner answers before seeing the answer
SETTING_INSTRUCTIONS = "deck_maker_instructions"  # what the learner last told Claude when improving cards
SETTING_MODEL = "deck_maker_model"

SYSTEM_PROMPT = (
    "You write spaced-repetition review cards for a study app, and improve the learner's existing "
    "ones. Each message says what to do and the JSON to reply with. Reply with JSON only."
)

CARD_FORMAT = r"""Each card is a JSON object with "type" and, by type:
- "flashcard": "question" (the front), "answer" (the back) and optionally "explanation" (shown
  under the back);
- "reversed": a flashcard that is also asked back to front (term and definition, word and
  translation), with the same fields;
- "cloze": "text" holding deletions {{c1::hidden text}} or {{c1::hidden text::hint}}, and
  optionally "explanation". Each number becomes its own card, so number things to recall
  separately c1, c2, ..., and give things to recall together the same number;
- "choice": "question", "choices" (2-5 strings), "answer" (the 0-based index of the right one) and
  "explanation";
- "truefalse": "question" (a claim), "answer" (true or false) and "explanation";
- "numeric": "question", "answer" (a number or an expression like "3/8" or "sqrt(2)/2"), optionally
  "tolerance" (absolute; default 1% of the answer) and "unit" (shown beside the input),
  "explanation", and "varies": true if its given values could be changed to make a fresh version
  of the problem for later review, else false;
- "written": "question", "answer" (a model answer), "rubric" (the key points a full answer must
  contain), "explanation", and "varies" as for numeric.
"explanation" is shown after answering: briefly, why the answer is right. Any card may carry
"tags": 1-3 short tags naming the concepts it tests, lowercase with underscores for spaces and ::
for nesting (e.g. ["entropy", "thermodynamics::second_law"]); reuse the learner's tags in
<existing_tags> rather than near-duplicates. Text is Markdown, with $LaTeX$ for math.

Choosing a type:
- "numeric" when the answer is a single quantity that can be computed. Pose the problem so it
  needs the idea, not memorized arithmetic.
- "choice" to tell apart ideas that are easily confused: plausible wrong choices, and vary where
  the right one sits.
- "truefalse" for a crisp claim that people commonly get wrong.
- "flashcard" for recall worth memorizing (a definition, a formula, a name, a short explanation
  of why or how): a specific front (not "Explain X") and a short back, the answer or its few key
  points, not an essay. "reversed" only when both directions are worth recalling.
- "cloze" for a statement, formula or list whose key words are worth recalling in context.
- "written" only as a last resort, when no other type could fit (a derivation or argument the
  learner must produce in full, that no number, choice or short flashcard back captures): it is
  the slowest to review.
Each card tests one idea and stands alone: it names the concept or setup it is about instead of
saying "the passage", "above" or "this", and doesn't rely on other cards. Never add facts that
aren't standard, correct knowledge of the subject or in the learner's material.
It is JSON: escape quotes and double every LaTeX backslash ("$\\frac{a}{b}$")."""

NEW_INSTRUCTIONS = f"""Make a deck of review cards on what the learner asks for in <request>.
Cover what someone learning it would most want to remember and be able to use months later: the
central ideas, the intuition behind them, why results hold, how concepts relate, and when and how
to apply methods. Skip trivia (incidental numbers, names, dates) unless it is the point or the
learner asks for it. Make as many cards as the subject needs and no more: roughly 10-20 for a
narrow topic and 20-40 for a broad one, never padded, and at most {MAX_NEW} in one reply (if it
needs more, make the {MAX_NEW} that matter most and say in "note" what is left). The learner's own
wishes about the number, types, difficulty or style of cards come first. When they give material
(<notes>, <file>), draw the cards from it.

Reply with one JSON object and nothing else:
{{"deck": "<a short name for the deck; Parent::Child to nest>",
  "note": "<optional: a sentence or two to the learner, e.g. what you left out>",
  "cards": [{{"action": "add", "card": {{...}}}}, ...]}}"""

IMPROVE_INSTRUCTIONS = """Improve the learner's review cards in <cards>: rewrite each as the type that fits it best,
split it, or keep it. Each card has a number ("id"); refer to it by that.
- Rewrite a card when another type would make the learner think harder about what it really
  tests, or review faster without losing that, or when its front is vague or its back overloaded.
- A card the learner already answers (choice, truefalse, numeric) never becomes a flashcard: keep
  it, or rewrite it as another of those types if that is clearly better.
- A "written" card: rewrite it as another type whenever one fits; keep it only if none could.
- Split a card that asks several separate things (what something is, why it holds, what follows
  from it...) into 2-4 cards, each on one part and each of whatever type fits that part; together
  they must cover everything the card asks. Don't split parts that are one idea, or a card whose
  answer is already short.
- Keep a card that already works well (good cloze, recall, choice, truefalse and numeric cards
  usually do), when a rewrite would only reword it, or when it depends on an image or context
  you can't see. Keep image occlusion cards unless the learner asks otherwise.
A rewrite keeps the card's scope: it tests everything the card does, no more and no less (split
the card instead if one card can't). A cloze card's "deletion" says which of its text's
deletions this card asks for.
{adding}

Reply with one JSON object and nothing else:
{{"note": "<optional: a sentence or two to the learner>",
  "cards": [{{"id": <n>, "action": "keep", "reason": "<a few words>"}},
            {{"id": <n>, "action": "rewrite", "reason": "<a few words>", "card": {{...}}}},
            {{"id": <n>, "action": "split", "reason": "<a few words>", "cards": [{{...}}, {{...}}]}}]}}
with exactly one entry per card, in order."""

ADD_ALLOWED = ('Add new cards only when the learner asks for them, each as {"action": "add", "card": {...}}; '
               "they go into the deck the learner chose.")
ADD_ELSEWHERE = "Don't add new cards: another request handles that. Only change the cards here."

FOLLOW_UP_INSTRUCTIONS = """The learner has looked over the draft and asks for changes in <learner_feedback>.
<draft> is the draft as it stands now. Each card has its number ("id") and "ticked" (false means
the learner unticked it: they don't want it as it is). A card that is one of the learner's
existing cards also has "now" (what it is) and "proposed" (your proposal for it, absent if you
kept it). The other cards are new cards you proposed.

Reply with the changes only, as one JSON object and nothing else:
{{"note": "<optional: a sentence or two to the learner>", "deck": "<only to rename the deck>",
  "cards": [...]}}
where each entry is one of
  {{"id": <n>, "action": "rewrite", "reason": "<a few words>", "card": {{...}}}}: replace card n (for an
    existing card, this is your new proposal for it)
  {{"id": <n>, "action": "split", "reason": "<a few words>", "cards": [{{...}}, ...]}}: the same, as 2-4 cards
  {{"id": <n>, "action": "remove"}}: drop a new card from the draft
  {{"id": <n>, "action": "keep", "reason": "<a few words>"}}: withdraw your proposal for an existing
    card, leaving it as it is
  {{"action": "add", "card": {{...}}}}: a new card. {adding}
Leave out every card you don't change."""


class DeckMakerError(ValueError):
    pass


# -- cards as Claude writes them -------------------------------------------------------------
def card_spec(raw) -> dict:
    """{"kind", "fields", "tags"} of a card as Claude writes it (CARD_FORMAT). Raises ValueError
    (or TypeError, KeyError) if it can't make a card."""
    if not isinstance(raw, dict):
        raise ValueError("a card isn't a JSON object")
    name = str(raw.get("type", "")).lower().replace("-", "_").replace(" ", "_")
    raw_tags = raw.get("tags")
    tags = [cardmodel.clean_tag(t) for t in (raw_tags.split() if isinstance(raw_tags, str) else raw_tags or [])
            if isinstance(t, str)]
    tags = list(dict.fromkeys(t for t in tags if t))
    if name in ("cloze", "cloze_deletion"):
        text = str(raw.get("text") or raw.get("question") or "")
        kind, fields = "cloze", {"text": text, "extra": str(raw.get("explanation") or raw.get("extra") or "")}
    elif name in ("reversed", "reverse", "both_ways", "flashcard_both_ways"):
        q = quiz.validate_question({**raw, "type": "flashcard"})
        kind, fields = "reversed", cardmodel.card_fields(q)[1]
    else:
        if name in ("occlusion", "image_occlusion"):
            raise ValueError("image occlusion cards can't be written here")
        q = quiz.validate_question(raw)
        kind, fields = cardmodel.card_fields(q)
    if kind == "cloze" and not cardmodel.card_ords(kind, fields):
        raise ValueError("a cloze needs a deletion {{c1::...}}")
    try:
        cardmodel.check(kind, fields)
    except cardmodel.NoteError as e:
        raise ValueError(str(e)) from None
    return {"kind": kind, "fields": fields, "tags": tags}


def for_claude(spec: dict) -> dict:
    """A card spec back in the JSON Claude writes (CARD_FORMAT)."""
    kind, f = spec["kind"], spec["fields"]
    if kind in ("flashcard", "reversed"):
        out = {"type": kind, "question": f.get("front", ""), "answer": f.get("back", "")}
        if f.get("extra"):
            out["explanation"] = f["extra"]
    elif kind == "cloze":
        out = {"type": "cloze", "text": f.get("text", "")}
        if f.get("extra"):
            out["explanation"] = f["extra"]
    else:
        out = {k: v for k, v in f.items() if k not in ("answer_latex", "page", "section", "revises")}
        out["type"] = kind
    if spec.get("tags"):
        out["tags"] = spec["tags"]
    return out


def previews(spec: dict) -> list[dict]:
    """The questions a card spec makes, as quiz.js draws them (a cloze's each deletion, a
    reversed flashcard's both sides)."""
    return [cardmodel.card_data(spec["kind"], spec["fields"], o) for o in cardmodel.card_ords(spec["kind"], spec["fields"])]


def specs(proposal) -> list[dict]:
    """A draft item's proposed cards (proposal: its JSON column, or the list)."""
    if isinstance(proposal, str):
        return json.loads(proposal) if proposal else []
    return list(proposal or [])


def existing_payload(db: Database, card, number: int) -> dict:
    """What Claude sees of one of the learner's cards: where it is, its type and its content."""
    note = db.card_note(card["note_id"])
    deck = db.deck(card["deck_id"])
    kind = note["kind"] if note else json.loads(card["data"]).get("type")
    fields = json.loads(note["fields"]) if note else {}
    out = {"id": number, "deck": deck["name"] if deck else "", "tags": note["tags"].split() if note else []}
    if kind in ("flashcard", "reversed"):
        out.update(for_claude({"kind": kind, "fields": fields}))
        if kind == "reversed":
            out["side"] = "back to front" if card["ord"] == 1 else "front to back"
    elif kind == "cloze":
        out.update(for_claude({"kind": kind, "fields": fields}), deletion=card["ord"] + 1)
    elif kind == "occlusion":
        out.update(type="occlusion", header=fields.get("header", ""),
                   note=f"masks region {card['ord'] + 1} of the image {fields.get('image', '')}")
        if fields.get("extra"):
            out["explanation"] = fields["extra"]
    else:
        out.update(for_claude({"kind": kind, "fields": fields}))
    if not out["tags"]:
        del out["tags"]
    return out


def card_images(card) -> list[Path]:
    """Image files a card shows, that can be attached for Claude to see."""
    q = json.loads(card["data"])
    names = cardmodel.media_files("flashcard", {"front": q.get("question", ""), "back": str(q.get("answer", "")),
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
    """Existing cards grouped into requests: smaller groups for cards with images."""
    plain, pictured = [], []
    for card_id in dict.fromkeys(card_ids):
        card = db.card(card_id)
        if card is not None:
            (pictured if card_images(card) else plain).append(card_id)
    return ([plain[i:i + BATCH_SIZE] for i in range(0, len(plain), BATCH_SIZE)]
            + [pictured[i:i + IMAGE_BATCH_SIZE] for i in range(0, len(pictured), IMAGE_BATCH_SIZE)])


def usual_deck(db: Database, card_ids) -> str:
    """The deck most of these cards are in (where cards added while improving them go)."""
    decks = Counter(c["deck_id"] for c in map(db.card, card_ids) if c is not None)
    deck = db.deck(decks.most_common(1)[0][0]) if decks else None
    return deck["name"] if deck else DEFAULT_DECK


# -- requests --------------------------------------------------------------------------------
def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tags(db: Database) -> list[dict]:
    tags = srs.concept_tags(db)[:srs.MAX_TAGS_IN_CONTEXT]
    return [_text(f"Tags already in the learner's cards:\n<existing_tags>{' '.join(tags)}</existing_tags>")] if tags else []


def new_blocks(db: Database, request: dict) -> list[dict]:
    """The first request for a new deck. Attached files stay a source_files block until
    expand_files reads them (off the GUI thread)."""
    blocks = [_text(CARD_FORMAT), _text(NEW_INSTRUCTIONS), *_tags(db)]
    if request.get("notes", "").strip():
        blocks.append(_text(f"The learner's material:\n<notes>\n{request['notes'].strip()}\n</notes>"))
    if request.get("files"):
        blocks.append({"type": "source_files", "paths": list(request["files"])})
    blocks.append(_text(f"<request>\n{request.get('description', '').strip()}\n</request>"))
    return blocks


def improve_blocks(db: Database, items, instructions: str, adding: bool) -> list[dict]:
    """The first request for a batch of existing cards (items: their draft rows)."""
    blocks = [_text(CARD_FORMAT), _text(IMPROVE_INSTRUCTIONS.format(adding=ADD_ALLOWED if adding else ADD_ELSEWHERE)),
              *_tags(db)]
    if instructions.strip():
        blocks.append(_text(f"The learner adds:\n<learner_instructions>\n{instructions.strip()}\n</learner_instructions>"))
    payload, attached = [], 0
    for item in items:
        card = db.card(item["card_id"])
        if card is None:
            continue
        payload.append(existing_payload(db, card, item["number"]))
        for path in card_images(card):
            if attached >= MAX_CARD_IMAGES:
                break
            blocks += [_text(f"Image {path.name} (card {item['number']}):"), image_block(path)]
            attached += 1
    blocks.append(_text(f"<cards>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</cards>"))
    return blocks


def draft_state(db: Database, items) -> list[dict]:
    """The draft's cards as follow-ups show them to Claude."""
    out = []
    for item in items:
        entry = {"id": item["number"], "ticked": bool(item["checked"])}
        cards = [for_claude(s) for s in specs(item["proposal"])]
        if item["card_id"] is not None:
            card = db.card(item["card_id"])
            if card is None:
                continue
            now = existing_payload(db, card, item["number"])
            now.pop("id", None)
            entry["now"] = now
            if cards:
                entry["proposed"] = cards if len(cards) > 1 else cards[0]
            elif item["status"] == "error":
                entry["error"] = item["reason"]
        elif cards:
            entry["card"] = cards[0]
        else:
            continue
        out.append(entry)
    return out


def follow_up_blocks(db: Database, draft, batch_id: int, feedback: str, adding: bool) -> list[dict]:
    items = db.draft_items(draft["id"], batch_id)
    state = json.dumps(draft_state(db, items), ensure_ascii=False, indent=1)
    deck = f"The deck is called \"{draft['deck']}\".\n" if draft["deck"] else ""
    return [
        _text(FOLLOW_UP_INSTRUCTIONS.format(adding=ADD_ALLOWED if adding else ADD_ELSEWHERE)),
        _text(f"{deck}<draft>\n{state}\n</draft>"),
        _text(f"<learner_feedback>\n{feedback.strip()}\n</learner_feedback>"),
    ]


MENTION = re.compile(r"#\s*(\d+)(?:\s*(?:-|–|—|to)\s*#?\s*(\d+))?")
MAX_RANGE = 1000


def mentioned(feedback: str) -> set[int]:
    """The card numbers a follow-up names: "#4", "#12-15", "#3 to #6"."""
    out: set[int] = set()
    for m in MENTION.finditer(feedback):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        lo, hi = min(a, b), max(a, b)
        out.update(range(lo, min(hi, lo + MAX_RANGE) + 1))
    return out


def follow_up_targets(db: Database, draft, feedback: str) -> tuple[list[int], bool]:
    """The batches a follow-up goes to, and whether it names cards. A follow-up that names cards
    goes only to the batches holding them (each batch is its own request to Claude, so sending
    "make #4 harder" to every one would cost a request per batch); one that names none, or only
    numbers no batch holds, goes to every batch that has a session."""
    batches = [b["id"] for b in db.draft_batches(draft["id"]) if b["session_id"]]
    numbers = mentioned(feedback)
    if numbers:
        holding = {i["batch_id"] for i in db.draft_items(draft["id"]) if i["number"] in numbers}
        routed = [b for b in batches if b in holding]
        if routed:
            return routed, True
    return batches, False


def first_blocks(db: Database, draft, batch_id: int) -> list[dict]:
    """The batch's first request, as it was sent (to start over when its session is gone)."""
    request = json.loads(draft["request"] or "{}")
    if draft["mode"] == "new":
        return new_blocks(db, request)
    items = [i for i in db.draft_items(draft["id"], batch_id) if i["card_id"] is not None]
    return improve_blocks(db, items, request.get("instructions", ""), adding=is_first_batch(db, draft, batch_id))


def is_first_batch(db: Database, draft, batch_id: int) -> bool:
    """Only the draft's first request may add new cards (so batches don't add the same ones)."""
    first = db.draft_batches(draft["id"])
    return not first or first[0]["id"] == batch_id


def image_block(path: Path) -> dict:
    from . import claude_client as cc
    return cc.image_block(path, IMAGE_TYPES[path.suffix.lower()])


def expand_files(blocks: list[dict]) -> list[dict]:
    """The request with its source_files blocks replaced by the files: text read from documents
    and text files, images attached. Raises DeckMakerError for a file that can't be used."""
    out: list[dict] = []
    for block in blocks:
        if block.get("type") != "source_files":
            out.append(block)
            continue
        chars, images = 0, 0
        for name in block["paths"]:
            path = Path(name)
            suffix = path.suffix.lower()
            if not path.is_file():
                raise DeckMakerError(f"The file {path.name} is gone.")
            if suffix in IMAGE_TYPES:
                images += 1
                if images > MAX_FILE_IMAGES:
                    raise DeckMakerError(f"Attach at most {MAX_FILE_IMAGES} images.")
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    raise DeckMakerError(f"The image {path.name} is over {MAX_IMAGE_BYTES // 1_000_000} MB.")
                out += [_text(f"The learner's image {path.name}:"), image_block(path)]
                continue
            text = read_text(path)
            chars += len(text)
            if chars > MAX_SOURCE_CHARS:
                raise DeckMakerError(
                    f"The attached files are too long for one request (over {MAX_SOURCE_CHARS // 1000}k characters of "
                    "text). Attach less, or a chapter at a time.")
            out.append(_text(f'<file name="{path.name}">\n{text}\n</file>'))
    return out


def read_text(path: Path) -> str:
    """A file's text: a document's (PDF, EPUB...) page by page, or a text file's."""
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_TYPES:
        import pymupdf
        try:
            with pymupdf.open(str(path)) as doc:
                pages = [f"[[page {i + 1}]]\n{page.get_text().strip()}" for i, page in enumerate(doc)]
        except Exception as e:
            raise DeckMakerError(f"Can't read {path.name}: {e}") from None
        text = "\n\n".join(pages)
        if not re.sub(r"\[\[page \d+\]\]", "", text).strip():
            raise DeckMakerError(f"{path.name} has no text to read (a scan?). Attach its pages as images instead.")
        return text
    if suffix in TEXT_TYPES or not suffix:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise DeckMakerError(f"Can't read {path.name}: {e}") from None
    raise DeckMakerError(f"Can't use {path.name}: attach PDFs, EPUBs, text files or images.")


# -- replies ---------------------------------------------------------------------------------
def parse(text: str) -> dict:
    """{"deck", "note", "entries"} of a reply; each entry {"id" (a number or None), "action"
    ("add" | "rewrite" | "keep" | "remove" | "error"), "specs", "reason"}. Raises DeckMakerError
    if the reply has no usable JSON."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise DeckMakerError("Claude's reply had no JSON in it")
    try:
        data = quiz.loads(m.group(0))
    except ValueError as e:
        raise DeckMakerError(f"Claude's reply wasn't valid JSON ({e})") from None
    raw_entries = data.get("cards") if isinstance(data, dict) else data
    if not isinstance(raw_entries, list):
        raise DeckMakerError("Claude's reply had no list of cards")
    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw["id"]) if raw.get("id") is not None else None
        except (TypeError, ValueError):
            number = None
        action = str(raw.get("action") or "").lower()
        given = next((raw[k] for k in ("cards", "card", "questions", "question") if isinstance(raw.get(k), (dict, list))
                      and raw[k]), None)
        if not action:
            action = "add" if number is None else "rewrite" if given else "keep"
        action = {"convert": "rewrite", "replace": "rewrite", "change": "rewrite", "split": "rewrite",
                  "delete": "remove", "drop": "remove", "new": "add"}.get(action, action)
        if action not in ("add", "rewrite", "keep", "remove"):
            continue
        if action == "add" and number is not None and not given:
            continue
        entry = {"id": number, "action": action, "specs": None, "reason": str(raw.get("reason") or "")}
        if action in ("add", "rewrite"):
            if not given and isinstance(raw.get("type"), str):  # the card itself, flattened into the entry
                given = {k: v for k, v in raw.items() if k not in ("id", "action", "reason")}
            given = given if isinstance(given, list) else [given] if given else []
            try:
                if not given:
                    raise ValueError("no card was given")
                if len(given) > MAX_SPLIT and action == "rewrite":
                    raise ValueError(f"split into {len(given)} cards (at most {MAX_SPLIT})")
                entry["specs"] = [card_spec(g) for g in given]
            except (ValueError, TypeError, KeyError) as e:
                entry.update(action="error", reason=f"unusable card: {e}", adding=action == "add")
        entries.append(entry)
    deck = data.get("deck") if isinstance(data, dict) else None
    note = data.get("note") if isinstance(data, dict) else None
    return {"deck": deck.strip() if isinstance(deck, str) else "", "note": note.strip() if isinstance(note, str) else "",
            "entries": entries}


def needless(current_kind: str, current_fields: dict, spec: dict) -> str | None:
    """Why a proposed flashcard shouldn't replace a card, if it shouldn't: cards the learner
    answers don't become flashcards, and a flashcard saying the same needs no change."""
    if spec["kind"] not in ("flashcard", "reversed"):
        return None
    if current_kind in ANSWERED:
        return "cards you answer stay that way"
    if current_kind == spec["kind"] and all(
        cardmodel.plain(str(current_fields.get(k) or "")) == cardmodel.plain(str(spec["fields"].get(k) or ""))
        for k in ("front", "back")
    ):
        return "the flashcard would be the same"
    return None


def _dumps(specs_: list[dict]) -> str:
    return json.dumps(specs_, ensure_ascii=False)


def record(db: Database, batch_id: int, session_id: str, text: str) -> None:
    """Apply Claude's reply for a batch to the draft. The batch's first reply settles every
    existing card of the batch (a card the reply leaves out is an error); a follow-up's changes
    only the cards it names."""
    batch = db.draft_batch(batch_id)
    draft = db.draft()
    if batch is None or draft is None or batch["draft_id"] != draft["id"]:
        return
    first = not batch["session_id"]
    try:
        reply = parse(text)
    except DeckMakerError as e:
        fail(db, batch_id, str(e))
        return
    request = json.loads(draft["request"] or "{}")
    if reply["deck"] and draft["mode"] == "new":
        parent = request.get("parent", "")
        db.update_draft(draft["id"], deck=deck_name(f"{parent}::{reply['deck']}" if parent else reply["deck"]))
    items = {i["number"]: i for i in db.draft_items(draft["id"])}
    adding = is_first_batch(db, draft, batch_id)
    added = 0
    for entry in reply["entries"]:
        item = items.get(entry["id"]) if entry["id"] is not None else None
        if item is not None and item["batch_id"] != batch_id:
            item = None  # another batch's card
        if item is None:
            if entry["action"] == "add" or (entry["action"] == "error" and entry.get("adding")):
                if not adding or added >= MAX_NEW:
                    continue
                for spec in entry["specs"] or [None]:
                    if spec is None:
                        db.add_draft_item(draft["id"], batch_id, status="error", reason=entry["reason"])
                    else:
                        db.add_draft_item(draft["id"], batch_id, status="proposed", proposal=_dumps([spec]),
                                          reason=entry["reason"])
                    added += 1
            continue
        _apply_entry(db, draft, batch_id, item, entry)
    if first:
        for item in db.draft_items(draft["id"], batch_id):
            if item["status"] == "pending":
                db.update_draft_item(item["id"], status="error", reason="Claude left this card out")
    db.update_draft_batch(batch_id, status="done", error="", session_id=session_id or batch["session_id"],
                          note=reply["note"])


def _apply_entry(db: Database, draft, batch_id: int, item, entry: dict) -> None:
    action, reason = entry["action"], entry["reason"]
    if item["card_id"] is None:  # a new card the draft proposes
        if action == "remove":
            db.update_draft_item(item["id"], status="removed")
        elif action == "rewrite":
            first, *rest = entry["specs"]
            db.update_draft_item(item["id"], status="proposed", proposal=_dumps([first]), reason=reason, checked=1)
            for spec in rest:
                db.add_draft_item(draft["id"], batch_id, status="proposed", proposal=_dumps([spec]), reason=reason)
        elif action == "error" and item["status"] != "proposed":
            db.update_draft_item(item["id"], status="error", reason=reason)
        return
    card = db.card(item["card_id"])
    note = db.card_note(card["note_id"]) if card else None
    if note is None:
        return
    if action in ("keep", "remove"):
        db.update_draft_item(item["id"], status="kept", proposal="", reason=reason, checked=1)
    elif action == "error":
        db.update_draft_item(item["id"], status="error", proposal="", reason=reason)
    else:
        fields = json.loads(note["fields"])
        why = next((w for s in entry["specs"] if (w := needless(note["kind"], fields, s))), None)
        if why:
            db.update_draft_item(item["id"], status="kept", proposal="", checked=1,
                                 reason=f"{reason} ({why})" if reason else why)
        else:
            db.update_draft_item(item["id"], status="proposed", proposal=_dumps(entry["specs"]), reason=reason,
                                 checked=1)


def fail(db: Database, batch_id: int, message: str) -> None:
    """A request failed: its batch says why, and the cards still waiting for a first answer are errors."""
    batch = db.draft_batch(batch_id)
    if batch is None:
        return
    db.update_draft_batch(batch_id, status="error", error=message)
    for item in db.draft_items(batch["draft_id"], batch_id):
        if item["status"] == "pending":
            db.update_draft_item(item["id"], status="error", reason=message)


# -- starting ------------------------------------------------------------------------------
def start_new(db: Database, description: str, notes: str = "", files=(), parent: str = "",
              model: str = MODEL) -> tuple[int, int]:
    """A draft for a new deck, replacing any other: (draft id, batch id of its request)."""
    request = {"description": description.strip(), "notes": notes.strip(), "files": [str(f) for f in files],
               "parent": deck_name(parent) if parent.strip() else ""}
    draft_id = db.add_draft("new", request, "", model)
    return draft_id, db.add_draft_batch(draft_id)


def start_improve(db: Database, card_ids, instructions: str = "", model: str = MODEL) -> tuple[int, list[int]]:
    """A draft improving existing cards, replacing any other: (draft id, its batches' ids)."""
    groups = batches(db, card_ids)
    ids = [i for g in groups for i in g]
    draft_id = db.add_draft("improve", {"instructions": instructions.strip(), "count": len(ids)},
                            usual_deck(db, ids), model)
    out = []
    for group in groups:
        batch_id = db.add_draft_batch(draft_id)
        for card_id in group:
            db.add_draft_item(draft_id, batch_id, card_id=card_id)
        out.append(batch_id)
    return draft_id, out


def counts(db: Database, draft_id: int) -> dict:
    """How the draft's cards stand: {status: n}, plus "ticked" (proposed and ticked, new and
    rewrites) and "new" / "rewrites" of those."""
    out = Counter()
    for item in db.draft_items(draft_id):
        out[item["status"]] += 1
        if item["status"] == "proposed" and item["checked"]:
            out["ticked"] += 1
            out["rewrites" if item["card_id"] is not None else "new"] += 1
            if item["card_id"] is None:
                out["new_cards"] += sum(len(previews(s)) for s in specs(item["proposal"]))
    return dict(out)


# -- applying ------------------------------------------------------------------------------
def apply(db: Database, draft_id: int) -> dict:
    """Add the draft's ticked new cards to its deck and apply its ticked rewrites; the draft is
    then gone. {"added": card ids, "rewritten": card ids, "deck": deck id or None}."""
    draft = db.draft()
    if draft is None or draft["id"] != draft_id:
        return {"added": [], "rewritten": [], "deck": None}
    items = [i for i in db.draft_items(draft_id) if i["status"] == "proposed" and i["checked"]]
    deck_id = None
    added, rewritten = [], []
    with db.conn:
        for item in items:
            if item["card_id"] is not None:
                if db.card(item["card_id"]) is not None:
                    added += rewrite(db, item["card_id"], specs(item["proposal"]))
                    rewritten.append(item["card_id"])
                continue
            if deck_id is None:
                deck_id = db._ensure_deck(draft["deck"] or DEFAULT_DECK)
            for spec in specs(item["proposal"]):
                note_id = db.add_card_note(spec["kind"], spec["fields"], deck_id, tags=spec.get("tags", []))
                added += [c["id"] for c in db.note_cards(note_id)]
        db.conn.execute("DELETE FROM deck_drafts WHERE id=?", (draft_id,))
    return {"added": added, "rewritten": rewritten, "deck": deck_id}


def rewrite(db: Database, card_id: int, new: list[dict]) -> list[int]:
    """Give a card its proposed content (the first card spec; a split's others become new cards
    in its deck, book and page, with its tags), keeping its deck, schedule and history. What it
    was is kept for undo_rewrite. Returns the ids of the cards it added."""
    card = db.card(card_id)
    note = db.card_note(card["note_id"])
    first, *rest = new
    original = {"note_id": note["id"], "kind": note["kind"], "fields": json.loads(note["fields"]),
                "ord": card["ord"], "data": json.loads(card["data"])}
    tags = list(dict.fromkeys([*note["tags"].split(), *first.get("tags", [])]))
    ords = cardmodel.card_ords(first["kind"], first["fields"])
    with db.conn:
        note_id = db.move_card_to_note(card_id, first["kind"], first["fields"])  # as its first card, ords[0]
        db.update_card_note(note_id, tags=tags)
        added = [db.ensure_card(note_id, o, card["deck_id"]) for o in ords[1:]]
        for spec in rest:
            other = db.add_card_note(spec["kind"], spec["fields"], card["deck_id"],
                                     tags=list(dict.fromkeys([*note["tags"].split(), *spec.get("tags", [])])),
                                     book_id=card["book_id"], page=card["page"], title=card["title"])
            added += [c["id"] for c in db.note_cards(other)]
        if added:
            db.conn.execute(f"UPDATE cards SET book_id=?, page=? WHERE id IN ({','.join('?' * len(added))})",
                            (card["book_id"], card["page"], *added))
            original["added"] = added
        db.add_card_rewrite(card_id, original)
    return added


def undo_rewrite(db: Database, card_id: int) -> bool:
    """Put a rewritten card back as it was (on its old card_notes row if that still exists and
    hasn't a card in its place), keeping its schedule. Cards the rewrite added go unless they have
    been reviewed."""
    r = db.card_rewrite(card_id)
    card = db.card(card_id)
    if r is None or card is None:
        return False
    original = json.loads(r["original"])
    old = db.card_note(original["note_id"])
    taken = old is not None and any(x["ord"] == original["ord"] for x in db.note_cards(old["id"]))
    with db.conn:
        if old is not None and not taken:
            db.move_card(card_id, old["id"], original["ord"])
        elif original["kind"] in (*cardmodel.QUESTION_TYPES, "flashcard"):
            db.move_card_to_note(card_id, original["kind"], original["fields"])
        else:  # a reversed, cloze or occlusion card whose row is gone: keep what it showed, as a flashcard
            q = original["data"]
            back = q.get("answer", "") if q.get("type") != "occlusion" else ""
            db.move_card_to_note(card_id, "flashcard", {"front": q.get("question", "") or "(card)", "back": back,
                                                        "extra": q.get("explanation", "")})
        added = [i for i in original.get("added", []) if db.card(i) is not None and not db.reviews(i)]
        if added:
            db.delete_cards(added)
        db.delete_card_rewrite(r["id"])
    return True
