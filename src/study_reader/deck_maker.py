"""Make with Claude: Claude writes questions into a draft that the learner looks over before
anything changes. There are two ways in, and both end in the same draft:
- new: a deck made from a description of what the learner wants, optionally drawing on notes
  they paste and files they attach (PDF, EPUB and text files are read here; images are attached).
  Claude decides how many questions the subject needs and names the deck.
- improve: existing questions (e.g. migrated from Anki) rewritten as the types that fit them best,
  split when they ask several things, or kept. This used to be Convert with Claude.
The learner ticks or unticks each proposed question and can ask for changes in follow-ups. A
follow-up resumes each request's Claude session (or, if the session is gone, starts over from
the request and the draft as it stands) and gets back only the changes: rewrite, split, remove,
add. Then the ticked new questions go into the draft's deck, and the ticked rewrites are applied.
A rewritten question keeps its schedule and past attempts, and undo_rewrite puts it back.

The draft lives in the deck_drafts, draft_batches (one per request to Claude, each with its
session) and draft_items tables. There is at most one draft. Each item has a number that the
learner sees as #n and that Claude uses to refer to it. Claude writes the question types by their
current names; replies (and drafts or rewrites stored before the rename) may still use the old
ones, which are read too. Qt-free."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import question_model, quiz, practice
from .db import Database, deck_name, media_dir

MODEL = "sonnet"
MODELS = [("Sonnet (recommended)", "sonnet"), ("Opus (best, slower)", "opus"), ("Haiku (fastest)", "haiku")]
DEFAULT_DECK = "Made with Claude"
BATCH_SIZE = 8  # existing questions per request
IMAGE_BATCH_SIZE = 3  # existing questions per request when they have images
MAX_SPLIT = 4  # questions one question may be split into
MAX_NEW = 60  # new questions Claude may add in one reply
MAX_QUESTION_IMAGES = 6  # images of existing questions attached per request
MAX_FILE_IMAGES = 10  # image files the learner may attach
MAX_IMAGE_BYTES = 3_000_000
MAX_SOURCE_CHARS = 300_000  # text read from the learner's files (about 100k tokens)
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
DOCUMENT_TYPES = (".pdf", ".epub", ".xps", ".fb2", ".mobi")
TEXT_TYPES = (".txt", ".md", ".markdown", ".tex", ".rst", ".org", ".csv", ".tsv", ".json", ".html", ".htm", ".xml")
ANSWERED = ("choice", "truefalse", "calculation")  # types the learner answers before seeing the answer
SELF_CHECKS = ("self_check", "both_ways")
SETTING_INSTRUCTIONS = "deck_maker_instructions"  # what the learner last told Claude when improving questions
SETTING_MODEL = "deck_maker_model"

SYSTEM_PROMPT = (
    "You write practice questions for a study app that brings each question back right before the "
    "learner would forget it, and you improve the learner's existing ones. Each message says what to "
    "do and the JSON to reply with. Reply with JSON only."
)

QUESTION_FORMAT = r"""Each question is a JSON object with "type" and, by type:
- "self_check": a question the learner answers in their head, then checks and rates themselves:
  "question", "answer" (shown when they check) and optionally "explanation" (shown under the
  answer);
- "both_ways": a self_check that is also asked the other way round (term and definition, word and
  translation), with the same fields;
- "fill_in": "text" holding blanks {{c1::hidden text}} or {{c1::hidden text::hint}}, and
  optionally "explanation". Each number becomes its own question, so number things to recall
  separately c1, c2, ..., and give things to recall together the same number;
- "choice": "question", "choices" (2-5 strings), "answer" (the 0-based index of the right one) and
  "explanation";
- "truefalse": "question" (a claim), "answer" (true or false) and "explanation";
- "calculation": "question", "answer" (a number or an expression like "3/8" or "sqrt(2)/2"),
  optionally "tolerance" (absolute; default 1% of the answer) and "unit" (shown beside the input),
  "explanation", and "varies": true if its given values could be changed to make a fresh version
  of the problem for later practice, else false;
- "short_answer": "question", "answer" (a model answer), "rubric" (the key points a full answer
  must contain), "explanation", and "varies" as for calculation.
