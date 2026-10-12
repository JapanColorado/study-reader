"""Anki-style card search for the card browser: a query like `deck:Physics tag:exam -is:new`
compiled to SQL over cards (c), their notes (n), decks (d) and books (b). Qt-free.

Terms are ANDed unless joined by OR; parentheses group, and `-` negates a term or group.
Bare words search the notes' text (`*` any run, `_` one character). See compile() for keys."""

from __future__ import annotations

import re
import time
from functools import lru_cache

from . import cardmodel, quiz, srs

FROM = ("cards c JOIN card_notes n ON n.id=c.note_id JOIN decks d ON d.id=c.deck_id "
        "LEFT JOIN books b ON b.id=c.book_id")

# Every question type a card can have: quiz.py's, and those of cloze and occlusion notes.
TYPE_LABELS = {"flashcard": quiz.TYPE_LABELS["flashcard"], **quiz.TYPE_LABELS, "cloze": "Cloze",
               "occlusion": "Image occlusion"}
QUESTION_TYPES = tuple(TYPE_LABELS)
KEYS = ("deck", "tag", "note", "type", "book", "flag", "is", "cid", "nid", "added", "edited", "rated",
        "introduced", "prop", "re")
PROPS = ("ivl", "due", "reps", "lapses", "s", "d", "r")
PROP = re.compile(r"^(\w+)(<=|>=|!=|=|<|>)(-?\d+(?:\.\d+)?)$")

# Browser columns: the ORDER BY expression for each. `?` stands for now.
SORTS: dict[str, str] = {
    "question": "n.sort_text COLLATE NOCASE",
    "deck": "d.name COLLATE NOCASE",
    "type": "json_extract(c.data, '$.type')",
    "note": "n.kind",
    # Scheduled cards by due date, then new ones in the order they were added.
    "due": "CASE WHEN c.stage=0 THEN 1e12 + c.created_at ELSE c.due END",
    "interval": "CASE WHEN c.stage>0 THEN c.due - c.last_review END",
    "stability": "c.stability",
    "difficulty": "c.difficulty",
    "retrievability": "retrievability(c.stability, c.last_review, ?)",
    "reps": "c.reps",
    "lapses": "c.lapses",
    "added": "c.created_at",
    "modified": "MAX(c.updated_at, n.updated_at)",
    "reviewed": "c.last_review",
    "tags": "n.tags COLLATE NOCASE",
    "book": "b.title COLLATE NOCASE",
}


class SearchError(ValueError):
    """A query that can't be read; the message is shown to the user."""


# -- SQL functions -------------------------------------------------------------------------
@lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern: str, text) -> bool:
    return text is not None and _regex(pattern).search(str(text)) is not None


def register_functions(conn, decay: float) -> None:
    """The SQL functions compiled queries use: REGEXP, and retrievability on FSRS-6's forgetting curve."""
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    conn.create_function("retrievability", 3, lambda s, last, now: srs.recall(s, last, now, decay), deterministic=True)


# -- reading a query -----------------------------------------------------------------------
def _tokens(query: str) -> list[tuple[str, str, bool]]:
    """(kind, text, quoted): kinds '(' ')' 'not' 'or' 'and' 'term'."""
    out: list[tuple[str, str, bool]] = []
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
        elif ch in "()":
            out.append((ch, ch, False))
            i += 1
        elif ch == "-" and i + 1 < n and not query[i + 1].isspace():
            out.append(("not", "-", False))
            i += 1
        else:
            text, quoted = [], False
            while i < n and not query[i].isspace() and query[i] not in "()":
                if query[i] == '"':
                    end = i + 1
                    while end < n and query[end] != '"':
                        end += 2 if query[end] == "\\" and end + 1 < n else 1
                    if end >= n:
                        raise SearchError("A quotation mark isn't closed.")
                    text.append(query[i + 1:end].replace('\\"', '"'))
                    quoted, i = True, end + 1
                elif query[i] == "\\" and i + 1 < n:
                    text.append(query[i:i + 2])
                    i += 2
                else:
                    text.append(query[i])
                    i += 1
            word = "".join(text)
            if not quoted and word.lower() in ("or", "and"):
                out.append((word.lower(), word, False))
            else:
                out.append(("term", word, quoted))
    return out


