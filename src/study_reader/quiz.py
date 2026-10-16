"""Quizzes Claude writes into replies as ```quiz <title> fences of JSON: parsing them, grading
answers (locally, except free responses, which Claude grades, and flashcards, which the user
rates), and describing the answers back to Claude on later questions. Every question also
becomes a spaced-repetition card (srs.py). Qt-free."""

from __future__ import annotations

import json
import math
import re

from .calc_latex import LatexError, expr_to_latex, latex_to_expr
from .calculator import CalcError, Calculator
from .cardmodel import clean_tag

TYPES = {
    "choice": "choice", "multiple_choice": "choice", "mc": "choice",
    "truefalse": "truefalse", "true_false": "truefalse", "tf": "truefalse", "boolean": "truefalse",
    "numeric": "numeric", "number": "numeric", "math": "numeric",
    "free": "free", "free_response": "free", "short_answer": "free", "open": "free",
    "flashcard": "flashcard", "card": "flashcard", "basic": "flashcard",
}
TYPE_LABELS = {
    "choice": "Multiple choice", "truefalse": "True or false", "numeric": "Numeric", "free": "Free response",
    "flashcard": "Flashcard",
}
VERDICTS = ("correct", "partial", "incorrect")
# How well a flashcard was recalled, as the user rates it (FSRS's Again / Hard / Good / Easy).
RATINGS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
RATING_VERDICTS = {1: "incorrect", 2: "partial", 3: "correct", 4: "correct"}
# Numeric answers within this fraction of the expected value count, unless the quiz gives a tolerance.
RELATIVE_TOLERANCE = 0.01

# "Quiz me" without a selection covers the contents entry around the page at a chosen depth (a
# contents level; PAGE_LEVEL is just the page, DOCUMENT_LEVEL all of a document of at most
# WHOLE_DOCUMENT_PAGES), DEFAULT_LEVEL until one is picked from the button's menu, or, without
# contents, this many pages either side.
DEFAULT_LEVEL = 2
PAGE_LEVEL = 0
DOCUMENT_LEVEL = -1
WHOLE_DOCUMENT_PAGES = 50
NEARBY_PAGES = 2

# A whole ```quiz fence (or ~~~quiz), closed by the same fence; the title follows the info word.
FENCE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*quiz\b(?P<title>[^\n]*)\n(?P<body>.*?)\n?^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# A backslash JSON would misread: LaTeX like \frac or \theta (valid escapes \f, \t plus letters)
# or \( and \{ (invalid escapes). Only an odd run of backslashes escapes anything.
BAD_ESCAPE = re.compile(r'(?<!\\)((?:\\\\)*)\\(?!u[0-9a-fA-F]{4})(?=[A-Za-z]{2}|[^"\\/bfnrtu])')


class AnswerError(ValueError):
    """An answer that can't be graded as entered (e.g. an unreadable number); nothing is saved."""


def loads(raw: str):
    """JSON as Claude writes it, forgiving LaTeX backslashes that weren't doubled."""
    return json.loads(BAD_ESCAPE.sub(lambda m: m.group(1) + "\\\\", raw))


def split(text: str) -> tuple[str, list[dict]]:
    """Replace each quiz fence with a ```quiz fence holding just `#<index>`, and return the
    quizzes in order: {title, questions} or, when one can't be read, {title, error, raw}."""
    quizzes: list[dict] = []

    def replace(m: re.Match) -> str:
        title = m["title"].strip()
        try:
            quiz = _validate(loads(m["body"]), title)
        except (ValueError, TypeError, KeyError) as e:
            quiz = {"title": title or "Quiz", "error": str(e) or type(e).__name__, "raw": m["body"]}
        quizzes.append(quiz)
        i, f = m["indent"], m["fence"]
        return f"{i}{f}quiz\n{i}#{len(quizzes) - 1}\n{i}{f}"

    return FENCE.sub(replace, text), quizzes


def quizzes(text: str) -> list[dict]:
    return split(text)[1]


