"""Read-aloud narration written by Claude, a page at a time (Qt-free).

speech_pages.BookText reads a page as PyMuPDF extracts it: fine for plain prose, but columns
and boxes can come out of order, a figure's labels are read as text, and equations are said
glyph by glyph. Here a small model (``MODEL``) is shown a picture of the page with its
extracted lines, numbered (``_listing``; running headers and page numbers are left out
first, as only neighbouring pages give those away), and writes what should be said: one item
per paragraph, heading, displayed equation or caption, in reading order, each naming the
lines it covers (``SYSTEM_PROMPT`` is the format; ``parse`` reads a reply).

The lines an item names are what light up while it is said. Its words are matched to
theirs (``_align``), so words read as printed are followed word by word, and words Claude
wrote (an equation in words) light up whatever lies between the matches around them.

``NarratedText`` turns the replies into ``Script``s, a page's sentences in spoken order,
and yields them from a place in the book; an ``Utterance.seq`` is its (page, index) there.
Narration isn't in page-word order, so positions are these, not BookText's word indices.
A page nobody has narrated yet is asked for through ``request`` and the iterators yield
``WAIT`` until it lands (``accept``) or fails (``fail``: that page is read as BookText
would). A sentence a page break cuts in two is marked by Claude on both sides (``CARRY``)
and said as one (``_join``). Replies are kept by the caller (the ``narrations`` table)
under ``_Listing.key``, a digest of what was sent, so a change to the extracted lines or
to ``VERSION`` asks again.
"""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from .document import Rect, _line_text, line_rects
from .speech_pages import LONG_PAUSE, PAUSE, BookText, _inside
from .speech_text import _END, Utterance, sentence_ends, speak_word

VERSION = 1  # of the prompt and format: replies stored under another are asked for again
MODEL = "haiku"
TIMEOUT = 120  # seconds a page may take before it is read as extracted
AHEAD = 2  # pages asked for beyond the one being read
ENABLED_SETTING = "tts_narration"
ENABLED_DEFAULT = "1"
IMAGE_SIDE = 1300  # pixels along the page picture's longer side
CONTEXT_WORDS = 50  # of the previous page's end, sent so a carried-over sentence is recognized
CARRY = ">>"
WIDE_GAP = 12  # skipped page words beyond which rewritten words don't light them all up

WAIT = Utterance("")  # yielded while the next page's narration is on its way