class _Parser:
    def __init__(self, tokens: list, now: float):
        self.tokens, self.i, self.now = tokens, 0, now
        self.args: list = []

    def peek(self) -> str | None:
        return self.tokens[self.i][0] if self.i < len(self.tokens) else None

    def parse(self) -> str:
        sql = self.expr()
        if self.peek() == ")":
            raise SearchError("A closing parenthesis has no opening one.")
        return sql

    def expr(self) -> str:
        parts = [self.conj()]
        while self.peek() == "or":
            self.i += 1
            parts.append(self.conj())
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def conj(self) -> str:
        parts = [self.unary()]
        while self.peek() not in (None, ")", "or"):
            if self.peek() == "and":
                self.i += 1
            parts.append(self.unary())
        return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"

    def unary(self) -> str:
        kind = self.peek()
        if kind is None:
            raise SearchError("The search ends where a term was expected.")
        if kind == "not":
            self.i += 1
            return f"NOT {self.unary()}"
        if kind == "(":
            self.i += 1
            if self.peek() == ")":
                raise SearchError("Empty parentheses.")
            sql = self.expr()
            if self.peek() != ")":
                raise SearchError("A parenthesis isn't closed.")
            self.i += 1
            return f"({sql})"
        if kind in (")", "or", "and"):
            raise SearchError(f"Unexpected “{self.tokens[self.i][1]}”.")
        _, text, _ = self.tokens[self.i]
        self.i += 1
        return self.term(text)

    # -- terms ---------------------------------------------------------------------------
    def arg(self, value) -> str:
        self.args.append(value)
        return "?"

    def since(self, value: str, key: str) -> float:
        if not value.isdigit() or int(value) < 1:
            raise SearchError(f"{key}: needs a number of days, 1 or more (1 = today).")
        return srs.day_start(self.now) - (int(value) - 1) * 86400

    def term(self, text: str) -> str:
        m = re.match(r"^([A-Za-z_]+):(.*)$", text, re.DOTALL)
        if not m:
            return f"n.search_text LIKE {self.arg('%' + _like(text) + '%')} ESCAPE '\\'"
        key, value = m.group(1).lower(), m.group(2)
        if key not in KEYS:
            raise SearchError(f"Unknown search key “{m.group(1)}:”.")
        return getattr(self, f"_{key}")(value)

    def _deck(self, value: str) -> str:
        pattern = _like(value) or "%"
        return (f"(d.name LIKE {self.arg(pattern)} ESCAPE '\\' OR d.name LIKE {self.arg(pattern + '::%')} "
                "ESCAPE '\\')")

    def _tag(self, value: str) -> str:
        if value.lower() == "none":
            return "n.tags=''"
        if not value:
            raise SearchError("tag: needs a tag (or tag:none).")
        body, i = [], 0
        while i < len(value):
            if value[i] == "\\" and i + 1 < len(value):
                body.append(re.escape(value[i + 1]))
                i += 2
                continue
            body.append(r"\S*" if value[i] == "*" else re.escape(value[i]))
            i += 1
        pattern = " " + "".join(body) + r"(::\S*)? "
        return f"n.tags REGEXP {self.arg(pattern)}"

    def _note(self, value: str) -> str:
        if value.lower() not in cardmodel.KINDS:
            raise SearchError(f"note: is one of {', '.join(cardmodel.KINDS)}.")
        return f"n.kind={self.arg(value.lower())}"

    def _type(self, value: str) -> str:
        if value.lower() not in QUESTION_TYPES:
            raise SearchError(f"type: is one of {', '.join(QUESTION_TYPES)}.")
        return f"json_extract(c.data, '$.type')={self.arg(value.lower())}"

    def _book(self, value: str) -> str:
        if value.lower() == "none":
            return "c.book_id IS NULL"
        if value.lower() == "any":
            return "c.book_id IS NOT NULL"
        return f"COALESCE(b.title, '') LIKE {self.arg('%' + _like(value) + '%')} ESCAPE '\\'"

    def _flag(self, value: str) -> str:
        if not value.isdigit() or int(value) > 7:
            raise SearchError("flag: is a number from 0 (none) to 7.")
        return f"c.flag={self.arg(int(value))}"

    def _is(self, value: str) -> str:
        value = value.lower()
        if value == "new":
            return "c.stage=0"
        if value == "learn":
            return "c.stage IN (1,3)"
        if value == "review":
            return "c.stage IN (2,3)"
        if value == "suspended":
            return "c.suspended=1"
        if value == "due":
            self.args += [srs.next_day(self.now), self.now]
            return f"({srs.due_sql('c.')})"
        raise SearchError("is: is one of new, learn, review, due, suspended.")

    def _ids(self, value: str, key: str, column: str) -> str:
        try:
            ids = [int(x) for x in value.split(",") if x.strip()]
        except ValueError:
            raise SearchError(f"{key}: takes ids separated by commas.") from None
        if not ids:
            raise SearchError(f"{key}: takes ids separated by commas.")
        return f"{column} IN ({', '.join(self.arg(i) for i in ids)})"

    def _cid(self, value: str) -> str:
        return self._ids(value, "cid", "c.id")

    def _nid(self, value: str) -> str:
        return self._ids(value, "nid", "c.note_id")

    def _added(self, value: str) -> str:
        return f"c.created_at>={self.arg(self.since(value, 'added'))}"

    def _edited(self, value: str) -> str:
        return f"n.updated_at>={self.arg(self.since(value, 'edited'))}"

    def _rated(self, value: str) -> str:
        days, _, rating = value.partition(":")
        sql = f"EXISTS (SELECT 1 FROM reviews r WHERE r.card_id=c.id AND r.reviewed_at>={self.arg(self.since(days, 'rated'))}"
        if rating:
            if rating not in ("1", "2", "3", "4"):
                raise SearchError("rated:days:rating takes a rating from 1 (again) to 4 (easy).")
            sql += f" AND r.rating={self.arg(int(rating))}"
        return sql + ")"

    def _introduced(self, value: str) -> str:
        return (f"(SELECT MIN(r.reviewed_at) FROM reviews r WHERE r.card_id=c.id)>="
                f"{self.arg(self.since(value, 'introduced'))}")

    def _prop(self, value: str) -> str:
        m = PROP.match(value.replace(" ", ""))
        if not m or m.group(1).lower() not in PROPS:
            raise SearchError(f"prop: is like prop:ivl>=10, with one of {', '.join(PROPS)}.")
        prop, op, number = m.group(1).lower(), m.group(2), float(m.group(3))
        if prop == "due":
            return self._due_days(op, number)
        if prop == "r":
            expr = f"retrievability(c.stability, c.last_review, {self.arg(self.now)})"
        else:
            expr = {
                "ivl": "ROUND((c.due - c.last_review) / 86400.0)",
                "reps": "c.reps",
                "lapses": "c.lapses",
                "s": "c.stability",
                "d": "(c.difficulty - 1) / 9.0",
            }[prop]
        sql = f"{expr}{'<>' if op == '!=' else op}{self.arg(number)}"
        return f"(c.stage>0 AND {sql})" if prop == "ivl" else sql

    def _due_days(self, op: str, number: float) -> str:
        """Due in whole days from today's start (0 today, -1 yesterday), scheduled cards only."""
        start = srs.day_start(self.now)
        lo, hi = number, number + 1

        def at_least(v: float) -> str:
            return f"c.due>={self.arg(start + v * 86400)}"

        def below(v: float) -> str:
            return f"c.due<{self.arg(start + v * 86400)}"

        if op == "=":
            cond = f"({at_least(lo)} AND {below(hi)})"
        elif op == "!=":
            cond = f"NOT ({at_least(lo)} AND {below(hi)})"
        elif op == "<":
            cond = below(lo)
        elif op == "<=":
            cond = below(hi)
        elif op == ">":
            cond = at_least(hi)
        else:
            cond = at_least(lo)
        return f"(c.stage>0 AND {cond})"

    def _re(self, value: str) -> str:
        try:
            _regex(value)
        except re.error as e:
            raise SearchError(f"re: the regular expression doesn't read: {e}.") from None
        return f"n.search_text REGEXP {self.arg(value)}"


