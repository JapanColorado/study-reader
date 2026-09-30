"""PyMuPDF wrapper for PDF and EPUB, plus the selection geometry helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Fixed layout for reflowable formats so page numbers are stable between runs.
REFLOW_WIDTH, REFLOW_HEIGHT, REFLOW_FONT = 600, 800, 12

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int


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

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def title(self) -> str:
        return (self.doc.metadata or {}).get("title") or Path(self.path).stem

    def toc(self) -> list[tuple[int, str, int]]:
        """(level, title, 0-based page)."""
        return [(lvl, title, max(page - 1, 0)) for lvl, title, page, *_ in self.doc.get_toc()]

    def page_size(self, index: int) -> tuple[float, float]:
        r = self.doc[index].rect
        return r.width, r.height

    def render(self, index: int, scale: float, clip: Rect | None = None) -> pymupdf.Pixmap:
        return self.doc[index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(clip) if clip else None, alpha=False
        )

    def words(self, index: int) -> list[Word]:
        if index not in self._words:
            self._words[index] = [
                Word(w[0], w[1], w[2], w[3], w[4], w[5], w[6])
                for w in self.doc[index].get_text("words", sort=False)
            ]
        return self._words[index]

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