SYSTEM_PROMPT = f"""You prepare one page of a book to be read aloud by a text-to-speech voice.

You get a picture of the page and the text extracted from it as numbered lines, each with where \
it sits: "12 @8,40| text" is line 12, whose left edge is 8% of the way across the page and whose \
top is 40% of the way down. The lines are numbered in the order the file stores them, which is \
often not the order they are read in; their equations are often garbled; and they include things \
nobody would read aloud. Use the picture and the positions to see the layout, and the picture to \
read equations as printed.

Begin your reply with one line that starts "layout:" and says how the page is laid out and \
which lines each part holds, in the order the parts are read: for example "layout: two columns; \
left column lines 31-58, right column lines 1-30; figure with labels lines 59-70". Work it out \
from the positions: lines whose left edges are near each other belong to the same column.

Then give the narration, one item per line of your reply, each in the form

<source lines>| <text to speak>

where <source lines> are the numbers of the extracted lines the item covers, as ranges and lists \
("3-7", "3-5,9"). Make one item for each paragraph, heading, displayed equation, list entry or \
caption, in the order your layout line gives. Nothing else: no comments. If nothing on the page \
should be read, reply with just: none

Rules:
- Order the items as a person reading the page aloud would, whatever the line numbers say: on a \
page with columns, the whole left column from top to bottom, then the next column; a box, sidebar \
or margin note after the paragraph it sits beside, never in the middle of a sentence. A sentence \
that runs from the bottom of one column to the top of the next is one item naming both sets of lines.
- Body text and headings are read word for word, with their own punctuation, and with numbers, \
dates and amounts left in digits exactly as printed ("4.12", "1997", "10%", "$4.23"). Do not paraphrase, \
summarize, correct, explain or add anything. Only repair extraction damage: join words hyphenated \
at line ends, restore broken ligatures and spacing.
- Leave out: running headers and footers, page numbers, footnotes and their markers in the \
text, text inside figures (axis labels, tick values, legends, labels in diagrams and plots), \
tables, code listings, equation numbers such as "(2.3)", and bracketed citation numbers such as "[12]".
- Read figure and table captions, each as its own item, placed after the paragraph that ends \
nearest to it.
- Say mathematics in words, as a lecturer would say it, inline formulas included: "x squared plus \
y squared equals 1", "P of X equals k", "X is distributed Binomial n, p", "q equals 1 minus p", \
"n over 2", "the integral from 0 to 1 of f of x, d x", "the sum over i from 1 to n of a sub i". \
No formula keeps a symbol such as = + - / ~ < > ^ _ or the parentheses of a function's argument. Name \
Greek letters and symbols; say a subscript or label as "S sub L R"; spell out an abbreviation \
made of capitals as the text has it ("LRMC"). Inline math stays in its sentence; a displayed equation is its own \
item unless it finishes the sentence before it, in which case keep them together.
- Write out abbreviations a voice would stumble on: "e.g." as "for example", "Fig. 2" as \
"Figure 2", "§3" as "Section 3", a URL as the site's name.
- Plain text only: no Markdown, no LaTeX, no symbols that have to be read as words.
- The page may begin in the middle of a sentence carried over from the previous page (whose last \
words may be given in <previous_page_end>, for context only: never narrate them). If it does, \
start that item's text with "{CARRY} ". If the very end of the page's body text (the bottom of its \
last column) stops in the middle of a sentence that carries on to the next page, end that item's \
text with " {CARRY}", leaving the fragment exactly as it stands (keep a hyphen at the very end of a \
divided word). At most one item starts with "{CARRY}" and at most one ends with it."""

_ITEM = re.compile(r"^\s*\[?\s*(\d[\d\s,\-–]*?)\s*\]?\s*\|\s*(.*\S)\s*$")
_MARKUP = re.compile(r"\*\*|__|`|^#+\s*")


@dataclass
class Script:
    """One page's narration."""

    items: list[Utterance] = field(default_factory=list)  # sentences in spoken order; seq is (page, index)
    words: list[frozenset[int]] = field(default_factory=list)  # each sentence's page words (indices)
    continues: bool = False  # the first sentence began on the previous page
    carries: bool = False  # the last sentence runs on into the next page


@dataclass
class _Listing:
    text: str  # the numbered lines, as sent
    words: list[list[int]]  # each listed line's words, as indices into Document.words(page)
    key: str

    @property
    def readable(self) -> bool:
        return any(c.isalnum() for c in self.text)


def parse(reply: str, lines: int) -> list[tuple[list[int], str]] | None:
    """(line numbers, text) of each item in a reply; [] for "none"; None if it holds no items."""
    reply = "\n".join(r for r in reply.strip().splitlines() if not r.lstrip().startswith("```")).strip()
    rows = [r for r in reply.splitlines() if not r.lstrip().lower().startswith("layout:")]
    reply = "\n".join(rows).strip()
    if reply.lower().strip(" .") == "none":
        return []
    items: list[tuple[list[int], str]] = []
    for row in reply.splitlines():
        m = _ITEM.match(row)
        if not m:
            if items and row.strip():  # an item's text wrapped onto a new line
                items[-1] = (items[-1][0], f"{items[-1][1]} {row.strip()}")
            continue
        nums: list[int] = []
        for part in re.split(r"[,\s]+", m[1].replace("–", "-")):
            a, _, b = part.partition("-")
            if a.isdigit():
                nums += range(int(a), min(int(b) if b.isdigit() else int(a), lines) + 1)
        items.append(([n for n in dict.fromkeys(nums) if 1 <= n <= lines], m[2]))
    return items or None


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).lower())


