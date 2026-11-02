"""A book's text as sentences to read aloud, each knowing the page boxes it covers.

Running headers, footers and page numbers, footnotes (and their markers in the text) and
figure captions are left out. Web snapshots print each formula's TeX nearly invisibly
right where the formula is (web_import's ``.tex-src``); those runs are spoken as math."""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from .document import BOLD, LABEL, Document, Rect, Word, _label, _line_text, footnote_starts, line_rects
from .speech_text import MAX_CHARS, SOFT_CHARS, Utterance, ends_sentence, speak_word, tex_to_speech

EDGE_BAND = 0.12  # of the page height, where running headers and footers live
PARA_GAP = 0.6  # a gap above a line, in line heights, that starts a paragraph
HEADING_RATIO = 1.15  # font size over the page's body size that makes a heading
TINY_RATIO = 0.25  # web snapshots' TeX source is printed at half a point
PAUSE, LONG_PAUSE = 0.12, 0.35  # after a sentence; after a paragraph or heading
PAGE_NUMBER = re.compile(r"^\W*(page\s*)?(\d+|[ivxlcdm]+)(\s*(of|/)\s*\d+)?\W*$", re.I)
TRAILING_MARK = re.compile(r"(?<=\D)[\d*†‡§¶¹²³⁰-⁹]+$")
# A footnote number printed straight after a word's punctuation: "complements.18".
GLUED_NOTE = re.compile(r"(?<=[^\W\d_][.,;:!?”\"’)])\d{1,3}$")


@dataclass
class _Tok:
    text: str  # as printed (markers stripped, hyphenated halves joined); decides sentence ends
    spoken: str
    first: tuple[int, int]  # (page, word index)
    last: tuple[int, int]
    words: list[tuple[int, Word]] = field(default_factory=list)
    extra: dict[int, list[Rect]] = field(default_factory=dict)  # boxes of formulas
    brk: bool = False  # a paragraph starts here
    heading: bool = False
    line_start: bool = False
    alone: bool = False  # display math: a sentence of its own

    def refs(self) -> dict[int, list[Rect]]:
        out: dict[int, list[Rect]] = {}
        for p, w in self.words:
            out.setdefault(p, []).append((w.x0, w.y0, w.x1, w.y1))
        for p, rs in self.extra.items():
            out.setdefault(p, []).extend(rs)
        return out


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\b[ivxlcdm]+\b", "#", text)
    return " ".join(re.sub(r"[^\w#]+", " ", text).split())


def _inside(w: Word, ln) -> bool:
    cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
    return w.block == ln.block and ln.x0 - 1 <= cx <= ln.x1 + 1 and ln.y0 - 1 <= cy <= ln.y1 + 1