"explanation" is shown after answering: briefly, why the answer is right. Any question may carry
"tags": 1-3 short tags naming the concepts it tests, lowercase with underscores for spaces and ::
for nesting (e.g. ["entropy", "thermodynamics::second_law"]); reuse the learner's tags in
<existing_tags> rather than near-duplicates. Text is Markdown, with $LaTeX$ for math.

Choosing a type:
- "calculation" when the answer is a single quantity that can be computed. Pose the problem so it
  needs the idea, not memorized arithmetic.
- "choice" to tell apart ideas that are easily confused: plausible wrong choices, and vary where
  the right one sits.
- "truefalse" for a crisp claim that people commonly get wrong.
- "self_check" for recall worth memorizing (a definition, a formula, a name, a short explanation
  of why or how): a specific question (not "Explain X") and a short answer, the answer or its few
  key points, not an essay. "both_ways" only when both directions are worth recalling.
- "fill_in" for a statement, formula or list whose key words are worth recalling in context.
- "short_answer" only as a last resort, when no other type could fit (a derivation or argument the
  learner must produce in full, that no number, choice or short self_check answer captures): it is
  the slowest to practice.
Each question tests one idea and stands alone: it names the concept or setup it is about instead
of saying "the passage", "above" or "this", and doesn't rely on other questions. Never add facts
that aren't standard, correct knowledge of the subject or in the learner's material.
It is JSON: escape quotes and double every LaTeX backslash ("$\frac{a}{b}$")."""

NEW_INSTRUCTIONS = f"""Make a deck of practice questions on what the learner asks for in <request>.
Cover what someone learning it would most want to remember and be able to use months later: the
central ideas, the intuition behind them, why results hold, how concepts relate, and when and how
to apply methods. Skip trivia (incidental numbers, names, dates) unless it is the point or the
learner asks for it. Make as many questions as the subject needs and no more: roughly 10-20 for a
narrow topic and 20-40 for a broad one, never padded, and at most {MAX_NEW} in one reply (if it
needs more, make the {MAX_NEW} that matter most and say in "note" what is left). The learner's own
wishes about the number, types, difficulty or style of questions come first. When they give
material (<notes>, <file>), draw the questions from it.

Reply with one JSON object and nothing else:
{{"deck": "<a short name for the deck; Parent::Child to nest>",
  "note": "<optional: a sentence or two to the learner, e.g. what you left out>",
  "questions": [{{"action": "add", "question": {{...}}}}, ...]}}"""

IMPROVE_INSTRUCTIONS = """Improve the learner's questions in <questions>: rewrite each as the type that fits it
best, split it, or keep it. Each question has a number ("id"); refer to it by that.
- Rewrite a question when another type would make the learner think harder about what it really
  tests, or practice faster without losing that, or when its question is vague or its answer
  overloaded.
- A question the learner already answers (choice, truefalse, calculation) never becomes a
  self_check: keep it, or rewrite it as another of those types if that is clearly better.
- A "short_answer" question: rewrite it as another type whenever one fits; keep it only if none
  could.
- Split a question that asks several separate things (what something is, why it holds, what
  follows from it...) into 2-4 questions, each on one part and each of whatever type fits that
  part; together they must cover everything the question asks. Don't split parts that are one
  idea, or a question whose answer is already short.
- Keep a question that already works well (good fill_in, self_check, choice, truefalse and
  calculation questions usually do), when a rewrite would only reword it, or when it depends on an
  image or context you can't see. Keep image_labels questions unless the learner asks otherwise.