def _validate(data, title: str) -> dict:
    if isinstance(data, list):
        data = {"questions": data}
    questions = []
    for q in data["questions"]:
        kind = TYPES.get(str(q.get("type", "")).lower().replace("-", "_").replace(" ", "_"))
        if kind is None:
            raise ValueError(f"unknown question type {q.get('type')!r}")
        if not str(q.get("question", "")).strip():
            raise ValueError("a question has no text")
        out = {
            "type": kind, "question": str(q["question"]), "explanation": str(q.get("explanation") or ""),
            "page": int(q["page"]) if isinstance(q.get("page"), (int, float)) and q["page"] >= 1 else None,
        }
        answer = q.get("answer")
        if kind == "choice":
            choices = [str(c) for c in q.get("choices") or []]
            if len(choices) < 2 or not isinstance(answer, int) or not 0 <= answer < len(choices):
                raise ValueError("a multiple-choice question needs choices and the index of the right one")
            out.update(choices=choices, answer=answer)
        elif kind == "truefalse":
            if isinstance(answer, str) and answer.lower() in ("true", "false"):
                answer = answer.lower() == "true"
            if not isinstance(answer, bool):
                raise ValueError("a true/false question needs a true or false answer")
            out["answer"] = answer
        elif kind == "numeric":
            expected_value(answer)  # raises if unreadable
            out.update(answer=answer, unit=str(q.get("unit") or ""), answer_latex=answer_latex(answer))
            if isinstance(q.get("tolerance"), (int, float)) and q["tolerance"] >= 0:
                out["tolerance"] = float(q["tolerance"])
        elif kind == "flashcard":
            if not _text(answer).strip():
                raise ValueError("a flashcard needs an answer for its back")
            out["answer"] = _text(answer)
        else:
            out.update(answer=_text(answer), rubric=_text(q.get("rubric")))
        raw_tags = q.get("tags")
        tags = [clean_tag(t) for t in (raw_tags.split() if isinstance(raw_tags, str) else raw_tags or [])
                if isinstance(t, str)]
        if any(tags):
            out["tags"] = list(dict.fromkeys(t for t in tags if t))
        if kind in ("numeric", "free") and isinstance(q.get("varies"), bool):
            out["varies"] = q["varies"]  # whether review can pose it with new numbers (variants.py)
        if isinstance(q.get("revises"), int) and not isinstance(q["revises"], bool) and q["revises"] >= 1:
            out["revises"] = q["revises"]  # its 1-based number in the quiz this one revises
        questions.append(out)
    if not questions:
        raise ValueError("the quiz has no questions")
    out = {"title": title or str(data.get("title") or "Quiz"), "questions": questions}
    if isinstance(data.get("revises"), str) and data["revises"].strip():
        out["revises"] = data["revises"].strip()  # the title of the earlier quiz it revises
    return out


def revised_quiz(earlier: list[tuple[int, int, dict]], title: str) -> tuple[int, int] | None:
    """Which earlier quiz a revision replaces: the latest with that title (or, with none, the
    latest). earlier: (message id, index, quiz) in chat order."""
    readable = [(m, i, q) for m, i, q in earlier if "questions" in q]
    key = " ".join(title.split()).lower()
    match = [x for x in readable if " ".join(x[2]["title"].split()).lower() == key] or readable
    return (match[-1][0], match[-1][1]) if match else None


def revise_request(title: str, change: str) -> str:
    """The question asking Claude to revise one of its quizzes."""
    return f"Revise your quiz “{title}”: {change.strip()}"


def validate_question(raw: dict) -> dict:
    """One question as a card holds it (no page). Raises ValueError, TypeError or KeyError if unusable."""
    (q,) = _validate({"questions": [raw]}, "")["questions"]
    for key in ("page", "tags", "revises"):
        q.pop(key, None)
    return q


def scopes(toc: list[tuple[int, str, int]], page: int, page_count: int) -> list[tuple[int, int, int, str]]:
    """The nested contents entries around `page`, outermost first, as (level, first page, last page,
    title), all 0-based. toc: (level, title, first page) as Document.toc() gives it."""
    path: list[int] = []  # indices into toc
    for i, (level, _, start) in enumerate(toc):
        if start > page:
            break
        path = [j for j in path if toc[j][0] < level] + [i]
    out = []
    for i in path:
        level, title, start = toc[i]
        following = next((s for lvl, _, s in toc[i + 1:] if lvl <= level), page_count)
        out.append((level, start, max(following - 1, start), " ".join(title.replace("\0", " ").split())))
    return out


