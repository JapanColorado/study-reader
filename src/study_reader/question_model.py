"""What questions are made of: each question has a type, and its fields (stored together as a
question_groups row, which a few types share among several questions) give what it shows in
practice (the JSON practice.html and quiz.js draw). Qt-free.

Types and their fields (text fields are Markdown that may hold inline HTML, with $math$ or
\\(math\\)):
- self_check: front, back, extra (shown under the answer); also every self-check in Claude's
  quizzes (question_fields)
- both_ways: like self_check, plus a second question asking the back for the front
- fill_in: text with {{c1::answer}} or {{c1::answer::hint}} blanks (Anki's cloze syntax), one
  question per number; extra
- image_labels: an image (media file name) with shapes masked over it, one question per shape
  number; header (shown above it), extra
- choice, truefalse, calculation, short_answer: a question the learner answers, as quiz.py parses
  it from Claude's quizzes, with its "type" the question's type (question, answer, ...)
The questions of one row share their fields and tags: editing a fill-in-the-blank changes its
other blanks' questions, and a both-ways self-check's two sides change together.
"""

from __future__ import annotations

import html
import json
import re

# Every question type, in the order they are listed (the browser, the Add dialog, the editor).
TYPES = ("self_check", "both_ways", "fill_in", "image_labels", "choice", "truefalse", "calculation", "short_answer")
TYPE_LABELS = {
    "self_check": "Self-check", "both_ways": "Self-check (both ways)", "fill_in": "Fill-in-the-blank",
    "image_labels": "Image labels", "choice": "Multiple choice", "truefalse": "True/false",
    "calculation": "Calculation", "short_answer": "Short answer",
}
# The names types had before (stored rows and question JSON from older versions, Anki-style
# kinds before that, and "free" for short answers): old name -> type.
KIND_ALIASES = {
    "flashcard": "self_check", "reversed": "both_ways", "cloze": "fill_in", "occlusion": "image_labels",
    "numeric": "calculation", "written": "short_answer", "free": "short_answer",
    "basic": "self_check", "reverse": "both_ways",
}
# Types whose fields are a quiz question (quiz.validate_question): answered, and graded unless a
# short answer.
QUESTION_TYPES = ("choice", "truefalse", "calculation", "short_answer")
TEXT_FIELDS = {
    "self_check": ("front", "back", "extra"),
    "both_ways": ("front", "back", "extra"),
    "fill_in": ("text", "extra"),
    "image_labels": ("header", "extra"),
}
FIELD_LABELS = {"front": "Front", "back": "Back", "extra": "Extra", "text": "Text", "header": "Header"}
# Question data types the learner rates on their own after revealing the answer (nothing is
# graded); in practice, short answers are rated this way too.
SELF_RATED = ("self_check", "fill_in", "image_labels")


def kind_name(name: str) -> str:
    """A type as stored now, for a name it may have had before (KIND_ALIASES)."""
    return KIND_ALIASES.get(name, name)


# {{c1::answer}} or {{c1::answer::hint}}; like Anki, the blank ends at the first }} that isn't
# inside math the answer holds ({{c1::\(\sqrt{LC}}\)}}). Each math span is taken whole or not at
# all (an atomic group): letting the regex also try it a character at a time made an unclosed
# blank before many math spans backtrack exponentially. Find blanks with fill_ins(), which
# searches no further than the last }}, so math that doesn't close before it is read a
# character at a time.
FILL_IN = re.compile(r"\{\{c(\d+)::((?:(?>\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$|.))*?)(?:::((?:(?!\}\}).)*?))?\}\}",
                     re.DOTALL)
# Math as markdown.js finds it: $$..$$, \[..\], \(..\), $..$ (not \$, which may sit inside: $p = \$1$).
MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<!\\)\$(?!\s)((?:\\.|[^$\\\n])+?)(?<![\s\\])\$(?!\d)", re.DOTALL)
TAG = re.compile(r"<[^>]*>")


class QuestionError(ValueError):
    """Fields that can't make any question (e.g. a fill-in-the-blank without blanks); the message is shown."""


# -- the questions fields make -------------------------------------------------------------
def question_ords(kind: str, fields: dict) -> list[int]:
    """Which questions the fields make: 0 for the first, 1 for a both-ways self-check's reverse
    side, n-1 for blank n."""
    if kind == "both_ways":
        return [0, 1] if _filled(fields.get("back")) else [0]
    if kind == "fill_in":
        return sorted({int(m.group(1)) - 1 for m in fill_ins(fields.get("text", "")) if int(m.group(1)) > 0})
    if kind == "image_labels":
        return sorted({int(s["ord"]) - 1 for s in fields.get("shapes", []) if int(s.get("ord", 0)) > 0})
    return [0]


def check(kind: str, fields: dict) -> None:
    """Raise QuestionError if the question can't be saved as it is."""
    if kind not in TYPES:
        raise QuestionError(f"There is no question type {kind!r}.")
    if kind in ("self_check", "both_ways") and not _filled(fields.get("front")):
        raise QuestionError("The front is empty.")
    if kind == "fill_in" and not question_ords(kind, fields):
        raise QuestionError("A fill-in-the-blank needs a blank: {{c1::text}} (select text and press Ctrl+Shift+C).")
    if kind == "image_labels" and (not fields.get("image") or not question_ords(kind, fields)):
        raise QuestionError("Image labels need an image and at least one shape.")
    if kind in QUESTION_TYPES and not _filled(fields.get("question")):
        raise QuestionError("The question is empty.")
    if kind in QUESTION_TYPES and fields.get("type") != kind:
        raise QuestionError(f"A {TYPE_LABELS[kind].lower()} question can't hold a {fields.get('type')!r} question.")