A rewrite keeps the question's scope: it tests everything the question does, no more and no less
(split the question instead if one question can't). A fill_in question's "blank" says which of
its text's blanks this question asks for.
{adding}

Reply with one JSON object and nothing else:
{{"note": "<optional: a sentence or two to the learner>",
  "questions": [{{"id": <n>, "action": "keep", "reason": "<a few words>"}},
                {{"id": <n>, "action": "rewrite", "reason": "<a few words>", "question": {{...}}}},
                {{"id": <n>, "action": "split", "reason": "<a few words>", "questions": [{{...}}, {{...}}]}}]}}
with exactly one entry per question, in order."""

ADD_ALLOWED = ('Add new questions only when the learner asks for them, each as {"action": "add", "question": '
               '{...}}; they go into the deck the learner chose.')
ADD_ELSEWHERE = "Don't add new questions: another request handles that. Only change the questions here."

FOLLOW_UP_INSTRUCTIONS = """The learner has looked over the draft and asks for changes in <learner_feedback>.
<draft> is the draft as it stands now. Each question has its number ("id") and "ticked" (false
means the learner unticked it: they don't want it as it is). A question that is one of the
learner's existing questions also has "now" (what it is) and "proposed" (your proposal for it,
absent if you kept it). The other questions are new questions you proposed.

Reply with the changes only, as one JSON object and nothing else:
{{"note": "<optional: a sentence or two to the learner>", "deck": "<only to rename the deck>",
  "questions": [...]}}
where each entry is one of
  {{"id": <n>, "action": "rewrite", "reason": "<a few words>", "question": {{...}}}}: replace question n
    (for an existing question, this is your new proposal for it)
  {{"id": <n>, "action": "split", "reason": "<a few words>", "questions": [{{...}}, ...]}}: the same, as
    2-4 questions
  {{"id": <n>, "action": "remove"}}: drop a new question from the draft
  {{"id": <n>, "action": "keep", "reason": "<a few words>"}}: withdraw your proposal for an existing
    question, leaving it as it is
  {{"action": "add", "question": {{...}}}}: a new question. {adding}
Leave out every question you don't change."""


class DeckMakerError(ValueError):
    pass


# -- questions as Claude writes them ---------------------------------------------------------
def question_spec(raw) -> dict:
    """{"kind", "fields", "tags"} of a question as Claude writes it (QUESTION_FORMAT). Raises
    ValueError (or TypeError, KeyError) if it can't make a question. The old type names (flashcard,
    reversed, cloze, numeric, written...) are read too."""
    if not isinstance(raw, dict):
        raise ValueError("a question isn't a JSON object")
    name = str(raw.get("type", "")).lower().replace("-", "_").replace(" ", "_")
    raw_tags = raw.get("tags")
    tags = [question_model.clean_tag(t) for t in (raw_tags.split() if isinstance(raw_tags, str) else raw_tags or [])
            if isinstance(t, str)]
    tags = list(dict.fromkeys(t for t in tags if t))
    if name in ("fill_in", "fill_in_the_blank", "fill_in_blank", "cloze", "cloze_deletion"):
        text = str(raw.get("text") or raw.get("question") or "")
        kind, fields = "fill_in", {"text": text, "extra": str(raw.get("explanation") or raw.get("extra") or "")}
    elif name in ("both_ways", "self_check_both_ways", "reversed", "reverse", "flashcard_both_ways"):
        q = quiz.validate_question({**raw, "type": "self_check"})
        kind, fields = "both_ways", question_model.question_fields(q)[1]
    else:
        if name in ("image_labels", "occlusion", "image_occlusion"):
            raise ValueError("image labels questions can't be written here")
        q = quiz.validate_question(raw)
        kind, fields = question_model.question_fields(q)
    if kind == "fill_in" and not question_model.question_ords(kind, fields):
        raise ValueError("a fill-in-the-blank needs a blank {{c1::...}}")
    try:
        question_model.check(kind, fields)
    except question_model.QuestionError as e:
        raise ValueError(str(e)) from None
    return {"kind": kind, "fields": fields, "tags": tags}


def for_claude(spec: dict) -> dict:
    """A question spec back in the JSON Claude writes (QUESTION_FORMAT)."""
    kind, f = question_model.kind_name(spec["kind"]), spec["fields"]
    if kind in SELF_CHECKS:
        out = {"type": kind, "question": f.get("front", ""), "answer": f.get("back", "")}
        if f.get("extra"):
            out["explanation"] = f["extra"]
    elif kind == "fill_in":
        out = {"type": "fill_in", "text": f.get("text", "")}
        if f.get("extra"):
            out["explanation"] = f["extra"]
    else:
        out = {k: v for k, v in f.items() if k not in ("answer_latex", "page", "section", "revises", "html")}
        out["type"] = kind
    if spec.get("tags"):
        out["tags"] = spec["tags"]
    return out


def previews(spec: dict) -> list[dict]:
    """The questions a question spec makes, as quiz.js draws them (each blank of a
    fill-in-the-blank, both sides of a both-ways self-check)."""
    kind = question_model.kind_name(spec["kind"])
    return [question_model.question_data(kind, spec["fields"], o)
            for o in question_model.question_ords(kind, spec["fields"])]


def specs(proposal) -> list[dict]:
    """A draft item's proposed questions (proposal: its JSON column, or the list), with their
    kinds by their current names."""
    if isinstance(proposal, str):
        proposal = json.loads(proposal) if proposal else []
    return [{**s, "kind": question_model.kind_name(s.get("kind"))} if isinstance(s, dict) else s for s in proposal or []]


def existing_payload(db: Database, question, number: int) -> dict:
    """What Claude sees of one of the learner's questions: where it is, its type and its content."""
    group = db.question_group(question["group_id"])
    deck = db.deck(question["deck_id"])
    kind = question_model.kind_name(group["kind"] if group else json.loads(question["data"]).get("type"))
    fields = json.loads(group["fields"]) if group else {}
    out = {"id": number, "deck": deck["name"] if deck else "", "tags": group["tags"].split() if group else []}
    if kind in SELF_CHECKS:
        out.update(for_claude({"kind": kind, "fields": fields}))
        if kind == "both_ways":
            out["side"] = "back to front" if question["ord"] == 1 else "front to back"
    elif kind == "fill_in":
        out.update(for_claude({"kind": kind, "fields": fields}), blank=question["ord"] + 1)
    elif kind == "image_labels":
        out.update(type="image_labels", header=fields.get("header", ""),
                   note=f"masks region {question['ord'] + 1} of the image {fields.get('image', '')}")
        if fields.get("extra"):
            out["explanation"] = fields["extra"]
    else:
        out.update(for_claude({"kind": kind, "fields": fields}))
    if not out["tags"]:
        del out["tags"]
    return out


def question_images(question) -> list[Path]:
    """Image files a question shows, that can be attached for Claude to see."""
    q = json.loads(question["data"])
    names = question_model.media_files("self_check", {"front": q.get("question", ""), "back": str(q.get("answer", "")),
                                                      "extra": q.get("explanation", "")})
    if question_model.kind_name(q.get("type")) == "image_labels" and q.get("image"):
        names.append(q["image"])
    out = []
    for name in names:
        path = media_dir() / name
        if "/" not in name and path.suffix.lower() in IMAGE_TYPES and path.is_file() \
                and path.stat().st_size <= MAX_IMAGE_BYTES:
            out.append(path)
    return out


def batches(db: Database, question_ids) -> list[list[int]]:
    """Existing questions grouped into requests: smaller groups for questions with images."""
    plain, pictured = [], []
    for question_id in dict.fromkeys(question_ids):
        question = db.question(question_id)
        if question is not None:
            (pictured if question_images(question) else plain).append(question_id)
    return ([plain[i:i + BATCH_SIZE] for i in range(0, len(plain), BATCH_SIZE)]
            + [pictured[i:i + IMAGE_BATCH_SIZE] for i in range(0, len(pictured), IMAGE_BATCH_SIZE)])


def usual_deck(db: Database, question_ids) -> str:
    """The deck most of these questions are in (where questions added while improving them go)."""
    decks = Counter(c["deck_id"] for c in map(db.question, question_ids) if c is not None)
    deck = db.deck(decks.most_common(1)[0][0]) if decks else None
    return deck["name"] if deck else DEFAULT_DECK


# -- requests --------------------------------------------------------------------------------
def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tags(db: Database) -> list[dict]:
    tags = practice.concept_tags(db)[:practice.MAX_TAGS_IN_CONTEXT]
    return [_text(f"Tags already in the learner's questions:\n<existing_tags>{' '.join(tags)}</existing_tags>")] if tags else []


def new_blocks(db: Database, request: dict) -> list[dict]:
    """The first request for a new deck. Attached files stay a source_files block until
    expand_files reads them (off the GUI thread)."""
    blocks = [_text(QUESTION_FORMAT), _text(NEW_INSTRUCTIONS), *_tags(db)]
    if request.get("notes", "").strip():
        blocks.append(_text(f"The learner's material:\n<notes>\n{request['notes'].strip()}\n</notes>"))
    if request.get("files"):
        blocks.append({"type": "source_files", "paths": list(request["files"])})
    blocks.append(_text(f"<request>\n{request.get('description', '').strip()}\n</request>"))
    return blocks


def improve_blocks(db: Database, items, instructions: str, adding: bool) -> list[dict]:
    """The first request for a batch of existing questions (items: their draft rows)."""
    blocks = [_text(QUESTION_FORMAT), _text(IMPROVE_INSTRUCTIONS.format(adding=ADD_ALLOWED if adding else ADD_ELSEWHERE)),
              *_tags(db)]
    if instructions.strip():
        blocks.append(_text(f"The learner adds:\n<learner_instructions>\n{instructions.strip()}\n</learner_instructions>"))
    payload, attached = [], 0
    for item in items:
        question = db.question(item["question_id"])
        if question is None:
            continue
        payload.append(existing_payload(db, question, item["number"]))
        for path in question_images(question):
            if attached >= MAX_QUESTION_IMAGES:
                break
            blocks += [_text(f"Image {path.name} (question {item['number']}):"), image_block(path)]
            attached += 1
    blocks.append(_text(f"<questions>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</questions>"))
    return blocks


def draft_state(db: Database, items) -> list[dict]:
    """The draft's questions as follow-ups show them to Claude."""
    out = []
    for item in items:
        entry = {"id": item["number"], "ticked": bool(item["checked"])}
        questions = [for_claude(s) for s in specs(item["proposal"])]
        if item["question_id"] is not None:
            question = db.question(item["question_id"])
            if question is None:
                continue
            now = existing_payload(db, question, item["number"])
            now.pop("id", None)
            entry["now"] = now
            if questions:
                entry["proposed"] = questions if len(questions) > 1 else questions[0]
            elif item["status"] == "error":
                entry["error"] = item["reason"]
        elif questions:
            entry["question"] = questions[0]
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
    """The question numbers a follow-up names: "#4", "#12-15", "#3 to #6"."""
    out: set[int] = set()
    for m in MENTION.finditer(feedback):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        lo, hi = min(a, b), max(a, b)
        out.update(range(lo, min(hi, lo + MAX_RANGE) + 1))
    return out


def follow_up_targets(db: Database, draft, feedback: str) -> tuple[list[int], bool]:
    """The batches a follow-up goes to, and whether it names questions. A follow-up that names questions
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
    items = [i for i in db.draft_items(draft["id"], batch_id) if i["question_id"] is not None]
    return improve_blocks(db, items, request.get("instructions", ""), adding=is_first_batch(db, draft, batch_id))


def is_first_batch(db: Database, draft, batch_id: int) -> bool:
    """Only the draft's first request may add new questions (so batches don't add the same ones)."""
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
    if isinstance(data, dict):  # replies said "cards" before the rename
        raw_entries = data.get("questions") if isinstance(data.get("questions"), list) else data.get("cards")
    else:
        raw_entries = data
    if not isinstance(raw_entries, list):
        raise DeckMakerError("Claude's reply had no list of questions")
    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw["id"]) if raw.get("id") is not None else None
        except (TypeError, ValueError):
            number = None
        action = str(raw.get("action") or "").lower()
        given = next((raw[k] for k in ("questions", "question", "cards", "card") if isinstance(raw.get(k), (dict, list))
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
            if not given and isinstance(raw.get("type"), str):  # the question itself, flattened into the entry
                given = {k: v for k, v in raw.items() if k not in ("id", "action", "reason")}
            given = given if isinstance(given, list) else [given] if given else []
            try:
                if not given:
                    raise ValueError("no question was given")
                if len(given) > MAX_SPLIT and action == "rewrite":
                    raise ValueError(f"split into {len(given)} questions (at most {MAX_SPLIT})")
                entry["specs"] = [question_spec(g) for g in given]
            except (ValueError, TypeError, KeyError) as e:
                entry.update(action="error", reason=f"unusable question: {e}", adding=action == "add")
        entries.append(entry)
    deck = data.get("deck") if isinstance(data, dict) else None
    note = data.get("note") if isinstance(data, dict) else None
    return {"deck": deck.strip() if isinstance(deck, str) else "", "note": note.strip() if isinstance(note, str) else "",
            "entries": entries}


def needless(current_kind: str, current_fields: dict, spec: dict) -> str | None:
    """Why a proposed self-check shouldn't replace a question, if it shouldn't: questions the
    learner answers don't become self-checks, and a self-check saying the same needs no change."""
    current_kind = question_model.kind_name(current_kind)
    if spec["kind"] not in SELF_CHECKS:
        return None
    if current_kind in ANSWERED:
        return "questions you answer stay that way"
    if current_kind == spec["kind"] and all(
        question_model.plain(str(current_fields.get(k) or "")) == question_model.plain(str(spec["fields"].get(k) or ""))
        for k in ("front", "back")
    ):
        return "the self-check would be the same"
    return None