def _align(said: list[str], source: list[str]) -> list[tuple[int, int]]:
    """For each word said, the run of source words [lo, hi) it stands for: its match, or for a
    word with none, the unmatched source words between the matches around it."""
    a = [_norm(w) or f"\0a{i}" for i, w in enumerate(said)]
    b = [_norm(w) or f"\0b{i}" for i, w in enumerate(source)]
    match = [-1] * len(a)
    for blk in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for d in range(blk.size):
            match[blk.a + d] = blk.b + d
    after = [len(b)] * (len(a) + 1)  # the next match at or after each word
    for i in range(len(a) - 1, -1, -1):
        after[i] = match[i] if match[i] >= 0 else after[i + 1]
    out, before, i = [], 0, 0
    while i < len(a):
        if match[i] >= 0:
            out.append((match[i], match[i] + 1))
            before = match[i] + 1
            i += 1
            continue
        j = i
        while j < len(a) and match[j] < 0:
            j += 1
        lo, hi = before, after[j]
        if hi - lo > 4 * (j - i) + WIDE_GAP:  # a table or figure was left out here
            lo = hi
        out += [(lo, hi)] * (j - i)
        i = j
    return out


def _join(a: Utterance, b: Utterance) -> Utterance:
    """A sentence a page break cut in two, as one."""
    divided = len(a.text) > 1 and a.text.endswith("-") and a.text[-2].isalpha() and b.text[:1].islower()
    head = a.text[:-1] if divided else a.text + " "
    rects = {p: list(rs) for p, rs in a.rects.items()}
    for p, rs in b.rects.items():
        rects.setdefault(p, []).extend(rs)
    marks = a.marks + [(s + len(head), e + len(head), r) for s, e, r in b.marks]
    return replace(a, text=head + b.text, rects=rects, marks=marks, pause=b.pause)


