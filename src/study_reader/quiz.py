"""Quizzes Claude writes into replies as ```quiz <title> fences of JSON: parsing them, grading
answers (locally, except written answers, which Claude grades, and flashcards, which the user
rates), and describing the answers back to Claude on later questions. Every question also
becomes a spaced-repetition card (srs.py). Qt-free."""

from __future__ import annotations

import json
import math
import re

from .calc_latex import LatexError, expr_to_latex, latex_to_expr
from .calculator import CalcError, Calculator
from .cardmodel import clean_tag

# A question's "type" as Claude may write it -> its card type (cardmodel.TYPES). "free" is what
# written answers were once called, still in older replies (and Claude's habits).
TYPE_NAMES = {
    "choice": "choice", "multiple_choice": "choice", "mc": "choice",
    "truefalse": "truefalse", "true_false": "truefalse", "tf": "truefalse", "boolean": "truefalse",
    "numeric": "numeric", "number": "numeric", "math": "numeric",
    "written": "written", "free": "written", "free_response": "written", "short_answer": "written", "open": "written",
    "flashcard": "flashcard", "card": "flashcard", "basic": "flashcard",
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
        kind = TYPE_NAMES.get(str(q.get("type", "")).lower().replace("-", "_").replace(" ", "_"))
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
        if kind in ("numeric", "written", "choice") and isinstance(q.get("varies"), bool):
            out["varies"] = q["varies"]  # whether review can pose it anew: new numbers or choices (variants.py)
        if isinstance(q.get("section"), str) and q["section"].strip():
            out["section"] = q["section"].strip()  # the book section it practices (srs.find_section)
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
    for key in ("page", "tags", "revises", "section"):
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
        return f"Quiz me on the whole document ({pages})", "Quiz: whole document"
    if title:
        return f"Quiz me on the section “{title}” ({pages})", f"Quiz: {title}"
    return f"Quiz me on {pages}", f"Quiz: {pages}"


def snippet_request(snippet: dict) -> tuple[str, str]:
    """(question, chat title) for a quiz on a selected passage or screenshot."""
    if snippet["kind"] == "image":
        return "Quiz me on this screenshot", f"Quiz: screenshot, p. {snippet['page'] + 1}"
    return "Quiz me on this passage", "Quiz: " + " ".join(snippet["text"].split())


def reply_request(reply_text: str, last: bool) -> str:
    """The question asking for a quiz on one of Claude's replies in the chat."""
    if last:
        return "Quiz me on your last reply"
    start = " ".join(split(reply_text)[0].split())
    start = start if len(start) <= 80 else start[:80].rsplit(" ", 1)[0] + "…"
    return f"Quiz me on your earlier reply that begins “{start}”"


def drafted_request(request: str, typed: str) -> str:
    """The question a Quiz me request drafted in the composer sends: what the user left there
    if it still holds the request (perhaps with their own instructions added), else the request
    followed by whatever they wrote instead of it."""
    typed = typed.strip()
    if not typed:
        return request
    if " ".join(request.split()) in " ".join(typed.split()):
        return typed
    return f"{request}\n\n{typed}"


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
    raise ValueError("written answers are graded by Claude")


def grading_prompt(quiz: dict, index: int, response_text: str) -> str:
    intro = f"Grade my answer to question {index + 1} of the quiz \"{quiz['title']}\" in your earlier reply."
    return _grading_prompt(intro, quiz["questions"][index], response_text)


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


# -- review cards and stopped replies --------------------------------------------------
def card_context(q: dict, where: str, answer: dict | None) -> str:
    """The review card on display beside a question asked on the Review page. q: the question as
    review shows it (cardmodel.card_data, or its new version); where: what it is from (a quiz's
    title, a deck); answer: what review holds for it once answered or shown, else None, and then
    Claude is asked not to give it away."""
    kind = q.get("type", "")
    label = {"cloze": "cloze deletion", "occlusion": "image occlusion", "truefalse": "true/false",
             "choice": "multiple-choice"}.get(kind, kind)
    lines = [f"Card ({label}){f', from {where}' if where else ''}:"]
    prompt = str(q.get("question") or "")
    if kind == "occlusion":
        lines.append(f"An image with labels hidden (shape {q.get('active')} is asked)"
                     + (f", headed: {prompt}" if prompt.strip() else "") + ".")
    else:
        lines.append(prompt)
    choices = q.get("choices", []) if kind == "choice" else []
    lines += [f"- {c}" for c in choices]  # unlettered: review shows them in a new order each time
    if answer is None:
        lines.append(
            "\nI haven't answered it yet. Don't give the answer away: help me get there myself (a hint, "
            "a related idea, a question back) unless I ask you outright for the answer."
        )
        return "The review card I'm looking at:\n<review_card>\n" + "\n".join(lines) + "\n</review_card>"
    if kind == "choice":
        i = q.get("answer")
        right = choices[i] if isinstance(i, int) and 0 <= i < len(choices) else "?"
    elif kind == "truefalse":
        right = "true" if q.get("answer") else "false"
    elif kind == "numeric":
        right = f"{q.get('answer')}{(' ' + q['unit']) if q.get('unit') else ''}"
    elif kind == "occlusion":
        right = "(shown on the image)"
    else:
        right = str(q.get("answer") or "")
    lines.append(f"\nAnswer: {right}")
    for key, name in (("rubric", "Rubric"), ("explanation", "Explanation")):
        if str(q.get(key) or "").strip():
            lines.append(f"{name}: {q[key]}")
    response = answer.get("response") or {}
    mine = ""
    if kind == "choice" and isinstance(response.get("choice"), int) and 0 <= response["choice"] < len(choices):
        mine = f"chose: {choices[response['choice']]}"
    elif kind in ("truefalse", "numeric") and response:
        mine = describe_response(q, response)
    elif str(response.get("text") or "").strip():
        mine = "wrote:\n<answer>\n" + str(response["text"]) + "\n</answer>"
    if mine:
        verdict = f" ({answer['verdict']})" if answer.get("verdict") else ""
        lines.append(f"\nI {mine}{verdict}")
    else:
        lines.append("\nI've seen the answer.")
    return "The review card I'm looking at:\n<review_card>\n" + "\n".join(lines) + "\n</review_card>"


def stopped_context(question: str, reply: str) -> str:
    """For the question after a reply the user stopped: what they asked and how much of the reply
    they saw (the session may hold neither)."""
    seen = (f"I saw only this much of it:\n<stopped_reply>\n{reply}\n</stopped_reply>" if reply.strip()
            else "I stopped it before any of it arrived.")
    return f"I stopped your reply to my previous message partway. That message was:\n<message>\n{question}\n</message>\n{seen}"