class BookText:
    """Sentences of one Document, read lazily a page at a time (on the GUI thread, as
    PyMuPDF isn't thread-safe)."""

    def __init__(self, doc: Document):
        self.doc = doc
        self._toks: dict[int, list[_Tok]] = {}
        self._skip: dict[int, set[int]] = {}
        self._edges: dict[int, list[tuple[float, str, list[int]]]] = {}

    # -- what is left out -------------------------------------------------------------
    def skipped(self, page: int) -> set[int]:
        """Indices of the page's words that are never read."""
        if page not in self._skip:
            self._skip[page] = self._furniture(page) | self._footnotes(page) | self._captions(page)
        return self._skip[page]

    def _edge_lines(self, page: int) -> list[tuple[float, str, list[int]]]:
        """(y centre, key, word indices) of the outermost lines at the top and bottom."""
        if page not in self._edges:
            h = self.doc.page_size(page)[1]
            rows: list[list] = []  # [y centre, [indices]]
            for i, w in enumerate(self.doc.words(page)):
                y = (w.y0 + w.y1) / 2
                if EDGE_BAND * h < y < (1 - EDGE_BAND) * h:
                    continue
                row = next((r for r in rows if abs(r[0] - y) <= 2), None)
                if row:
                    row[1].append(i)
                else:
                    rows.append([y, [i]])
            rows.sort(key=lambda r: r[0])
            top = [r for r in rows if r[0] <= EDGE_BAND * h][:2]
            bottom = [r for r in rows if r[0] >= (1 - EDGE_BAND) * h][-2:]
            words = self.doc.words(page)
            self._edges[page] = [
                (y, _key(" ".join(words[i].text for i in sorted(ix, key=lambda i: words[i].x0))), ix)
                for y, ix in top + bottom
            ]
        return self._edges[page]

    def _furniture(self, page: int) -> set[int]:
        if self.doc.doc.is_reflowable or self.doc.web_snapshot:
            return set()
        h = self.doc.page_size(page)[1]
        words = self.doc.words(page)
        out: set[int] = set()
        for y, key, ix in self._edge_lines(page):
            text = " ".join(words[i].text for i in ix)
            repeats = len(key) <= 80 and any(
                k == key and abs(oy - y) < 0.03 * h
                for q in (page - 2, page - 1, page + 1, page + 2) if 0 <= q < self.doc.page_count
                for oy, k, _ in self._edge_lines(q)
            )
            if PAGE_NUMBER.match(text) or (key and repeats):
                out.update(ix)
        return out

    def _footnotes(self, page: int) -> set[int]:
        h = self.doc.page_size(page)[1]
        words = self.doc.words(page)
        out: set[int] = set()
        starts = footnote_starts(self.doc._lines(page), h)
        if starts:
            first = min(starts.values(), key=lambda ln: ln.y0)
            for i, w in enumerate(words):
                if (w.y0 + w.y1) / 2 >= first.y0 - 1 and (w.x0 + w.x1) / 2 >= first.x0 - 20:
                    out.add(i)
        for link in self.doc.links(page):
            if not link.footnote:
                continue
            r = link.rect
            for i, w in enumerate(words):
                cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
                if r[0] - 1 <= cx <= r[2] + 1 and r[1] - 1 <= cy <= r[3] + 1 and w.x1 - w.x0 <= r[2] - r[0] + 2:
                    out.add(i)
        return out

    def _marker_glued(self, page: int, w: Word) -> bool:
        """A footnote marker printed onto the end of a word (``devices.1``)."""
        return any(
            ln.footnote and ln.rect[0] > w.x0 and ln.rect[0] < w.x1 + 1 and ln.rect[1] < w.y1 and w.y0 < ln.rect[3]
            for ln in self.doc.links(page)
        )

    def _captions(self, page: int) -> set[int]:
        lines = self.doc._lines(page)
        body = self.doc.body_size(page)
        chosen = []
        for k, ln in enumerate(lines):
            text = _line_text(ln)
            if not _label(text):
                continue
            rest = text[LABEL.match(text).end():].lstrip()
            small = max(s["size"] for s in ln.spans) < 0.95 * body
            if not (rest[:1] in tuple(".:—–-|") and rest or ln.spans[0]["flags"] & BOLD or small):
                continue
            chosen.append(ln)
            # The rest of its row ("Figure 2." then the caption's text, set apart).
            chosen += [o for o in lines if o is not ln and o.x0 >= ln.x1 - 1 and o.y0 < ln.y1 and ln.y0 < o.y1]
            prev = ln
            for nxt in lines[k + 1:]:
                gap, hgt = nxt.y0 - prev.y1, prev.y1 - prev.y0
                if not (-1 <= gap < 0.35 * hgt) or max(s["size"] for s in nxt.spans) > max(s["size"] for s in prev.spans) + 0.5:
                    break
                chosen.append(nxt)
                prev = nxt
        out: set[int] = set()
        for i, w in enumerate(self.doc.words(page)):
            cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
            if any(ln.x0 - 1 <= cx <= ln.x1 + 1 and ln.y0 - 1 <= cy <= ln.y1 + 1 for ln in chosen):
                out.add(i)
        return out

    # -- tokens -------------------------------------------------------------------------
    def _tokens(self, page: int) -> list[_Tok]:
        if page in self._toks:
            return self._toks[page]
        words = self.doc.words(page)
        skip = self.skipped(page)
        heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
        median = statistics.median(heights) if heights else 10.0
        web = self.doc.web_snapshot
        headings = self._heading_lines(page)
        toks: list[_Tok] = []
        prev: Word | None = None  # the last body word kept
        i = 0
        while i < len(words):
            w = words[i]
            if i in skip:
                i += 1
                continue
            if web and w.y1 - w.y0 < TINY_RATIO * median:
                j = i
                while j < len(words) and j not in skip and words[j].y1 - words[j].y0 < TINY_RATIO * median:
                    j += 1
                toks.append(self._math(page, i, j, words, median, prev))
                i = j
                continue
            new_line = prev is None or (w.block, w.line) != (prev.block, prev.line)
            brk = bool(prev) and new_line and w.y0 - prev.y1 > PARA_GAP * (prev.y1 - prev.y0)
            text = w.text
            if self._marker_glued(page, w):
                text = TRAILING_MARK.sub("", text)
            text = GLUED_NOTE.sub("", text)
            toks.append(_Tok(
                text, speak_word(text), (page, i), (page, i), [(page, w)],
                brk=brk, heading=any(_inside(w, ln) for ln in headings), line_start=new_line,
            ))
            prev = w
            i += 1
        self._toks[page] = toks
        return toks

    def _heading_lines(self, page: int) -> list:
        """Lines set larger than the page's body text, with words in them (not lone symbols)."""
        body = self.doc.body_size(page)
        out = []
        for ln in self.doc._lines(page):
            letters = sum(c.isalpha() for s in ln.spans for c in s["text"])
            big = sum(len(s["text"]) for s in ln.spans if s["size"] > HEADING_RATIO * body)
            if letters >= 3 and big > 0.6 * sum(len(s["text"]) for s in ln.spans):
                out.append(ln)
        return out

    def _math(self, page: int, i: int, j: int, words: list[Word], median: float, prev: Word | None) -> _Tok:
        run = words[i:j]
        src = " ".join(w.text for w in run).strip()
        display = src.startswith("$$")
        tex = src.strip("$ ").strip()
        x0, x1 = min(w.x0 for w in run), max(w.x1 for w in run)
        y0, y1 = min(w.y0 for w in run), max(w.y1 for w in run)
        if display:
            top = prev.y1 if prev and prev.y1 < y0 else y0 - 2 * median
            rect = (x0, top, x1, y1)
        else:
            rect = (x0, y0, x1, y0 + median)
        return _Tok("x", tex_to_speech(tex), (page, i), (page, j - 1), extra={page: [rect]}, alone=display, brk=display)

    # -- sentences ----------------------------------------------------------------------
    def utterances(self, start: tuple[int, int], end: tuple[int, int] | None = None) -> Iterator[Utterance]:
        """Sentences from the word at ``start`` on, up to the one holding ``end``."""
        # Lone symbols (an end-of-proof ■) have nothing to say.
        return (u for u in self._sentences(start, end) if any(c.isalnum() for c in u.text))

    def _sentences(self, start: tuple[int, int], end: tuple[int, int] | None) -> Iterator[Utterance]:
        buf: list[_Tok] = []
        size = 0
        for page in range(start[0], self.doc.page_count):
            for t in self._tokens(page):
                if t.last < start:
                    continue
                if end is not None and t.first > end:
                    if buf:
                        yield self._utterance(buf, True)
                    return
                if buf and self._hyphenated(buf[-1], t):
                    a = buf[-1]  # a copy: the page's cached tokens stay as they are
                    joined = a.text[:-1] + t.text
                    buf[-1] = replace(a, text=joined, spoken=speak_word(joined), words=a.words + t.words, last=t.last)
                    continue
                if buf:
                    last = buf[-1]
                    boundary = (
                        t.brk or t.alone or last.alone or t.heading != last.heading
                        or ends_sentence(last.text, t.text)
                        or size > MAX_CHARS
                        or (size > SOFT_CHARS and last.text[-1:] in ";:—–,")
                    )
                    if boundary:
                        yield self._utterance(buf, t.brk or t.heading != last.heading or last.alone)
                        buf, size = [], 0
                buf.append(t)
                size += len(t.spoken) + 1
        if buf:
            yield self._utterance(buf, True)

    @staticmethod
    def _hyphenated(a: _Tok, b: _Tok) -> bool:
        return (
            len(a.text) > 1 and a.text.endswith("-") and a.text[-2].isalpha() and not a.extra
            and (b.line_start or b.first[0] != a.last[0]) and b.text[:1].islower() and not b.extra
        )

    def _utterance(self, toks: list[_Tok], paragraph_end: bool) -> Utterance:
        parts: list[str] = []
        marks = []
        pos = 0
        for t in toks:
            if not t.spoken:
                continue
            if parts:
                pos += 1
            marks.append((pos, pos + len(t.spoken), t.refs()))
            parts.append(t.spoken)
            pos += len(t.spoken)
        text = " ".join(parts)
        heading = toks[0].heading
        if heading and text and not text[-1] in ".!?:;":
            text += "."
        rects: dict[int, list[Rect]] = {}
        pages = sorted({p for t in toks for p, _ in t.words} | {p for t in toks for p in t.extra})
        for p in pages:
            rects[p] = line_rects([w for t in toks for q, w in t.words if q == p])
            rects[p] += [r for t in toks for r in t.extra.get(p, [])]
        return Utterance(
            text, toks[0].first, toks[-1].last, rects, marks,
            LONG_PAUSE if heading or paragraph_end else PAUSE,
        )

    # -- positions ----------------------------------------------------------------------
    def start_at(self, page: int, y: float) -> tuple[int, int]:
        """The first word read at or below ``y`` on ``page`` (else the next page's top)."""
        skip = self.skipped(page)
        for i, w in enumerate(self.doc.words(page)):
            if i not in skip and (w.y0 + w.y1) / 2 >= y:
                return page, i
        return (page + 1, 0) if page + 1 < self.doc.page_count else (page, 0)

    def utterance_at(self, page: int, idx: int) -> Utterance | None:
        """The sentence holding a word, or the next one read after it."""
        for u in self.utterances((max(page - 1, 0), 0)):
            if u.covers(page, idx) or u.first > (page, idx):
                return u
        return None

    def utterance_before(self, u: Utterance) -> Utterance | None:
        best = None
        for v in self.utterances((max(u.first[0] - 1, 0), 0)):
            if v.last >= u.first:
                break
            best = v
        return best