def _dumps(specs_: list[dict]) -> str:
    return json.dumps(specs_, ensure_ascii=False)


def record(db: Database, batch_id: int, session_id: str, text: str) -> None:
    """Apply Claude's reply for a batch to the draft. The batch's first reply settles every
    existing question of the batch (a question the reply leaves out is an error); a follow-up's changes
    only the questions it names."""
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
            item = None  # another batch's question
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
                db.update_draft_item(item["id"], status="error", reason="Claude left this question out")
    db.update_draft_batch(batch_id, status="done", error="", session_id=session_id or batch["session_id"],
                          note=reply["note"])


def _apply_entry(db: Database, draft, batch_id: int, item, entry: dict) -> None:
    action, reason = entry["action"], entry["reason"]
    if item["question_id"] is None:  # a new question the draft proposes
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
    question = db.question(item["question_id"])
    group = db.question_group(question["group_id"]) if question else None
    if group is None:
        return
    if action in ("keep", "remove"):
        db.update_draft_item(item["id"], status="kept", proposal="", reason=reason, checked=1)
    elif action == "error":
        db.update_draft_item(item["id"], status="error", proposal="", reason=reason)
    else:
        fields = json.loads(group["fields"])
        why = next((w for s in entry["specs"] if (w := needless(group["kind"], fields, s))), None)
        if why:
            db.update_draft_item(item["id"], status="kept", proposal="", checked=1,
                                 reason=f"{reason} ({why})" if reason else why)
        else:
            db.update_draft_item(item["id"], status="proposed", proposal=_dumps(entry["specs"]), reason=reason,
                                 checked=1)


