"""Finding text in a book (Ctrl+F), Qt-free.

Each page's words (``Document.search_words``) are joined into one searchable string,
folded so that case, accents, ligatures and curly quotes don't matter, with words hyphenated
across lines joined again (as ``words_text`` does). Every folded character remembers the word and
character it came from, so a match turns back into rects on the page: whole lines of words, cut
at the match's ends in proportion to the characters (word boxes are all PyMuPDF gives).

A whole-book search takes seconds on a long book, so ``BookSearch`` scans a few pages per
``step`` from the page being read to the end, then from the start, and the GUI calls it a turn
of the event loop at a time. ``SearchIndex`` keeps the pages it has folded, so a second search,
or the next keystroke's, runs much faster.
"""

from __future__ import annotations

import bisect
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .document import Document, Rect, Word

MAX_MATCHES = 5000  # a search stops here ("5000+")
CONTEXT_CHARS = 60  # of text shown on each side of a match in the results list
STEP_SECONDS = 0.025  # how long one step may scan for

# Typographic variants typed as their plain forms.
_PLAIN = {
    "‘": "'", "’": "'", "‚": "'", "′": "'", "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "­": "",
    " ": " ", " ": " ", " ": " ",
}


@lru_cache(maxsize=4096)
def fold_char(c: str) -> str:
    """What a character is searched as: lower case, without accents, ligatures spelled out."""
    if c.isascii():
        return c.lower()
    c = _PLAIN.get(c, c)
    d = unicodedata.normalize("NFKD", c)
    return "".join(ch for ch in d if not unicodedata.combining(ch)).casefold()


def fold(text: str) -> str:
    """A query, folded like the pages, with runs of whitespace made single spaces."""
    return " ".join("".join(fold_char(c) for c in text).split())


@dataclass(frozen=True)
class PageText:
    text: str  # folded, words separated by single spaces (none where a hyphenated word was joined)
    starts: tuple[int, ...]  # where each word starts in ``text``
    ends: tuple[int, ...]  # and ends (a joined word's hyphen is left out)
    maps: tuple[tuple[int, ...] | None, ...]  # per word, its character for each folded one (None: the same)
    joined: tuple[bool, ...]  # the word continues the previous one, hyphenated across lines
    words: tuple[Word, ...]


def _fold_word(text: str) -> tuple[str, tuple[int, ...] | None]:
    if text.isascii():
        return text.lower(), None
    out, chars = [], []
    for k, c in enumerate(text):
        f = fold_char(c)
        out.append(f)
        chars += [k] * len(f)
    folded = "".join(out)
    folded = "".join(" " if ch.isspace() else ch for ch in folded)
    return folded, (tuple(chars) if len(folded) != len(text) else None)


def page_text(words: list[Word]) -> PageText:
    out: list[str] = []
    starts, ends, maps, joined = [], [], [], []
    pos = 0
    prev: Word | None = None
    for w in words:
        join = False
        if prev is not None:
            if w.block == prev.block and w.line != prev.line and prev.text.endswith("-") and len(prev.text) > 1 \
                    and out[-1].endswith("-"):
                # Hyphenated across lines: "contin-" + "uous" is searched as "continuous".
                out[-1] = out[-1][:-1]
                ends[-1] -= 1
                pos -= 1
                join = True
            else:
                out.append(" ")
                pos += 1
        folded, chars = _fold_word(w.text)
        starts.append(pos)
        out.append(folded)
        pos += len(folded)
        ends.append(pos)
        maps.append(chars)
        joined.append(join)
        prev = w
    return PageText("".join(out), tuple(starts), tuple(ends), tuple(maps), tuple(joined), tuple(words))


def _spans(pt: PageText, start: int, end: int):
    """(word index, first char, last char) of the words a range of the folded text covers."""
    i = max(bisect.bisect_right(pt.starts, start) - 1, 0)
    while i < len(pt.words) and pt.starts[i] < end:
        a, b = max(start, pt.starts[i]) - pt.starts[i], min(end, pt.ends[i]) - pt.starts[i] - 1
        if b >= a:
            m = pt.maps[i]
            if m:
                # A character folded into several (ß, ligatures) belongs to the range that starts it.
                first = m[a] + (a > 0 and m[a - 1] == m[a])
                if first <= m[b]:
                    yield i, first, m[b]
            else:
                yield i, a, b
        i += 1


@dataclass(frozen=True)
class Match:
    page: int
    start: int  # in the page's folded text
    end: int
    rects: tuple[Rect, ...]  # in page points, one per line
    context: tuple[str, str, str]  # (before, the match, after), as printed

    @property
    def key(self) -> tuple[int, int]:
        return self.page, self.start


def match_rects(pt: PageText, start: int, end: int) -> list[Rect]:
    """The match's characters as one rect per line, partial words cut in proportion."""
    rects: list[list[float]] = []
    key = None
    for wi, a, b in _spans(pt, start, end):
        w = pt.words[wi]
        n = max(len(w.text), 1)
        x0 = w.x0 + (w.x1 - w.x0) * a / n
        x1 = w.x0 + (w.x1 - w.x0) * (b + 1) / n
        if (w.block, w.line) == key:
            r = rects[-1]
            r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], w.y0), max(r[2], x1), max(r[3], w.y1)
        else:
            rects.append([x0, w.y0, x1, w.y1])
            key = (w.block, w.line)
    return [tuple(r) for r in rects]


