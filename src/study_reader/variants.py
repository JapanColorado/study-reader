"""Fresh versions of calculation problems for review: after a numeric or free-response quiz card
is reviewed (or first answered in its chat), Claude writes the same problem with new numbers,
which review shows the next time the card comes up (cards.variant), so a remembered answer
doesn't stand in for doing the problem. It is asked in a fresh session without the book (the
problem and its solution are enough), to keep it cheap. Questions Claude marked "varies": false
when writing the quiz are never asked about; for older ones without the mark Claude may decline
(a concept rather than a calculation), and that is remembered. Qt-free."""

from __future__ import annotations

import json
import re

from . import quiz

TYPES = ("numeric", "free")
SKIP = {"skip": True}
# Fields of a question that aren't part of the problem itself.
_EXTRA = ("page", "tags", "revises", "section", "answer_latex", "html", "varies")


def _load(card) -> dict | None:
    return json.loads(card["variant"]) if card["variant"] else None


def wants_variant(card) -> bool:
    """A card whose next version is worth asking for: a numeric or free-response question not
    known to have nothing to vary."""
    q = json.loads(card["data"])
    return q.get("type") in TYPES and q.get("varies") is not False and _load(card) != SKIP


def question(card) -> dict:
    """The question review shows: the latest version with new numbers, else the card's own."""
    variant = _load(card)
    return variant if variant and variant != SKIP else json.loads(card["data"])


def is_varied(card) -> bool:
    variant = _load(card)
    return bool(variant) and variant != SKIP


def _problem(q: dict) -> str:
    return json.dumps({k: v for k, v in q.items() if k not in _EXTRA and v not in ("", None)}, ensure_ascii=False)


def request(original: dict, last: dict | None = None, book: str = "", title: str = "") -> str:
    """Asking for the next version of a question. last: the version shown this time, if it was
    a new one. book: the book it is about, if any; title: its quiz's (or deck's) name."""
    source = f" on \"{book}\"" if book else ""
    named = f" (\"{title}\")" if title else ""
    intro = f"I just reviewed this question{named} from my spaced-repetition cards{source}."
    parts = [
        intro + " When it comes back I want a fresh version of the problem rather than the same one, "
        "so I practice the method instead of remembering the answer.",
        f"<question>\n{_problem(original)}\n</question>",
    ]
    if last:
        parts.append(f"<last_version>\n{_problem(last)}\n</last_version>")
    parts.append(
        "Write a new version: the same type, idea, method and difficulty, with different given values "
        "(and, if it helps, a different setting), so its answer differs from the original's"
        + (" and the last version's" if last else "") + ". Work the solution out step by step and check "
        "it before writing the question. For \"numeric\", \"answer\" is the exact value as a number or a "
        "short expression like \"3/8\" or \"sqrt(2)/2\", with \"unit\" and \"tolerance\" if the original "
        "has them (rescaled to the new answer); for \"free\", \"answer\" is a model answer and \"rubric\" "
        "what a full-credit answer must contain. \"explanation\" briefly shows the worked solution.\n\n"
        "If the question has no givens that could change (it asks about a concept, a definition or a "
        "fixed fact rather than posing a calculation or worked problem), the JSON is {\"skip\": true}.\n\n"
        "End your reply with the JSON object in a ```json fence: {\"type\", \"question\", \"answer\", "
        "\"explanation\", ...} in the quiz format. Double every LaTeX backslash (\"$\\\\frac{a}{b}$\")."
    )
    return "\n\n".join(parts)


def parse(text: str, original: dict) -> dict:
    """The new version from Claude's reply (SKIP if it declined). ValueError if unusable."""
    fences = re.findall(r"```(?:json)?[ \t]*\n(.*?)\n?```", text, re.DOTALL)
    raw = fences[-1] if fences else text[text.find("{"):text.rfind("}") + 1]
    data = quiz.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if data.get("skip") is True:
        return dict(SKIP)
    q = quiz.validate_question(data)
    if q["type"] != original.get("type"):
        raise ValueError(f"a {q['type']} question in place of a {original.get('type')} one")
    q["page"] = original.get("page")
    return q