def fail(db: Database, batch_id: int, message: str) -> None:
    """A request failed: its batch says why, and the questions still waiting for a first answer are errors."""
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


def start_improve(db: Database, question_ids, instructions: str = "", model: str = MODEL) -> tuple[int, list[int]]:
    """A draft improving existing questions, replacing any other: (draft id, its batches' ids)."""
    groups = batches(db, question_ids)
    ids = [i for g in groups for i in g]
    draft_id = db.add_draft("improve", {"instructions": instructions.strip(), "count": len(ids)},
                            usual_deck(db, ids), model)
    out = []
    for group in groups:
        batch_id = db.add_draft_batch(draft_id)
        for question_id in group:
            db.add_draft_item(draft_id, batch_id, question_id=question_id)
        out.append(batch_id)
    return draft_id, out


def counts(db: Database, draft_id: int) -> dict:
    """How the draft's questions stand: {status: n}, plus "ticked" (proposed and ticked, new and
    rewrites), "rewrites" of those, and "new_questions" (the questions the ticked new ones make)."""
    out = Counter()
    for item in db.draft_items(draft_id):
        out[item["status"]] += 1
        if item["status"] == "proposed" and item["checked"]:
            out["ticked"] += 1
            if item["question_id"] is not None:
                out["rewrites"] += 1
            else:
                out["new_questions"] += sum(len(previews(s)) for s in specs(item["proposal"]))
    return dict(out)


