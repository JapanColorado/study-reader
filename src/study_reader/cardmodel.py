"""What review cards are made of: each card has a type, and its fields (stored together as a
card_notes row, which a few types share among several cards) give the question it shows in
review (the JSON review.html and quiz.js draw). Qt-free.

Types and their fields (text fields are Markdown that may hold inline HTML, with $math$ or
\\(math\\)):
- flashcard: front, back, extra (shown under the answer); also every flashcard in Claude's
  quizzes (card_fields)
- reversed: like flashcard, plus a second card asking the back for the front
- cloze: text with {{c1::answer}} or {{c1::answer::hint}} deletions, one card per number; extra
- occlusion: an image (media file name) with shapes masked over it, one card per shape number;
  header (shown above it), extra
- choice, truefalse, numeric, written: a question the learner answers, as quiz.py parses it
  from Claude's quizzes, with its "type" the card's type (question, answer, ...)
The cards of one row share their fields and tags: editing a cloze changes its other cloze
cards, and a reversed flashcard's two sides change together.
"""

from __future__ import annotations

import html
import json
import re

# Every card type, in the order they are listed (the browser, the Add dialog, the editor).
TYPES = ("flashcard", "reversed", "cloze", "occlusion", "choice", "truefalse", "numeric", "written")
TYPE_LABELS = {
    "flashcard": "Flashcard", "reversed": "Flashcard (both ways)", "cloze": "Cloze", "occlusion": "Image occlusion",
    "choice": "Multiple choice", "truefalse": "True/false", "numeric": "Numeric", "written": "Written answer",
}
# Types whose fields are a quiz question (quiz.validate_question): answered, and graded unless written.
QUESTION_TYPES = ("choice", "truefalse", "numeric", "written")
TEXT_FIELDS = {
    "flashcard": ("front", "back", "extra"),
    "reversed": ("front", "back", "extra"),
    "cloze": ("text", "extra"),
    "occlusion": ("header", "extra"),
}
FIELD_LABELS = {"front": "Front", "back": "Back", "extra": "Extra", "text": "Text", "header": "Header"}
# Card data types a reviewer rates on their own after revealing the answer (nothing is graded);
# in review, written answers are rated this way too.
SELF_RATED = ("flashcard", "cloze", "occlusion")

# {{c1::answer}} or {{c1::answer::hint}}; like Anki, the deletion ends at the first }} that isn't
# inside math the answer holds ({{c1::\(\sqrt{LC}}\)}}).
CLOZE = re.compile(r"\{\{c(\d+)::((?:\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$|.)*?)(?:::((?:(?!\}\}).)*?))?\}\}",
                   re.DOTALL)
# Math as markdown.js finds it: $$..$$, \[..\], \(..\), $..$ (not \$, which may sit inside: $p = \$1$).
MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<!\\)\$(?!\s)((?:\\.|[^$\\\n])+?)(?<![\s\\])\$(?!\d)", re.DOTALL)
TAG = re.compile(r"<[^>]*>")


class NoteError(ValueError):
    """Fields that can't make any card (e.g. a cloze without deletions); the message is shown."""


# -- the cards fields make -----------------------------------------------------------------
def card_ords(kind: str, fields: dict) -> list[int]:
    """Which cards the fields make: 0 for the first, 1 for a reversed flashcard's reverse side,
    n-1 for cloze n."""
    if kind == "reversed":
        return [0, 1] if _filled(fields.get("back")) else [0]
    if kind == "cloze":
        return sorted({int(m.group(1)) - 1 for m in CLOZE.finditer(fields.get("text", "")) if int(m.group(1)) > 0})
    if kind == "occlusion":
        return sorted({int(s["ord"]) - 1 for s in fields.get("shapes", []) if int(s.get("ord", 0)) > 0})
    return [0]


def check(kind: str, fields: dict) -> None:
    """Raise NoteError if the card can't be saved as it is."""
    if kind not in TYPES:
        raise NoteError(f"There is no card type {kind!r}.")
    if kind in ("flashcard", "reversed") and not _filled(fields.get("front")):
        raise NoteError("The front is empty.")
    if kind == "cloze" and not card_ords(kind, fields):
        raise NoteError("A cloze needs a deletion: {{c1::text}} (select text and press Ctrl+Shift+C).")
    if kind == "occlusion" and (not fields.get("image") or not card_ords(kind, fields)):
        raise NoteError("An image occlusion needs an image and at least one shape.")
    if kind in QUESTION_TYPES and not _filled(fields.get("question")):
        raise NoteError("The question is empty.")
    if kind in QUESTION_TYPES and fields.get("type") != kind:
        raise NoteError(f"A {TYPE_LABELS[kind].lower()} card holds a {fields.get('type')!r} question.")