def section_around(
    toc: list[tuple[int, str, int]], page: int, page_count: int, level: int = DEFAULT_LEVEL
) -> tuple[int, int, str | None]:
    """(first page, last page, title or None) of what to quiz on around `page`, all 0-based: the
    deepest entry around it no deeper than `level`."""
    if level == PAGE_LEVEL:
        return page, page, None
    if level == DOCUMENT_LEVEL and page_count <= WHOLE_DOCUMENT_PAGES:
        return 0, page_count - 1, None
    path = scopes(toc, page, page_count)
    if path:
        _, start, end, title = ([s for s in path if s[0] <= level] or path[:1])[-1]
        return start, end, title
    return max(page - NEARBY_PAGES, 0), min(page + NEARBY_PAGES, page_count - 1), None


def pages_label(first: int, last: int) -> str:
    return f"page {first + 1}" if first == last else f"pages {first + 1}–{last + 1}"


def request(first: int, last: int, title: str | None, whole: bool = False) -> tuple[str, str]:
    """(question asking for a quiz on pages first..last, chat title). whole: they are the whole document."""
    pages = pages_label(first, last)
    if whole:
        return f"Quiz me on the whole document ({pages}).", "Quiz: whole document"
    if title:
        return f"Quiz me on the section “{title}” ({pages}).", f"Quiz: {title}"
    return f"Quiz me on {pages}.", f"Quiz: {pages}"


def snippet_request(snippet: dict) -> tuple[str, str]:
    """(question, chat title) for a quiz on a selected passage or screenshot."""
    if snippet["kind"] == "image":
        return "Quiz me on this.", f"Quiz: screenshot, p. {snippet['page'] + 1}"
    return "Quiz me on this passage.", "Quiz: " + " ".join(snippet["text"].split())


def reply_request(reply_text: str, last: bool) -> str:
    """The question asking for a quiz on one of Claude's replies in the chat."""
    if last:
        return "Quiz me on your last reply."
    start = " ".join(split(reply_text)[0].split())
    start = start if len(start) <= 80 else start[:80].rsplit(" ", 1)[0] + "…"
    return f"Quiz me on your earlier reply that begins “{start}”."


def _text(value) -> str:
    """A string field that may come as a list of points."""
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    return str(value or "")


# -- grading ---------------------------------------------------------------------------
def _number(expr: str) -> float:
    return float(Calculator().compute(expr))


def expected_value(answer) -> float:
    if isinstance(answer, bool):
        raise ValueError("a numeric answer must be a number")
    if isinstance(answer, (int, float)):
        return float(answer)
    text = str(answer).strip()
    try:
        return _number(text)
    except CalcError:
        try:
            return _number(latex_to_expr(text))
        except (CalcError, LatexError):
            raise ValueError(f"can't read the numeric answer {text!r}") from None


def answer_latex(answer) -> str:
    if isinstance(answer, (int, float)):
        return f"{answer:g}" if isinstance(answer, float) else str(answer)
    text = str(answer).strip()
    return text if "\\" in text else expr_to_latex(text)


def grade(question: dict, response: dict) -> str:
    """Verdict for a locally graded question. response: {"choice": i} | {"value": bool} | {"latex": s}."""
    kind = question["type"]
    if kind == "choice":
        return "correct" if response.get("choice") == question["answer"] else "incorrect"
    if kind == "truefalse":
        return "correct" if response.get("value") is question["answer"] else "incorrect"
    if kind == "flashcard":
        rating = response.get("rating")
        if rating not in RATING_VERDICTS:
            raise AnswerError("Rate how well you knew it")
        return RATING_VERDICTS[rating]
    if kind == "numeric":
        latex = str(response.get("latex", "")).strip()
        if not latex:
            raise AnswerError("Enter a number")
        try:
            given = _number(latex_to_expr(latex))
        except (CalcError, LatexError) as e:
            raise AnswerError(str(e)) from None
        expected = expected_value(question["answer"])
        tol = question.get("tolerance")
        if tol is None:
            tol = RELATIVE_TOLERANCE * abs(expected)
        return "correct" if math.isclose(given, expected, rel_tol=0, abs_tol=tol + 1e-12) else "incorrect"
    raise ValueError("free responses are graded by Claude")