# -- applying ------------------------------------------------------------------------------
def apply(db: Database, draft_id: int) -> dict:
    """Add the draft's ticked new questions to its deck and apply its ticked rewrites; the draft is
    then gone. {"added": question ids, "rewritten": question ids, "deck": deck id or None}."""
    draft = db.draft()
    if draft is None or draft["id"] != draft_id:
        return {"added": [], "rewritten": [], "deck": None}
    items = [i for i in db.draft_items(draft_id) if i["status"] == "proposed" and i["checked"]]
    deck_id = None
    added, rewritten = [], []
    with db.conn:
        for item in items:
            if item["question_id"] is not None:
                if db.question(item["question_id"]) is not None:
                    added += rewrite(db, item["question_id"], specs(item["proposal"]))
                    rewritten.append(item["question_id"])
                continue
            if deck_id is None:
                deck_id = db._ensure_deck(draft["deck"] or DEFAULT_DECK)
            for spec in specs(item["proposal"]):
                group_id = db.add_question_group(spec["kind"], spec["fields"], deck_id, tags=spec.get("tags", []))
                added += [c["id"] for c in db.group_questions(group_id)]
        db.conn.execute("DELETE FROM deck_drafts WHERE id=?", (draft_id,))
    return {"added": added, "rewritten": rewritten, "deck": deck_id}