def card_fields(q: dict) -> tuple[str, dict]:
    """(type, fields) of the card for a question as quiz.py parses it: a flashcard's front,
    back and extra, or the question itself."""
    if q.get("type") == "flashcard":
        return "flashcard", {"front": str(q.get("question", "")), "back": str(q.get("answer", "")),
                             "extra": str(q.get("explanation") or "")}
    return q["type"], dict(q)


def card_data(kind: str, fields: dict, ord: int) -> dict:
    """The question a card shows in review, as quiz.js draws it. html: its text may hold HTML."""
    if kind in QUESTION_TYPES:
        return {**fields, "type": kind}
    if kind in ("flashcard", "reversed"):
        front, back = fields.get("front", ""), fields.get("back", "")
        if ord == 1:
            front, back = back, front
        return {"type": "flashcard", "question": front, "answer": back, "explanation": fields.get("extra", ""),
                "html": True}
    if kind == "cloze":
        text = fields.get("text", "")
        return {"type": "cloze", "question": cloze_side(text, ord + 1, False), "answer": cloze_side(text, ord + 1, True),
                "explanation": fields.get("extra", ""), "html": True}
    if kind == "occlusion":
        return {"type": "occlusion", "question": fields.get("header", ""), "image": fields.get("image", ""),
                "shapes": fields.get("shapes", []), "active": ord + 1, "explanation": fields.get("extra", ""),
                "html": True}
    raise NoteError(f"There is no card type {kind!r}.")


def cloze_side(text: str, number: int, revealed: bool) -> str:
    """The text of cloze card `number`: its deletions hidden ([...] or [hint]) or revealed, and
    marked; the other deletions just show their text. Inside math they are marked with
    \\htmlClass{cloze}{...}."""
    spans = math_spans(text)

    def replace(m: re.Match) -> str:
        content, hint = m.group(2), m.group(3)
        if int(m.group(1)) != number:
            return content
        in_math = any(a < m.start() < b for a, b in spans)
        if in_math:
            shown = content if revealed else r"\text{[%s]}" % (_tex_text(hint) if hint else r"\dots")
            return r"\htmlClass{cloze}{%s}" % shown
        shown = content if revealed else f"[{hint or '…'}]"
        return f'<span class="cloze">{shown}</span>'

    return CLOZE.sub(replace, text)


def math_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in MATH.finditer(text)]


def _tex_text(s: str) -> str:
    return re.sub(r"([#$%&_{}\\])", r"\\\1", s)


def renumber_next(text: str) -> int:
    """The cloze number a new deletion would get (one past the highest)."""
    return max((int(m.group(1)) for m in CLOZE.finditer(text)), default=0) + 1


# -- occlusion -----------------------------------------------------------------------------
OCCLUSION = re.compile(r"\{\{c(\d+)::image-occlusion:(\w+):(.*?)\}\}", re.DOTALL)


def parse_occlusion(text: str) -> list[dict]:
    """Shapes from Anki's occlusion field: {ord, shape, left, top, width, height, points, oi}.
    Positions are fractions of the image size."""
    shapes = []
    for m in OCCLUSION.finditer(html.unescape(text)):
        shape = {"ord": int(m.group(1)), "shape": m.group(2)}
        for prop in m.group(3).split(":"):
            key, _, value = prop.partition("=")
            if key in ("left", "top", "width", "height", "rx", "ry", "angle"):
                try:
                    shape[key] = float(value)
                except ValueError:
                    pass
            elif key == "points":
                pts = []
                for pair in value.split():
                    x, _, y = pair.partition(",")
                    try:
                        pts.append([float(x), float(y)])
                    except ValueError:
                        pass
                shape["points"] = pts
            elif key == "oi":
                shape["oi"] = value == "1"
            elif key == "text":
                shape["text"] = value
        shapes.append(shape)
    return shapes