def grading_prompt(quiz: dict, index: int, response_text: str) -> str:
    intro = f"Grade my answer to question {index + 1} of the quiz \"{quiz['title']}\" in your earlier reply."
    return _grading_prompt(intro, quiz["questions"][index], response_text)


def review_grading_prompt(title: str, index: int | None, question: dict, response_text: str,
                          new_version: bool = False) -> str:
    """Grading a free response given when the question came back in review. index None: the
    question isn't from a quiz in a chat; title is then its deck. new_version: the question is
    a version of it with new numbers (variants.py)."""
    if index is None:
        deck = f" (deck \"{title}\")" if title else ""
        intro = f"I'm reviewing this question from my spaced-repetition deck{deck}. Grade my answer."
    elif new_version:
        intro = (f"I'm reviewing a new version (with different numbers) of question {index + 1} of your quiz "
                 f"\"{title}\", in spaced-repetition review. Grade my answer.")
    else:
        intro = (f"I'm reviewing question {index + 1} of your quiz \"{title}\" again, in spaced-repetition "
                 "review. Grade my answer.")
    return _grading_prompt(intro, question, response_text)


def _grading_prompt(intro: str, q: dict, response_text: str) -> str:
    parts = [intro, f"<question>\n{q['question']}\n</question>"]
    if q.get("answer"):
        parts.append(f"<model_answer>\n{q['answer']}\n</model_answer>")
    if q.get("rubric"):
        parts.append(f"<rubric>\n{q['rubric']}\n</rubric>")
    parts.append(f"<my_answer>\n{response_text}\n</my_answer>")
    parts.append(
        'Reply with only a JSON object, no code fence: {"verdict": "correct" | "partial" | "incorrect", '
        '"feedback": "..."}. The feedback is 1-3 sentences of Markdown (LaTeX allowed, backslashes '
        "escaped for JSON), addressed to me: what I got right, and what was missing or wrong. Judge the "
        "substance: accept correct answers worded differently from the model answer."
    )
    return "\n\n".join(parts)


def parse_grade(text: str) -> tuple[str | None, str]:
    """(verdict or None, feedback) from Claude's grading reply; unreadable replies become feedback."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            data = loads(m.group(0))
            verdict = str(data.get("verdict", "")).lower()
            return (verdict if verdict in VERDICTS else None), str(data.get("feedback") or "")
        except (ValueError, AttributeError):
            pass
    return None, text.strip()


# -- answers ---------------------------------------------------------------------------
def describe_response(question: dict, response: dict) -> str:
    kind = question["type"]
    if kind == "choice":
        i = response.get("choice")
        if isinstance(i, int) and 0 <= i < len(question["choices"]):
            return f"chose {chr(65 + i)} ({question['choices'][i]})"
        return "chose nothing"
    if kind == "truefalse":
        return f"answered {'true' if response.get('value') else 'false'}"
    if kind == "numeric":
        return f"answered ${response.get('latex', '')}$"
    if kind == "flashcard":
        return f"rated my recall {RATINGS.get(response.get('rating'), '?')}"
    return "wrote:\n<answer>\n" + str(response.get("text", "")) + "\n</answer>"


def answers_context(messages: list[dict], answers: dict[int, list[dict]]) -> str:
    """What the user answered in the quizzes of `messages` (assistant replies), for Claude to see
    alongside the next question. answers: message id -> quiz_answers rows."""
    sections = []
    for m in messages:
        rows = answers.get(m["id"])
        if m["role"] != "assistant" or not rows:
            continue
        for qi, quiz in enumerate(quizzes(m["text"])):
            mine = {r["question"]: r for r in rows if r["quiz"] == qi}
            if "questions" not in quiz or not mine:
                continue
            lines = [f"Quiz \"{quiz['title']}\":"]
            for i, q in enumerate(quiz["questions"]):
                r = mine.get(i)
                if r is None:
                    lines.append(f"Q{i + 1}: not answered yet")
                    continue
                result = r["verdict"] or ("being graded" if r["status"] == "pending" else "not graded")
                lines.append(f"Q{i + 1}: I {describe_response(q, json.loads(r['response']))} ({result})")
            sections.append("\n".join(lines))
    if not sections:
        return ""
    return "My answers to your quizzes in this chat so far:\n<quiz_answers>\n" + "\n\n".join(sections) + "\n</quiz_answers>"