def _like(value: str) -> str:
    """An Anki pattern as a LIKE pattern (ESCAPE '\\'): * any run, _ one character."""
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\\" + nxt if nxt in "_%\\" else nxt)
            i += 2
            continue
        out.append({"*": "%", "%": "\\%", "\\": "\\\\"}.get(ch, ch))
        i += 1
    return "".join(out)


def compile(query: str, now: float | None = None) -> tuple[str, list]:
    """A query as a SQL condition over c, n, d, b (see FROM) and its arguments."""
    now = time.time() if now is None else now
    tokens = _tokens(query or "")
    if not tokens:
        return "1", []
    parser = _Parser(tokens, now)
    return parser.parse(), parser.args


def card_ids(db, query: str, sort: str = "added", descending: bool = False, now: float | None = None) -> list[int]:
    """The ids of the cards a query finds, sorted by a browser column (SORTS)."""
    now = time.time() if now is None else now
    where, args = compile(query, now)
    register_functions(db.conn, srs.decay(db))
    order = SORTS.get(sort, SORTS["added"])
    order_args = [now] * order.count("?")
    direction = "DESC" if descending else "ASC"
    return [r[0] for r in db.conn.execute(
        f"SELECT c.id FROM {FROM} WHERE {where} ORDER BY {order} {direction}, c.id {direction}",
        [*args, *order_args],
    )]