def rewrite(db: Database, question_id: int, new: list[dict]) -> list[int]:
    """Give a question its proposed content (the first question spec; a split's others become new
    questions in its deck, book and page, with its tags), keeping its deck, schedule and past
    attempts. What it was is kept for undo_rewrite. Returns the ids of the questions it added."""
    question = db.question(question_id)
    group = db.question_group(question["group_id"])
    first, *rest = new
    original = {"group_id": group["id"], "kind": group["kind"], "fields": json.loads(group["fields"]),
                "ord": question["ord"], "data": json.loads(question["data"])}
    tags = list(dict.fromkeys([*group["tags"].split(), *first.get("tags", [])]))
    ords = question_model.question_ords(first["kind"], first["fields"])
    with db.conn:
        group_id = db.move_question_to_group(question_id, first["kind"], first["fields"])  # as its first question, ords[0]
        db.update_question_group(group_id, tags=tags)
        added = [db.ensure_question(group_id, o, question["deck_id"]) for o in ords[1:]]
        for spec in rest:
            other = db.add_question_group(spec["kind"], spec["fields"], question["deck_id"],
                                          tags=list(dict.fromkeys([*group["tags"].split(), *spec.get("tags", [])])),
                                          book_id=question["book_id"], page=question["page"], title=question["title"])
            added += [c["id"] for c in db.group_questions(other)]
        if added:
            db.conn.execute(f"UPDATE questions SET book_id=?, page=? WHERE id IN ({','.join('?' * len(added))})",
                            (question["book_id"], question["page"], *added))
            original["added"] = added
        db.add_question_rewrite(question_id, original)
    return added


def undo_rewrite(db: Database, question_id: int) -> bool:
    """Put a rewritten question back as it was (on its old question_groups row if that still exists
    and hasn't a question in its place), keeping its schedule. Questions the rewrite added go unless
    they have been attempted. A rewrite stored before the rename names its row note_id and its kind
    by the old names."""
    r = db.question_rewrite(question_id)
    question = db.question(question_id)
    if r is None or question is None:
        return False
    original = json.loads(r["original"])
    old = db.question_group(original.get("group_id", original.get("note_id")))
    kind, fields = question_model.kind_name(original["kind"]), original["fields"]
    if kind in question_model.QUESTION_TYPES:
        fields = {**fields, "type": kind}
    taken = old is not None and any(x["ord"] == original["ord"] for x in db.group_questions(old["id"]))
    with db.conn:
        if old is not None and not taken:
            db.move_question(question_id, old["id"], original["ord"])
        elif kind in (*question_model.QUESTION_TYPES, "self_check"):
            db.move_question_to_group(question_id, kind, fields)
        else:  # a both-ways, fill-in or image labels question whose row is gone: keep what it showed, as a self-check
            q = original["data"]
            back = q.get("answer", "") if question_model.kind_name(q.get("type")) != "image_labels" else ""
            db.move_question_to_group(question_id, "self_check", {"front": q.get("question", "") or "(question)",
                                                                  "back": back, "extra": q.get("explanation", "")})
        added = [i for i in original.get("added", []) if db.question(i) is not None and not db.attempts(i)]
        if added:
            db.delete_questions(added)
        db.delete_question_rewrite(r["id"])
    return True
