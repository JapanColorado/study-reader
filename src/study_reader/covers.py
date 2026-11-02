"""Book covers for the Library and Home: a small image of each book's first page, kept in
<data>/covers by content hash (so a moved file keeps its cover)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from .db import data_dir
from .document import REFLOW_FONT, REFLOW_HEIGHT, REFLOW_WIDTH

COVER_WIDTH = 240  # pixels; shown at up to half that on a 2x screen


def cover_file(book_hash: str) -> Path:
    folder = data_dir() / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{book_hash}.png"


def render(path: str | Path, out: Path, width: int = COVER_WIDTH) -> bool:
    """Draw the first page of the book at `path` into `out`; False if it can't be read."""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return False
    try:
        if doc.is_reflowable:
            doc.layout(width=REFLOW_WIDTH, height=REFLOW_HEIGHT, fontsize=REFLOW_FONT)
        if doc.page_count == 0:
            return False
        page = doc[0]
        scale = width / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        tmp = out.with_suffix(".tmp.png")
        pix.save(tmp)
        tmp.replace(out)
        return True
    except Exception:
        return False
    finally:
        doc.close()


def cover(book) -> Path | None:
    """The book's cover image, drawn first if need be; None if its file can't be read."""
    out = cover_file(book["hash"])
    if out.exists() or render(book["path"], out):
        return out
    return None


def cached(book_hash: str) -> Path | None:
    """The cover if it has been drawn already."""
    out = cover_file(book_hash)
    return out if out.exists() else None