def _printed(pt: PageText, start: int, end: int) -> str:
    """The page's own text for a range of its folded text (the words, as printed)."""
    out: list[str] = []
    last_end = start
    for wi, a, b in _spans(pt, start, end):
        if pt.starts[wi] > last_end or (out and not pt.joined[wi]):
            out.append(" ")
        out.append(pt.words[wi].text[a:b + 1])
        last_end = pt.ends[wi]
    if end > last_end and out:
        out.append(" ")
    return "".join(out)


def context(pt: PageText, start: int, end: int, chars: int = CONTEXT_CHARS) -> tuple[str, str, str]:
    a, b = max(start - chars, 0), min(end + chars, len(pt.text))
    # Start and end on whole words where there is room.
    if a > 0:
        space = pt.text.find(" ", a, start)
        a = space + 1 if space >= 0 else a
    if b < len(pt.text):
        space = pt.text.rfind(" ", end, b)
        b = space if space >= 0 else b
    before, hit, after = _printed(pt, a, start), _printed(pt, start, end), _printed(pt, end, b)
    return ("…" if a > 0 else "") + before.lstrip(), hit, after.rstrip() + ("…" if b < len(pt.text) else "")


def find_in_page(pt: PageText, page: int, query: str) -> list[Match]:
    """Every (non-overlapping) match of a folded query on a page."""
    out = []
    i = pt.text.find(query) if query else -1
    while i >= 0:
        j = i + len(query)
        out.append(Match(page, i, j, tuple(match_rects(pt, i, j)), context(pt, i, j)))
        i = pt.text.find(query, j)
    return out


class SearchIndex:
    """A document's pages, folded for searching as they are first needed."""

    def __init__(self, doc: Document):
        self.doc = doc
        self._pages: dict[int, PageText] = {}

    def page(self, index: int) -> PageText:
        if index not in self._pages:
            self._pages[index] = page_text(self.doc.search_words(index))
        return self._pages[index]


class BookSearch:
    """One query, scanned a few pages at a time from ``start_page`` on, then from the first page.
    ``matches`` stays in page order; ``step`` says where the new ones went."""

    def __init__(self, index: SearchIndex, query: str, start_page: int = 0, limit: int = MAX_MATCHES):
        self.index = index
        self.query = fold(query)
        self.limit = limit
        n = index.doc.page_count
        start_page = min(max(start_page, 0), max(n - 1, 0))
        self._order = list(range(start_page, n)) + list(range(0, start_page))
        self._wrap = n - start_page  # where the scan goes back to the first page
        self._next = 0
        self.matches: list[Match] = []
        self._keys: list[tuple[int, int]] = []
        self.truncated = False  # stopped at the limit

    @property
    def done(self) -> bool:
        return not self.query or self.truncated or self._next >= len(self._order)

    @property
    def pages_done(self) -> int:
        return self._next

    @property
    def page_count(self) -> int:
        return len(self._order)

    def step(self, seconds: float = STEP_SECONDS) -> tuple[int, list[Match]]:
        """Scan for about ``seconds``: (where in ``matches`` the new ones start, the new ones).
        A step never crosses the wrap to the first page, so its matches are always together."""
        found: list[Match] = []
        until = time.monotonic() + seconds
        stop = self._wrap if self._next < self._wrap else len(self._order)
        while not self.done and self._next < stop:
            page = self._order[self._next]
            self._next += 1
            found += find_in_page(self.index.page(page), page, self.query)
            if len(self.matches) + len(found) >= self.limit:
                found = found[: self.limit - len(self.matches)]
                self.truncated = True
            if time.monotonic() >= until:
                break
        if not found:
            return len(self.matches), []
        at = bisect.bisect_left(self._keys, found[0].key)
        self.matches[at:at] = found
        self._keys[at:at] = [m.key for m in found]
        return at, found

    def run(self) -> list[Match]:
        """The whole search at once (tests, small books)."""
        while not self.done:
            self.step(1.0)
        return self.matches

    def position(self, m: Match) -> int:
        """Where a match is in ``matches``."""
        return bisect.bisect_left(self._keys, m.key)

    def after(self, page: int) -> int | None:
        """The first match on or after a page (wrapping), as a position in ``matches``."""
        if not self.matches:
            return None
        at = bisect.bisect_left(self._keys, (page, -1))
        return at if at < len(self.matches) else 0


def section_titles(toc: list[tuple[int, str, int]]):
    """A function from a page to the title of the contents entry it falls in ("" before any)."""
    entries = sorted(((page, k), title) for k, (_, title, page) in enumerate(toc))
    starts = [e[0][0] for e in entries]

    def title(page: int) -> str:
        i = bisect.bisect_right(starts, page) - 1
        return entries[i][1] if i >= 0 else ""

    return title
