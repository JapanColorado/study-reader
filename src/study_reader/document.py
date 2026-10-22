"""PyMuPDF wrapper for PDF and EPUB, plus the selection geometry helpers."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

# Fixed layout for reflowable formats so page numbers are stable between runs.
REFLOW_WIDTH, REFLOW_HEIGHT, REFLOW_FONT = 600, 800, 12

Rect = tuple[float, float, float, float]

# Margin detection: pages scanned, render scale, what counts as ink, padding kept around it.
CROP_SAMPLE_PAGES, CROP_SCALE, CROP_INK, CROP_PAD = 200, 0.25, 235, 6.0

PREVIEW_CHARS = 700  # longest link-target preview
# Numbers and symbols only: superscript letters are far more often math than footnotes.
FOOTNOTE_MARK = re.compile(r"[(\[]?(\d{1,3}|[*†‡§¶‖#]{1,3})[.)\]]?")
SUPERSCRIPT, BOLD = 1, 16  # PyMuPDF span flags
BACKREF = re.compile(r"(?<!\S)[\^↩↑][\ufe0e\ufe0f]?(?!\S)")  # "back to the text" links in notes
LABEL = re.compile(r"\s*(fig(?:ure|\.)?|table|exhibit|chart|box|listing|algorithm)\s*(\d+(?:[.\-–]\d+)*)(?![\d.]\d)", re.I)
# Page margins of web-page snapshots (web_render prints with them), in points: left, top, right, bottom.
WEB_MARGINS = (46, 44, 46, 44)


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int


@dataclass(frozen=True)
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    spans: tuple[dict, ...]  # PyMuPDF "dict" spans, blank ones dropped


@dataclass(frozen=True)
class Link:
    rect: Rect  # the clickable area, on the page that holds the link
    page: int  # target page and point, in page points
    x: float
    y: float
    footnote: bool = False  # a footnote marker, as opposed to a cross-reference
    quiet: bool = False  # works, but isn't marked on the page (headings linking to the contents)
    # What to look for on the target page when the PDF's point is off (see Document.target):
    # ("note", marker), ("back", marker or None, the in-text link's name) or ("label", ("fig", "2.1")).
    hint: tuple | None = None


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Document:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.doc = pymupdf.open(self.path)
        if self.doc.is_reflowable:
            self.doc.layout(width=REFLOW_WIDTH, height=REFLOW_HEIGHT, fontsize=REFLOW_FONT)
        self._words: dict[int, list[Word]] = {}
        self._line_cache: dict[int, list[Line]] = {}
        self._links: dict[int, list[Link]] = {}
        self._raw: dict[int, list[dict]] = {}
        self._body_size: dict[int, float] = {}
        self._targets: dict[Link, tuple[float, float]] = {}
        self._contents: set[int] | None = None

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def title(self) -> str:
        return (self.doc.metadata or {}).get("title") or Path(self.path).stem

    def toc(self) -> list[tuple[int, str, int]]:
        """(level, title, 0-based page). Titles lose control characters (some PDFs pad them with NULs)."""
        return [(lvl, re.sub(r"[\x00-\x1f\x7f]+", " ", title).strip(), max(page - 1, 0))
                for lvl, title, page, *_ in self.doc.get_toc()]

    def page_size(self, index: int) -> tuple[float, float]:
        r = self.doc[index].rect
        return r.width, r.height

    def render(self, index: int, scale: float, clip: Rect | None = None) -> pymupdf.Pixmap:
        return self.doc[index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(clip) if clip else None, alpha=False
        )

    def content_box(self) -> Rect | None:
        """One box around the ink of every (sampled) page, in page points, so a whole book
        can be cropped consistently. Full-bleed pages (covers, scans) are ignored."""
        n = self.page_count
        step = max(n / CROP_SAMPLE_PAGES, 1)
        box: list[float] | None = None
        for i in sorted({int(k * step) for k in range(min(n, CROP_SAMPLE_PAGES))}):
            page = self.doc[i]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(CROP_SCALE, CROP_SCALE), colorspace=pymupdf.csGRAY, alpha=False)
            ink = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width] < CROP_INK
            rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
            if not len(rows):
                continue
            r = [cols[0] / CROP_SCALE, rows[0] / CROP_SCALE, (cols[-1] + 1) / CROP_SCALE, (rows[-1] + 1) / CROP_SCALE]
            w, h = page.rect.width, page.rect.height
            if (r[2] - r[0]) > 0.97 * w and (r[3] - r[1]) > 0.97 * h:
                continue
            box = r if box is None else [min(box[0], r[0]), min(box[1], r[1]), max(box[2], r[2]), max(box[3], r[3])]
        if box is None:
            return None
        return (float(box[0]) - CROP_PAD, float(box[1]) - CROP_PAD, float(box[2]) + CROP_PAD, float(box[3]) + CROP_PAD)

    def words(self, index: int) -> list[Word]:
        if index not in self._words:
            self._words[index] = [
                Word(w[0], w[1], w[2], w[3], w[4], w[5], w[6])
                for w in self.doc[index].get_text("words", sort=False)
            ]
        return self._words[index]

    def _lines(self, index: int) -> list[Line]:
        if index not in self._line_cache:
            out = []
            for b in self.doc[index].get_text("dict", sort=False)["blocks"]:
                for ln in b.get("lines", []):
                    spans = [s for s in ln["spans"] if s["text"].strip()]
                    if spans:
                        x0, y0, x1, y1 = ln["bbox"]
                        out.append(Line(x0, y0, x1, y1, b["number"], tuple(spans)))
            self._line_cache[index] = out
        return self._line_cache[index]

    def links(self, index: int) -> list[Link]:
        """Internal links on a page (footnotes, cross-references), plus unlinked footnote
        markers matched to their footnote at the bottom of the page."""
        if index not in self._links:
            links = [link for ln in self._raw_links(index) if (link := self._link(index, ln))]
            for rect, x, y in footnote_refs(self._lines(index), self.page_size(index)[1]):
                if not any(_overlaps(rect, ln.rect) for ln in links):
                    links.append(Link(rect, index, x, y, True))
            self._links[index] = links
        return self._links[index]

    def _raw_links(self, index: int) -> list[dict]:
        if index not in self._raw:
            self._raw[index] = self.doc[index].get_links()
        return self._raw[index]

    def _link(self, index: int, ln: dict) -> Link | None:
        """A link, from what its own page says; the target page is only read once needed."""
        name = ln.get("nameddest") or ""
        if name:
            # PyMuPDF leaves a named destination's point in PDF (bottom-up) coordinates.
            target, x, y = self.doc.resolve_link("#" + name)
            if self._web_snapshot:
                # Chromium writes named destinations as if the page had no margins.
                x, y = x + WEB_MARGINS[0], y + WEB_MARGINS[1]
        elif ln["kind"] == pymupdf.LINK_GOTO:
            target, x, y = ln["page"], ln["to"].x, ln["to"].y
        else:
            return None
        if not 0 <= target < self.page_count:
            return None
        rect = tuple(ln["from"])
        mark = marker_at(self._lines(index), rect)
        if mark and not mark[1]:
            hint = ("note", mark[0])
        elif mark or name.startswith("fnref"):
            hint = ("back", mark and mark[0], "fn" + name[5:] if name.startswith("fnref") else None)
        else:
            text = words_text([
                w for w in self.words(index)
                if rect[0] <= (w.x0 + w.x1) / 2 <= rect[2] and rect[1] <= (w.y0 + w.y1) / 2 <= rect[3]
            ])
            label = _label(text)
            hint = ("label", label) if label else None
        footnote = mark is not None or name.lower().startswith("fn") or "footnote" in name.lower()
        # Headings that link back to the contents would mark up every section opening.
        quiet = not footnote and (self._is_heading(index, rect) or target in self._contents_pages)
        return Link(rect, target, x, y, footnote, quiet, hint)

    def target(self, link: Link) -> tuple[float, float]:
        """Where a link really points. Many books point links at the top of the page, so a
        footnote marker is matched to the footnote with the same marker on the target page
        (and a link back from a footnote to the marker in the text), and "Figure 2.1" to
        its caption."""
        if link in self._targets:
            return self._targets[link]
        x, y, hint, page = link.x, link.y, link.hint, link.page
        found: tuple[float, float] | None = None
        if hint and hint[0] == "note":
            if note := footnote_starts(self._lines(page), self.page_size(page)[1]).get(hint[1]):
                found = note.x0, note.y0
        elif hint and hint[0] == "back":
            _, mark, name = hint
            for other in self._raw_links(page):
                if (name and other.get("nameddest") == name) or (
                    mark and marker_at(self._lines(page), tuple(other["from"])) == (mark, False)
                ):
                    found = other["from"].x0, other["from"].y0
                    break
        elif hint and hint[0] == "label":
            # The caption rather than a line that mentions the figure: captions are usually
            # bold, and mentions are usually links themselves.
            refs = [tuple(other["from"]) for other in self._raw_links(page)]
            cands = [c for c in self._lines(page) if _label(_line_text(c)) == hint[1]]
            cands.sort(key=lambda c: (
                not c.spans[0]["flags"] & BOLD,
                any(_overlaps((c.x0, c.y0, c.x1, c.y1), r) for r in refs),
                c.y0,
            ))
            if cands:
                found = cands[0].x0, cands[0].y0
        self._targets[link] = found or (x, y)
        return self._targets[link]

    @property
    def _web_snapshot(self) -> bool:
        return (self.doc.metadata or {}).get("creator") == "Study Reader"

    def _is_heading(self, index: int, rect: Rect) -> bool:
        if index not in self._body_size:
            sizes: Counter[float] = Counter()
            for ln in self._lines(index):
                for s in ln.spans:
                    sizes[round(s["size"], 1)] += len(s["text"])
            self._body_size[index] = sizes.most_common(1)[0][0] if sizes else 0
        body = self._body_size[index]
        inside = [s["size"] for ln in self._lines(index) for s in ln.spans if _overlaps(tuple(s["bbox"]), rect)]
        return bool(inside) and max(inside) > 1.15 * body

    @property
    def _contents_pages(self) -> set[int]:
        """Pages of the table of contents, going by the outline."""
        if self._contents is None:
            toc = self.doc.get_toc()
            self._contents = set()
            for i, (_, title, page, *_) in enumerate(toc):
                if title.strip(" \x00").lower().endswith("contents") and page > 0:
                    end = next((p for _, _, p, *_ in toc[i + 1 :] if p > page), page + 1)
                    self._contents.update(range(page - 1, end - 1))
        return self._contents

    def link_target(self, link: Link) -> tuple[str, list[Rect]]:
        """The text a link points at (a footnote, a heading and what follows, a caption)
        and the rects of its lines, for previewing and flashing the target."""
        x, y = self.target(link)
        if x <= 1 and y <= 1:
            return "", []  # the whole page, not a spot on it
        lines = self._lines(link.page)
        below = [ln for ln in lines if ln.y0 >= y - 2]
        if not below:
            return "", []
        # Nearest line below the point; in a two-column layout, the one in the point's column.
        top = min(ln.y0 for ln in below)
        first = min((ln for ln in below if ln.y0 <= top + 3), key=lambda ln: _x_dist(ln, x))
        if link.hint and link.hint[0] == "back":
            # Back to the marker in the text: show the lines leading up to it.
            h = first.y1 - first.y0
            above = sorted(
                (ln for ln in lines if first.y0 - 3 * h < ln.y1 <= first.y0 + 1 and ln.x1 > first.x0 and ln.x0 < first.x1),
                key=lambda ln: ln.y0,
            )
            chosen = [*above[-2:], first]
            return BACKREF.sub("", " ".join(_line_text(ln) for ln in chosen)).strip(), [
                (ln.x0, ln.y0, ln.x1, ln.y1) for ln in chosen
            ]
        chosen = [first]
        for ln in lines[lines.index(first) + 1 :]:
            text = sum(len(_line_text(c)) for c in chosen)
            if text >= PREVIEW_CHARS or is_footnote_start(ln):
                break
            if ln.block != chosen[-1].block and text >= 12:
                # A heading takes the paragraph under it too, and a bare marker ("1. ↩") the
                # note after it.
                heading = max(s["size"] for s in chosen[-1].spans) > 1.1 * max(s["size"] for s in ln.spans)
                gap, h = ln.y0 - chosen[-1].y1, chosen[-1].y1 - chosen[-1].y0
                # Some PDFs (Chromium's) make every line its own block; a line set tight
                # under the last one continues the paragraph.
                runs_on = 0 <= gap < 0.35 * h and ln.x0 >= chosen[0].x0 - 2
                if not runs_on and (not heading or text >= 120 or gap > 2 * h):
                    break
            chosen.append(ln)
        text = BACKREF.sub("", " ".join(_line_text(ln) for ln in chosen)).strip()
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
        return text, [(ln.x0, ln.y0, ln.x1, ln.y1) for ln in chosen]

    def page_text(self, index: int) -> str:
        return self.doc[index].get_text("text").strip()

    def full_text(self) -> str:
        return "\n\n".join(f"[[page {i + 1}]]\n{self.page_text(i)}" for i in range(self.page_count))

    def text_window(self, page: int, max_chars: int) -> str:
        """Text of the pages around `page`, growing outwards until the budget is spent."""
        chosen = {page: self.page_text(page)}
        used = len(chosen[page])
        lo, hi = page - 1, page + 1
        while used < max_chars and (lo >= 0 or hi < self.page_count):
            for i in (hi, lo):
                if 0 <= i < self.page_count:
                    t = self.page_text(i)
                    if used + len(t) > max_chars:
                        used = max_chars
                        break
                    chosen[i] = t
                    used += len(t)
            lo, hi = lo - 1, hi + 1
        return "\n\n".join(f"[[page {i + 1}]]\n{chosen[i]}" for i in sorted(chosen))

    def close(self) -> None:
        self.doc.close()


# -- footnotes ---------------------------------------------------------------

def _line_text(ln: Line) -> str:
    return " ".join(s["text"].strip() for s in ln.spans)


def _x_dist(ln: Line, x: float) -> float:
    return max(ln.x0 - x, 0, x - ln.x1)


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _mark_key(text: str) -> str | None:
    m = FOOTNOTE_MARK.fullmatch(text.strip())
    return m.group(1) if m else None


def _label(text: str) -> tuple[str, str] | None:
    """("fig", "2.1") for text starting "Figure 2.1", so a reference can find its caption."""
    m = LABEL.match(text)
    return (m.group(1).lower()[:3], m.group(2).replace("–", "-")) if m else None


def marker_at(lines: list[Line], rect: Rect) -> tuple[str, bool] | None:
    """If a link's text is a footnote marker (a number or symbol, smaller or raised), its
    key and whether it opens the footnote itself rather than sitting in the text."""
    for ln in lines:
        spans = [s for s in ln.spans if _overlaps(tuple(s["bbox"]), rect)]
        if not spans:
            continue
        size = max(s["size"] for s in ln.spans)
        small = all(s["flags"] & SUPERSCRIPT or s["size"] < 0.85 * size for s in spans)
        key = _mark_key("".join(s["text"] for s in spans))
        if not small or key is None:
            return None
        return key, spans[0] is ln.spans[0] and is_footnote_start(ln)
    return None


def footnote_starts(lines: list[Line], page_height: float) -> dict[str, Line]:
    """The first line of each footnote in the lower part of a page, by marker."""
    starts: dict[str, Line] = {}
    for ln in lines:
        if ln.y0 > 0.4 * page_height and is_footnote_start(ln):
            starts.setdefault(_mark_key(ln.spans[0]["text"]), ln)
    return starts


def is_footnote_start(ln: Line) -> bool:
    """A line opening with a small or raised marker (``¹ Text…``), as footnotes do."""
    if len(ln.spans) < 2 or _mark_key(ln.spans[0]["text"]) is None:
        return False
    mark, text = ln.spans[0], ln.spans[1]
    return bool(mark["flags"] & SUPERSCRIPT) or mark["size"] < 0.9 * text["size"]


def footnote_refs(lines: list[Line], page_height: float) -> list[tuple[Rect, float, float]]:
    """Unlinked footnote markers: raised, small numbers or symbols in the text that match a
    footnote starting lower on the page. Each is (marker rect, footnote x, footnote y)."""
    starts = footnote_starts(lines, page_height)
    if not starts:
        return []
    out = []
    for ln in lines:
        if ln in starts.values():
            continue
        size = max(s["size"] for s in ln.spans)
        baseline = Counter(round(s["origin"][1]) for s in ln.spans if s["size"] == size).most_common(1)[0][0]
        for prev, span in zip(ln.spans, ln.spans[1:]):
            key = _mark_key(span["text"])
            note = starts.get(key) if key else None
            if note is None or note.y0 <= ln.y1 or span["size"] > 0.85 * size:
                continue
            raised = span["flags"] & SUPERSCRIPT or span["origin"][1] < baseline - 1
            # A marker follows a word; a lone letter before it is more likely math (x², y²).
            word = (prev["text"].split() or [""])[-1]
            if raised and len(word) >= 2 and not word[-1].isdigit():
                out.append((tuple(span["bbox"]), note.x0, note.y0))
    return out


# -- selection geometry --------------------------------------------------------

def nearest_word(words: list[Word], x: float, y: float) -> int | None:
    """Index of the word under (x, y), else the closest one."""
    best, best_d = None, float("inf")
    for i, w in enumerate(words):
        dx = max(w.x0 - x, 0, x - w.x1)
        dy = max(w.y0 - y, 0, y - w.y1)
        # Vertical distance dominates so a drag in the margin picks the right line.
        d = dx + dy * 4
        if d < best_d:
            best, best_d = i, d
    return best


def words_text(words: list[Word]) -> str:
    out: list[str] = []
    prev: Word | None = None
    for w in words:
        if prev is not None:
            if w.block != prev.block:
                out.append("\n")
            elif w.line != prev.line and out and out[-1].endswith("-") and len(out[-1]) > 1:
                out[-1] = out[-1][:-1]  # re-join words hyphenated across lines
            else:
                out.append(" ")
        out.append(w.text)
        prev = w
    return "".join(out)


def line_rects(words: list[Word]) -> list[Rect]:
    """Merge word boxes into one rectangle per line."""
    rects: list[list[float]] = []
    key = None
    for w in words:
        if (w.block, w.line) == key:
            r = rects[-1]
            r[0], r[1], r[2], r[3] = min(r[0], w.x0), min(r[1], w.y0), max(r[2], w.x1), max(r[3], w.y1)
        else:
            rects.append([w.x0, w.y0, w.x1, w.y1])
            key = (w.block, w.line)
    return [tuple(r) for r in rects]


def select_range(
    doc: Document, start: tuple[int, float, float], end: tuple[int, float, float]
) -> dict[int, list[Word]]:
    """Words in reading order between two (page, x, y) points, possibly across pages."""
    (p0, x0, y0), (p1, x1, y1) = start, end
    i0 = nearest_word(doc.words(p0), x0, y0)
    i1 = nearest_word(doc.words(p1), x1, y1)
    if (p1, i1 if i1 is not None else -1) < (p0, i0 if i0 is not None else -1):
        p0, i0, p1, i1 = p1, i1, p0, i0
    out: dict[int, list[Word]] = {}
    for p in range(p0, p1 + 1):
        ws = doc.words(p)
        if not ws:
            continue
        a = (i0 or 0) if p == p0 else 0
        b = (i1 if i1 is not None else -1) if p == p1 else len(ws) - 1
        if b >= a:
            out[p] = ws[a : b + 1]
    return out