# -- text ----------------------------------------------------------------------------------
def plain(text: str) -> str:
    """Readable plain text of a field: deletions shown, tags, entities and Markdown marks gone
    (math stays as written), one line."""
    text = CLOZE.sub(lambda m: m.group(2), text or "")
    text = re.sub(r"<br\s*/?>|</(div|p|li|tr|h\d)>", " ", text, flags=re.I)
    text = html.unescape(TAG.sub("", text))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links: their text
    text = re.sub(r"(?<![\\\w])(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)  # emphasis
    text = re.sub(r"(?<!\\)`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6}\s+|>\s?|[-+*]\s+(?=\S))", "", text, flags=re.M)
    text = re.sub(r"\\([*_`$#\\~\[\]-])", r"\1", text)
    return " ".join(text.split())


def text_fields(kind: str, fields: dict) -> list[str]:
    """The card's text, field by field, most important first."""
    if kind in QUESTION_TYPES:
        out = [str(fields.get("question", ""))]
        out += [str(c) for c in fields.get("choices", [])]
        answer = fields.get("answer")
        if isinstance(answer, str) or (isinstance(answer, (int, float)) and fields.get("type") == "numeric"):
            out.append(str(answer))
        elif isinstance(answer, bool):
            out.append("true" if answer else "false")
        out += [str(fields.get(k) or "") for k in ("explanation", "rubric", "unit")]
        return out
    return [str(fields.get(k, "")) for k in TEXT_FIELDS.get(kind, ())]


def sort_text(kind: str, fields: dict) -> str:
    """What the browser shows (and sorts by) for a card: its first field as plain text."""
    texts = [plain(t) for t in text_fields(kind, fields)]
    if kind == "occlusion":  # the header, else whatever text it has, else the image
        return next((t for t in texts if t), f"(image) {fields.get('image', '')}")[:300]
    return texts[0][:300] if texts else ""


def answer_text(data: dict) -> str:
    """A card's answer as plain text, for the browser's Answer column."""
    kind = data.get("type")
    if kind == "choice":
        try:
            return plain(data["choices"][data["answer"]])
        except (IndexError, KeyError, TypeError):
            return ""
    if kind == "truefalse":
        return "True" if data.get("answer") else "False"
    if kind == "cloze":
        found = re.findall(r'<span class="cloze">(.*?)</span>|\\htmlClass\{cloze\}\{(.*?)\}', data.get("answer", ""), re.S)
        return plain(" … ".join(a or b for a, b in found))
    if kind == "occlusion":
        return f"shape {data.get('active')}"
    return plain(str(data.get("answer", "")))


def search_text(kind: str, fields: dict) -> str:
    return " \n".join(p for p in (plain(t) for t in text_fields(kind, fields)) if p)


def media_files(kind: str, fields: dict) -> list[str]:
    """Media files (by name) a card's text refers to."""
    names = re.findall(r'<img[^>]+src="([^"]+)"', " ".join(text_fields(kind, fields)), re.I)
    names += re.findall(r"!\[[^\]]*\]\(<?([^)>\s]+)>?\)", " ".join(text_fields(kind, fields)))
    if kind == "occlusion" and fields.get("image"):
        names.append(fields["image"])
    return [n for n in dict.fromkeys(names) if "://" not in n and not n.startswith("data:")]


def _filled(value) -> bool:
    return bool(plain(str(value or "")) or re.search(r"<img|!\[", str(value or "")))


# -- tags ----------------------------------------------------------------------------------
def split_tags(text: str) -> list[str]:
    """Tags as typed (space or comma separated), without duplicates; Anki-style, no spaces inside."""
    return list(dict.fromkeys(t for t in re.split(r"[\s,]+", text or "") if t))


def tag_part(title: str) -> str:
    """Text as one part of a tag: no spaces, commas, control characters (some PDFs pad their
    contents' titles with NULs) or '::' (which would split it)."""
    part = re.sub(r"[\s,\x00-\x1f\x7f]+", "_", str(title).strip()).replace("::", ":")
    return re.sub(r"_+", "_", part).strip("_:")[:80]


def clean_tag(tag: str) -> str:
    """A tag as written (by Claude, say) made storable: each Parent::child part a tag_part."""
    return "::".join(p for p in (tag_part(x) for x in str(tag).split("::")) if p)


def join_tags(tags) -> str:
    """Stored as ' a b c ' so a tag can be found with LIKE '% tag %'."""
    tags = list(dict.fromkeys(tags))
    return f" {' '.join(tags)} " if tags else ""


def dumps(fields: dict) -> str:
    return json.dumps(fields, ensure_ascii=False)