class NarratedText:
    """A book's narration: Claude's where there is one, BookText's sentences for the pages
    where it failed (and, with no ``request``, for every page not narrated)."""

    def __init__(self, book: BookText, texts: dict[int, tuple[str, str]] | None = None,
                 request: Callable[[int], None] | None = None):
        self.book = book
        self.texts = dict(texts or {})  # page -> (key, reply)
        self.request = request
        self.failed: set[int] = set()
        self._listings: dict[int, _Listing] = {}
        self._scripts: dict[int, Script] = {}

    # -- what is sent -------------------------------------------------------------------
    def _listing(self, page: int) -> _Listing:
        if page not in self._listings:
            doc = self.book.doc
            words, lines = doc.words(page), doc._lines(page)
            width, height = doc.page_size(page)
            furniture = self.book._furniture(page)
            by_block: dict[int, list[int]] = {}
            for k, ln in enumerate(lines):
                by_block.setdefault(ln.block, []).append(k)
            owned: list[list[int]] = [[] for _ in lines]
            for i, w in enumerate(words):
                for k in by_block.get(w.block, ()):
                    if _inside(w, lines[k]):
                        owned[k].append(i)
                        break
            rows, kept = [], []
            for k, ln in enumerate(lines):
                text = " ".join(_line_text(ln).split())
                if not text or (owned[k] and all(i in furniture for i in owned[k])):
                    continue
                kept.append(owned[k])
                rows.append(f"{len(kept)} @{round(100 * ln.x0 / width)},{round(100 * ln.y0 / height)}| {text}")
            text = "\n".join(rows)
            key = hashlib.sha1(f"{VERSION}\n{text}".encode()).hexdigest()[:16]
            self._listings[page] = _Listing(text, kept, key)
        return self._listings[page]

    def key(self, page: int) -> str:
        return self._listing(page).key

    def blocks(self, page: int) -> list[dict]:
        """The request for a page: its picture, the previous page's last words, its lines."""
        doc = self.book.doc
        w, h = doc.page_size(page)
        png = doc.render(page, min(2.0, IMAGE_SIDE / max(w, h))).tobytes("png")
        parts = []
        if page > 0:
            tail = " ".join(t.text for t in self.book._tokens(page - 1)[-CONTEXT_WORDS:])
            if tail:
                parts.append(f"<previous_page_end>\n… {tail}\n</previous_page_end>")
        parts.append(f"<page>\n{self._listing(page).text}\n</page>")
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(png).decode()}},
            {"type": "text", "text": "\n\n".join(parts)},
        ]

    # -- what comes back ----------------------------------------------------------------
    def accept(self, page: int, key: str, reply: str) -> bool:
        """Take a page's reply; False (and the page read as extracted) if it can't be used."""
        listing = self._listing(page)
        if key != listing.key or parse(reply, len(listing.words)) is None:
            self.fail(page)
            return False
        self.texts[page] = (key, reply)
        self._scripts.pop(page, None)
        return True

    def fail(self, page: int) -> None:
        self.failed.add(page)

    def script(self, page: int, ask: bool = True) -> Script | None:
        """The page's sentences; None while its narration is still to come (asked for now)."""
        if page in self._scripts:
            return self._scripts[page]
        listing = self._listing(page)
        script = None
        if not listing.readable:
            script = Script()
        else:
            key, reply = self.texts.get(page, ("", ""))
            items = parse(reply, len(listing.words)) if key == listing.key else None
            if items is not None:
                script = self._script(page, items)
            elif page in self.failed or self.request is None:
                script = self._extracted(page)
        if script is None:
            if ask:
                self.request(page)
            return None
        self._scripts[page] = script
        return script

    def _extracted(self, page: int) -> Script:
        """The page as BookText reads it."""
        n = len(self.book.doc.words(page))
        script = Script()
        for u in self.book.utterances((page, 0), (page, n - 1)) if n else ():
            script.items.append(replace(u, seq=(page, len(script.items))))
            lo = u.first[1] if u.first[0] == page else 0
            hi = u.last[1] if u.last[0] == page else n - 1
            script.words.append(frozenset(range(lo, hi + 1)))
        return script

    def _script(self, page: int, items: list[tuple[list[int], str]]) -> Script:
        words = self.book.doc.words(page)
        listing = self._listing(page)
        script = Script()
        parts = []
        for nums, text in items:
            text = _MARKUP.sub("", text).strip()
            cont, carry = text.startswith(CARRY), text.endswith(CARRY)
            text = text.removeprefix(CARRY).removesuffix(CARRY).strip() if text != CARRY else ""
            if text:
                parts.append((nums, text, cont, carry))
        # The sentence from the previous page comes first, the one going on to the next last.
        parts.sort(key=lambda p: (not p[2], p[3]))
        merged: list[tuple] = []
        for part in parts:
            prev = merged[-1] if merged else None
            # A sentence left unfinished by one item and picked up by the next (a column break).
            if prev and not prev[3] and not _END.search(prev[1]) and prev[1][-1] not in ":;" and part[1][0].islower():
                divided = prev[1].endswith("-") and prev[1][-2:-1].isalpha()
                text = prev[1][:-1] + part[1] if divided else f"{prev[1]} {part[1]}"
                merged[-1] = (prev[0] + part[0], text, prev[2], part[3])
            else:
                merged.append(part)
        parts = merged
        for n, (nums, text, cont, carry) in enumerate(parts):
            src = list(dict.fromkeys(i for k in nums for i in listing.words[k - 1]))
            pairs = [(w, s) for w in text.split() if (s := speak_word(w))]
            if not pairs:
                continue
            said = [s for _, s in pairs]
            spans = _align([w for w, _ in pairs], [words[i].text for i in src])
            start = 0
            first = len(script.items)
            for end in sentence_ends(said):
                self._sentence(script, page, said[start:end], spans[start:end], src, words)
                start = end
            last = script.items[-1]
            carry = carry and n == len(parts) - 1 and not _END.search(last.text)
            if not carry:
                last.pause = LONG_PAUSE
                if last.text[-1] not in ".!?:;…\"'”’)":
                    last.text += "."
            script.carries = carry
            if n == 0 and first == 0:
                script.continues = cont
        return script

    def _sentence(self, script: Script, page: int, said: list[str], spans: list[tuple[int, int]],
                  src: list[int], words: list) -> None:
        marks, pos, shown = [], 0, None
        lo_all, hi_all = len(src), 0
        for k, (word, (lo, hi)) in enumerate(zip(said, spans)):
            if k:
                pos += 1
            if lo < hi:
                if (lo, hi) != shown:
                    refs = [(w.x0, w.y0, w.x1, w.y1) for w in (words[i] for i in src[lo:hi])]
                    marks.append((pos, pos + len(word), {page: refs}))
                    shown = (lo, hi)
                lo_all, hi_all = min(lo_all, lo), max(hi_all, hi)
            pos += len(word)
        covered = sorted(src[lo_all:hi_all] if lo_all < hi_all else src)
        rects: dict[int, list[Rect]] = {page: line_rects([words[i] for i in covered])} if covered else {}
        script.items.append(Utterance(" ".join(said), rects=rects, marks=marks, pause=PAUSE,
                                      seq=(page, len(script.items))))
        script.words.append(frozenset(covered))

    # -- reading ------------------------------------------------------------------------
    def from_view(self, page: int, y: float) -> Iterator[Utterance]:
        """Sentences from the first one with a line at or below ``y`` on ``page``."""
        return self._from(page, lambda s: self._below(s, page, y))

    def from_word(self, page: int, idx: int) -> Iterator[Utterance]:
        """Sentences from the one holding a word of the page (or the next one below it)."""
        def pick(s: Script) -> int | None:
            k = next((k for k, ws in enumerate(s.words) if idx in ws), None)
            return k if k is not None else self._below(s, page, self.book.doc.words(page)[idx].y0)

        return self._from(page, pick)

    def all(self) -> Iterator[Utterance]:
        return self._run(0, 0)

    @staticmethod
    def _below(script: Script, page: int, y: float) -> int | None:
        return next((k for k, u in enumerate(script.items)
                     if any((r[1] + r[3]) / 2 >= y for r in u.rects.get(page, ()))), None)

    def _from(self, page: int, pick: Callable[[Script], int | None]) -> Iterator[Utterance]:
        while (script := self.script(page)) is None:
            yield WAIT
        k = pick(script)
        if k is None:
            page, k = page + 1, 0
        if k == 0 and (prev := self._carried_into(page)):
            page, k = page - 1, len(prev.items) - 1
        yield from self._run(page, k)

    def _carried_into(self, page: int) -> Script | None:
        """The previous page's script, if its last sentence ends on this page and is here already."""
        script = self.script(page, ask=False)
        prev = self.script(page - 1, ask=False) if page > 0 and script and script.items else None
        return prev if prev and prev.carries and prev.items else None

    def _run(self, page: int, k: int) -> Iterator[Utterance]:
        count = self.book.doc.page_count
        while page < count:
            while (script := self.script(page)) is None:
                yield WAIT
            for q in range(page + 1, min(page + 1 + AHEAD, count)):
                self.script(q)  # asked for now, so it is here when reading gets there
            if k >= len(script.items):
                page, k = page + 1, 0
                continue
            u = script.items[k]
            if k == len(script.items) - 1 and script.carries and page + 1 < count:
                while (nxt := self.script(page + 1)) is None:
                    yield WAIT
                if nxt.items:
                    yield _join(u, nxt.items[0])
                    page, k = page + 1, 1
                    continue
            yield u
            k += 1

    def before(self, u: Utterance) -> Utterance | None:
        """The sentence said before ``u``, if its page's narration is here."""
        page, k = u.seq
        if page < 0:
            return None
        if k == 0:
            page -= 1
            while page >= 0 and (script := self.script(page)) is not None and not script.items:
                page -= 1
            if page < 0 or script is None:
                return None
            k = len(script.items)
        script = self.script(page)
        if k - 1 == 0 and (prev := self._carried_into(page)):
            return _join(prev.items[-1], script.items[0])
        v = script.items[k - 1]
        if k == len(script.items) and script.carries:
            nxt = self.script(page + 1, ask=False)
            if nxt and nxt.items:
                return _join(v, nxt.items[0])
        return v