def question_fields(q: dict) -> tuple[str, dict]:
    """(type, fields) of the question for one as quiz.py parses it: a self-check's front, back
    and extra, or the question itself. Older type names (KIND_ALIASES) are read as their types."""
    kind = kind_name(q.get("type"))
    if kind == "self_check":
        return "self_check", {"front": str(q.get("question", "")), "back": str(q.get("answer", "")),
                              "extra": str(q.get("explanation") or "")}
    return kind, {**q, "type": kind}


def question_data(kind: str, fields: dict, ord: int) -> dict:
    """What a question shows in practice, as quiz.js draws it. html: its text may hold HTML."""
    if kind in QUESTION_TYPES:
        return {**fields, "type": kind}
    if kind in ("self_check", "both_ways"):
        front, back = fields.get("front", ""), fields.get("back", "")
        if ord == 1:
            front, back = back, front
        return {"type": "self_check", "question": front, "answer": back, "explanation": fields.get("extra", ""),
                "html": True}
    if kind == "fill_in":
        text = fields.get("text", "")
        return {"type": "fill_in", "question": fill_in_side(text, ord + 1, False),
                "answer": fill_in_side(text, ord + 1, True), "explanation": fields.get("extra", ""), "html": True}
    if kind == "image_labels":
        return {"type": "image_labels", "question": fields.get("header", ""), "image": fields.get("image", ""),
                "shapes": fields.get("shapes", []), "active": ord + 1, "explanation": fields.get("extra", ""),
                "html": True}
    raise QuestionError(f"There is no question type {kind!r}.")


def fill_in_side(text: str, number: int, revealed: bool) -> str:
    """The text of blank `number`'s question: its blanks hidden ([...] or [hint]) or revealed,
    and marked; the other blanks just show their text. Inside math they are marked with
    \\htmlClass{cloze}{...} (the class pages style blanks with)."""
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

    return _sub_fill_ins(text, replace)


def math_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in MATH.finditer(text)]


def fill_ins(text: str):
    """The blanks in text, in order, as FILL_IN matches: group 1 the number, 2 the answer, 3
    the hint or None."""
    # Every blank ends at a }}. Stopping at the last one also keeps a hint that can't close
    # from scanning on to the end at every :: after it.
    return FILL_IN.finditer(text, 0, text.rfind("}}") + 2)


def _sub_fill_ins(text: str, replace) -> str:
    out, pos = [], 0
    for m in fill_ins(text):
        out += [text[pos:m.start()], replace(m)]
        pos = m.end()
    return "".join(out) + text[pos:]


def _tex_text(s: str) -> str:
    return re.sub(r"([#$%&_{}\\])", r"\\\1", s)


def renumber_next(text: str) -> int:
    """The number a new blank would get (one past the highest)."""
    return max((int(m.group(1)) for m in fill_ins(text)), default=0) + 1


# -- image labels -------------------------------------------------------------------------
IMAGE_LABELS = re.compile(r"\{\{c(\d+)::image-occlusion:(\w+):(.*?)\}\}", re.DOTALL)


def parse_image_labels(text: str) -> list[dict]:
    """Shapes from Anki's image occlusion field: {ord, shape, left, top, width, height, points, oi}.
    Positions are fractions of the image size."""
    shapes = []
    for m in IMAGE_LABELS.finditer(html.unescape(text)):
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
    """Readable plain text of a field: blanks filled in, tags, entities and Markdown marks gone
    (math stays as written), one line."""
    text = _sub_fill_ins(text or "", lambda m: m.group(2))
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
    """The question's text, field by field, most important first."""
    if kind in QUESTION_TYPES:
        out = [str(fields.get("question", ""))]
        out += [str(c) for c in fields.get("choices", [])]
        answer = fields.get("answer")
        if isinstance(answer, str) or (isinstance(answer, (int, float)) and fields.get("type") == "calculation"):
            out.append(str(answer))
        elif isinstance(answer, bool):
            out.append("true" if answer else "false")
        out += [str(fields.get(k) or "") for k in ("explanation", "rubric", "unit")]
        return out
    return [str(fields.get(k, "")) for k in TEXT_FIELDS.get(kind, ())]


def sort_text(kind: str, fields: dict) -> str:
    """What the browser shows (and sorts by) for a question: its first field as plain text."""
    texts = [plain(t) for t in text_fields(kind, fields)]
    if kind == "image_labels":  # the header, else whatever text it has, else the image
        return next((t for t in texts if t), f"(image) {fields.get('image', '')}")[:300]
    return texts[0][:300] if texts else ""


def answer_text(data: dict) -> str:
    """A question's answer as plain text, for the browser's Answer column."""
    kind = data.get("type")
    if kind == "choice":
        try:
            return plain(data["choices"][data["answer"]])
        except (IndexError, KeyError, TypeError):
            return ""
    if kind == "truefalse":
        return "True" if data.get("answer") else "False"
    if kind == "fill_in":
        found = re.findall(r'<span class="cloze">(.*?)</span>|\\htmlClass\{cloze\}\{(.*?)\}', data.get("answer", ""), re.S)
        return plain(" … ".join(a or b for a, b in found))
    if kind == "image_labels":
        return f"shape {data.get('active')}"
    return plain(str(data.get("answer", "")))


def search_text(kind: str, fields: dict) -> str:
    return " \n".join(p for p in (plain(t) for t in text_fields(kind, fields)) if p)


def media_files(kind: str, fields: dict) -> list[str]:
    """Media files (by name) a question's text refers to."""
    names = re.findall(r'<img[^>]+src="([^"]+)"', " ".join(text_fields(kind, fields)), re.I)
    names += re.findall(r"!\[[^\]]*\]\(<?([^)>\s]+)>?\)", " ".join(text_fields(kind, fields)))
    if kind == "image_labels" and fields.get("image"):
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
