"""Migrating decks from Anki: reads a copy of an Anki collection (so Anki can stay open) and
brings its notes, cards, schedules, review history, media and FSRS parameters into the app's
decks. Anki itself is never written to. Qt-free.

Anki's note types map onto card types (cardmodel.TYPES) by their fields: cloze types (and Cloze
Overlapping) to cloze, image occlusion to occlusion, Front/Back types to flashcard (or reversed,
with two templates), anything else to a flashcard made from its fields. Field HTML becomes Markdown
with inline HTML (to_markdown)."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fsrs import Card, Rating, State

from . import cardmodel, srs
from .db import Database, media_dir


class AnkiError(RuntimeError):
    """A collection that can't be read."""


# -- finding collections -------------------------------------------------------------------
def find_collections() -> list[tuple[str, Path]]:
    """(profile name, collection.anki2 path) for every Anki profile on this machine."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    roots = [data / "Anki2", Path.home() / ".local/share/Anki2", Path.home() / ".var/app/net.ankiweb.Anki/data/Anki2"]
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for profile in sorted(root.iterdir()):
            path = profile / "collection.anki2"
            if path.is_file() and path.resolve() not in seen:
                seen.add(path.resolve())
                found.append((profile.name, path))
    return found


# -- protobuf ------------------------------------------------------------------------------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def pb_fields(b: bytes) -> list[tuple[int, int, object]]:
    """A protobuf message's (field number, wire type, value): ints for varints, bytes for
    length-delimited fields and fixed64, floats for fixed32."""
    out, i = [], 0
    b = bytes(b or b"")
    while i < len(b):
        key, i = _varint(b, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(b, i)
        elif wire == 1:
            value, i = b[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(b, i)
            value, i = b[i:i + n], i + n
        elif wire == 5:
            value, i = struct.unpack("<f", b[i:i + 4])[0], i + 4
        else:
            raise AnkiError(f"unreadable protobuf (wire type {wire})")
        out.append((number, wire, value))
    return out


def _pb_get(b: bytes, number: int, default=None):
    for n, _, v in pb_fields(b):
        if n == number:
            return v
    return default


def _pb_floats(value) -> list[float]:
    if isinstance(value, (bytes, bytearray)) and len(value) % 4 == 0:
        return [round(x, 6) for x in struct.unpack(f"<{len(value) // 4}f", value)]
    return []


# -- the collection ------------------------------------------------------------------------
@dataclass
class AnkiDeck:
    id: int
    name: str  # levels joined by '::'
    cards: int = 0  # directly in this deck (cards in filtered decks count in their home deck)
    suspended: int = 0
    note_types: list[str] = field(default_factory=list)
    config_id: int = 1


def _unicase(a: str, b: str) -> int:
    a, b = a.casefold(), b.casefold()
    return (a > b) - (a < b)


class Collection:
    """A read-only copy of an Anki collection."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise AnkiError(f"No Anki collection at {self.path}")
        self.media_dir = self.path.parent / "collection.media"
        self._tmp = Path(tempfile.mkdtemp(prefix="anki-import-"))
        try:
            shutil.copy2(self.path, self._tmp / "collection.anki2")
            wal = self.path.with_name(self.path.name + "-wal")
            if wal.exists():
                shutil.copy2(wal, self._tmp / "collection.anki2-wal")
            self.conn = sqlite3.connect(self._tmp / "collection.anki2")
            self.conn.row_factory = sqlite3.Row
            self.conn.create_collation("unicase", _unicase)
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as e:
            self.close()
            raise AnkiError(f"Can't read the Anki collection: {e}") from e
        if not {"notetypes", "fields", "templates", "decks", "deck_config"} <= tables:
            self.close()
            raise AnkiError("This collection is from an older Anki. Open it once in a current Anki first.")
        self.crt = self.conn.execute("SELECT crt FROM col").fetchone()[0]

    def __enter__(self) -> Collection:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()
            self.conn = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def config(self, key: str, default=None):
        row = self.conn.execute("SELECT val FROM config WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return default

    def fsrs_enabled(self) -> bool:
        return bool(self.config("fsrs", False))

    def decks(self) -> list[AnkiDeck]:
        """The collection's normal (not filtered) decks, by name."""
        counts = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT CASE WHEN odid THEN odid ELSE did END AS home, COUNT(*), SUM(queue=-1) FROM cards GROUP BY home")}
        types = defaultdict(set)
        for did, name in self.conn.execute(
            "SELECT DISTINCT CASE WHEN c.odid THEN c.odid ELSE c.did END, t.name FROM cards c "
            "JOIN notes n ON n.id=c.nid JOIN notetypes t ON t.id=n.mid"
        ):
            types[did].add(name)
        out = []
        for row in self.conn.execute("SELECT id, name, kind FROM decks"):
            normal = _pb_get(row["kind"], 1)
            if normal is None:  # filtered deck
                continue
            cards, suspended = counts.get(row["id"], (0, 0))
            out.append(AnkiDeck(
                id=row["id"], name=row["name"].replace("\x1f", "::"), cards=cards, suspended=suspended or 0,
                note_types=sorted(types.get(row["id"], ())), config_id=_pb_get(normal, 1, 1) or 1,
            ))
        return sorted(out, key=lambda d: d.name.casefold())

    def presets(self) -> dict[int, dict]:
        """Deck option presets: {id: {name, parameters (FSRS weights, newest version set), desired_retention}}."""
        out = {}
        for row in self.conn.execute("SELECT id, name, config FROM deck_config"):
            fields = {n: v for n, _, v in pb_fields(row["config"])}
            weights = next((w for n in (6, 5, 3) if (w := _pb_floats(fields.get(n)))), [])
            retention = fields.get(37)
            out[row["id"]] = {
                "name": row["name"], "parameters": weights,
                "desired_retention": round(retention, 4) if isinstance(retention, float) else None,
            }
        return out

    def note_types(self) -> dict[int, NoteType]:
        out = {}
        for row in self.conn.execute("SELECT id, name, config FROM notetypes"):
            names = [r[0] for r in self.conn.execute("SELECT name FROM fields WHERE ntid=? ORDER BY ord", (row["id"],))]
            qfmts = []
            for (cfg,) in self.conn.execute("SELECT config FROM templates WHERE ntid=? ORDER BY ord", (row["id"],)):
                q = _pb_get(cfg, 1, b"")
                qfmts.append(q.decode("utf-8", "replace") if isinstance(q, bytes) else "")
            cfg = row["config"] or b""
            out[row["id"]] = NoteType(
                row["id"], row["name"], names, qfmts, is_cloze=_pb_get(cfg, 1, 0) == 1,
                stock_kind=_pb_get(cfg, 9, 0) or 0,
            )
        return out


# -- note types ----------------------------------------------------------------------------
@dataclass
class NoteType:
    id: int
    name: str
    fields: list[str]
    qfmts: list[str]
    is_cloze: bool = False
    stock_kind: int = 0

    @property
    def lower(self) -> list[str]:
        return [f.lower() for f in self.fields]

    @property
    def kind(self) -> str:
        names = set(self.lower)
        if ({"occlusion", "image"} <= names or self.stock_kind == 6
                or any("image-occlusion" in q for q in self.qfmts)):
            return "occlusion"
        if self.is_cloze or any("{{cloze:" in q for q in self.qfmts) or "cloze" in self.name.lower():
            return "cloze"
        if ("front" in names and "back" in names and len(self.qfmts) == 2
                and "{{front}}" in self.qfmts[0].lower() and "{{back}}" in self.qfmts[1].lower()):
            return "reversed"
        return "flashcard"

    @property
    def generic(self) -> bool:
        """Mapped by the fallback (first field front, the rest back)."""
        return self.kind == "flashcard" and "front" not in self.lower


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in (p.strip() for p in parts) if p)


def note_fields(nt: NoteType, values: list[str]) -> tuple[str, dict]:
    """(kind, cardmodel fields) for an Anki note's field values (raw HTML)."""
    raw = {name.lower(): (values[i] if i < len(values) else "") for i, name in enumerate(nt.fields)}
    md = lambda key: to_markdown(raw.get(key, ""))
    kind = nt.kind
    source = md("source")
    source = f"Source: {source}" if source else ""
    if kind == "occlusion":
        images = _media_names([raw.get("image", "")])
        return kind, {
            "image": images[0] if images else "",
            "shapes": cardmodel.parse_occlusion(raw.get("occlusion", "")),
            "header": md("header"), "extra": _join(md("comments"), md("back extra"), source),
        }
    if kind == "cloze":
        text = raw["text"] if "text" in raw else (values[0] if values else "")
        text = re.sub(r"\[\[\w+::(.*?)\]\]", r"\1", text, flags=re.DOTALL)  # Cloze Overlapping's [[r::row]] groups
        return kind, {"text": to_markdown(text), "extra": _join(md("back extra"), md("extra"), source)}
    if not nt.generic:
        front = md("front")
        if "replace" in raw and "answer" in raw:
            return kind, {"front": front, "back": _join(md("answer"), md("back extra")), "extra": source}
        back = md("back") if "back" in raw else md("answer")
        used = {"front", "back", "answer", "back extra", "extra", "source", "comments"}
        if not back:
            back = _join(*(to_markdown(v) for k, v in raw.items() if k not in used))
        return kind, {"front": front, "back": back,
                      "extra": _join(md("back extra"), md("extra"), md("comments"), source)}
    texts = [to_markdown(v) for v in values]
    filled = [t for t in texts if t]
    return "flashcard", {"front": filled[0] if filled else "", "back": _join(*filled[1:]), "extra": ""}


# -- HTML to Markdown ----------------------------------------------------------------------
_MATH_RAW = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", re.DOTALL)
_PH = re.compile("\ue000(\\d+)\ue001")
SOFT, HARD, STRUCT, PRE = "\ue030", "\ue031", "\ue020", "\ue021"  # block breaks and protected lines
_EM = re.compile("\ue010(\\d+)\ue011|\ue012(\\d+)\ue013")
_KEEP_TAGS = {"u": "u", "sub": "sub", "sup": "sup", "s": "s", "strike": "s", "del": "s", "mark": "mark",
              "small": "small", "kbd": "kbd", "ins": "u"}
_DROP_TAGS = {"script", "style", "head", "title", "meta", "link", "iframe", "object", "embed", "noscript"}
_STYLES = ("color", "background-color", "font-weight", "font-style", "text-decoration")


def to_markdown(source: str) -> str:
    """An Anki field (HTML with \\(MathJax\\)) as Markdown with inline HTML, the format card text is
    stored in: emphasis as ** and *, line breaks as newlines, Markdown characters in the text
    escaped, and only harmless formatting kept as HTML. Cloze markers pass through."""
    if not source or not source.strip():
        return ""
    from lxml import html as lhtml

    maths: list[str] = []

    def protect(m: re.Match) -> str:
        display = m.group(2) is not None
        body = m.group(2) if display else m.group(1)
        body = re.sub(r"<br\s*/?>", " " if not display else "\n", body, flags=re.I)
        body = html.unescape(re.sub(r"<[^>]*>", "", body)).replace("\xa0", " ")
        maths.append(f"\\[{body}\\]" if display else f"\\({body}\\)")
        return f"\ue000{len(maths) - 1}\ue001"

    text = _MATH_RAW.sub(protect, source)
    try:
        root = lhtml.fragment_fromstring(text, create_parent="div")
    except Exception:
        root = None
    if root is None:
        out = _escape_text(html.unescape(re.sub(r"<[^>]*>", " ", text)))
    else:
        _merge_runs(root)
        conv = _Converter()
        out = conv.children(root)
        out = conv.resolve_emphasis(out)
    out = _blocks(out)
    out = _escape_line_starts(out)
    out = _PH.sub(lambda m: maths[int(m.group(1))], out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_MERGEABLE = {"b", "strong", "i", "em", "u"}


def _merge_runs(root) -> None:
    """<b>Kirchhoff</b><b>'s</b> → <b>Kirchhoff's</b>, so emphasis isn't split in two."""
    for el in list(root.iter()):
        prev = None
        for child in list(el):
            tag = child.tag.lower() if isinstance(child.tag, str) else ""
            if (prev is not None and tag in _MERGEABLE and tag == prev.tag.lower() and not (prev.tail or "")
                    and not child.attrib and not prev.attrib):
                if len(prev):
                    last = prev[-1]
                    last.tail = (last.tail or "") + (child.text or "")
                else:
                    prev.text = (prev.text or "") + (child.text or "")
                for grandchild in list(child):
                    prev.append(grandchild)
                prev.tail = child.tail
                el.remove(child)
            else:
                prev = child if tag else None


def _escape_text(s: str) -> str:
    s = re.sub(r"[ \t\r\n]+", " ", s.replace("\xa0", " "))
    s = re.sub(r"\\(?=[!-/:-@\[-`{-~])", r"\\\\", s)
    s = re.sub(r"([*`$])", r"\\\1", s)
    s = re.sub(r"~(?=~)|(?<=~)~", r"\\~", s)
    s = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", s)
    s = re.sub(r"&(?=#?\w+;)", "&amp;", s)
    s = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", s)
    return s


def _is_punct(ch: str) -> bool:
    if ch in "\ue000\ue001":
        return True
    return unicodedata.category(ch)[0] in "PS"


class _Converter:
    def __init__(self):
        self.em: list[str] = []  # emphasis pairs by id: "**" or "*"
        self.list_depth = 0

    def children(self, el) -> str:
        parts = [_escape_text(el.text or "")]
        for child in el:
            parts.append(self.node(child))
            parts.append(_escape_text(child.tail or ""))
        return "".join(parts)

    def node(self, el) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag:  # comment or processing instruction
            return ""
        tag = tag.lower()
        if tag in _DROP_TAGS:
            return ""
        if tag == "br":
            return "\n"
        if tag in ("b", "strong"):
            return self.emphasis(el, "**", "b")
        if tag in ("i", "em"):
            return self.emphasis(el, "*", "i")
        if tag in _KEEP_TAGS:
            inner = self.children(el)
            return f"<{_KEEP_TAGS[tag]}>{inner}</{_KEEP_TAGS[tag]}>" if inner.strip() else inner
        if tag == "code":
            code = _PH.sub("\ue000\\1\ue001", el.text_content())
            fence = "``" if "`" in code else "`"
            pad = " " if fence == "``" else ""
            return f"{fence}{pad}{code}{pad}{fence}" if code else ""
        if tag == "pre":
            lines = el.text_content().strip("\n").split("\n")
            body = "\n".join(PRE + line for line in lines)
            return f"{HARD}{PRE}```\n{body}\n{PRE}```{HARD}"
        if tag in ("div", "section", "article", "center", "header", "footer"):
            return f"{SOFT}{self.children(el)}{SOFT}"
        if tag in ("p", "blockquote"):
            return f"{HARD}{self.children(el)}{HARD}"
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            inner = re.sub(r"\s*\n\s*", " ", self.children(el)).strip()
            return f"{HARD}{STRUCT}{'#' * int(tag[1])} {inner}{HARD}" if inner else ""
        if tag in ("ul", "ol"):
            self.list_depth += 1
            items, n = [], 0
            for child in el:
                if isinstance(child.tag, str) and child.tag.lower() == "li":
                    n += 1
                    marker = f"{n}." if tag == "ol" else "-"
                    inner = re.sub(r"\s*\n\s*", " ", self.children(child)).strip()
                    items.append(f"{SOFT}{STRUCT}{'   ' * (self.list_depth - 1)}{marker} {inner}")
                else:
                    items.append(self.node(child))
            self.list_depth -= 1
            return f"{HARD}{''.join(items)}{HARD}"
        if tag == "li":
            return f"{SOFT}{STRUCT}- {self.children(el).strip()}{SOFT}"
        if tag == "hr":
            return f"{HARD}{STRUCT}***{HARD}"
        if tag == "a":
            inner = self.children(el).strip()
            href = (el.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "vbscript:")) or not inner:
                return inner
            if re.search(r"[\s()<>]", href):
                href = f"<{href.replace('<', '%3C').replace('>', '%3E')}>"
            return f"[{inner}]({href})"
        if tag == "img":
            return self.image(el)
        if tag in ("span", "font"):
            style = self.style(el)
            inner = self.children(el)
            return f'<span style="{style}">{inner}</span>' if style and inner.strip() else inner
        if tag == "table":
            return f"{HARD}{STRUCT}{_table_html(el)}{HARD}"
        return self.children(el)

    def emphasis(self, el, mark: str, tag: str) -> str:
        inner = self.children(el)
        core = inner.strip(" \n" + SOFT + HARD)
        if not core:
            return inner
        lead = inner[:len(inner) - len(inner.lstrip(" \n" + SOFT + HARD))]
        trail = inner[len(inner.rstrip(" \n" + SOFT + HARD)):]
        self.em.append(mark if "\n\n" not in core and HARD not in core else tag)
        i = len(self.em) - 1
        return f"{lead}\ue010{i}\ue011{core}\ue012{i}\ue013{trail}"

    def image(self, el) -> str:
        src = (el.get("src") or "").strip()
        style = (el.get("style") or "").replace(" ", "").lower()
        if not src or "display:none" in style:
            return ""
        alt = el.get("alt") or ""
        width, height = el.get("width"), el.get("height")
        if width or height or style or not re.fullmatch(r"[A-Za-z0-9._~/:%-]+", src):
            attrs = [f'src="{html.escape(src)}"']
            if alt:
                attrs.append(f'alt="{html.escape(alt)}"')
            for k, v in (("width", width), ("height", height)):
                if v and re.fullmatch(r"\d+%?|\d+px", v.strip()):
                    attrs.append(f'{k}="{v.strip()}"')
            return f"<img {' '.join(attrs)}>"
        return f"![{_escape_text(alt)}]({src})"

    def style(self, el) -> str:
        kept = []
        for decl in (el.get("style") or "").split(";"):
            name, _, value = decl.partition(":")
            name, value = name.strip().lower(), value.strip()
            if name in _STYLES and value and re.fullmatch(r"[#\w\s.,%()-]+", value):
                kept.append(f"{name}: {value}")
        color = el.get("color")
        if el.tag == "font" and color and re.fullmatch(r"#?\w+", color) and not any(k.startswith("color") for k in kept):
            kept.append(f"color: {color}")
        return "; ".join(kept)

    def resolve_emphasis(self, s: str) -> str:
        """** and * where CommonMark will read them as emphasis, else <b>/<i>."""
        positions = {}
        for m in _EM.finditer(s):
            i = int(m.group(1) or m.group(2))
            positions.setdefault(i, {})["open" if m.group(1) else "close"] = (m.start(), m.end())

        def neighbor(pos: int, step: int) -> str | None:
            j = pos
            while 0 <= j < len(s):
                ch = s[j]
                if ch == "\ue010" or ch == "\ue012":  # skip a whole marker forward
                    j = s.index("\ue011" if ch == "\ue010" else "\ue013", j) + 1 if step > 0 else j - 1
                    continue
                if ch in "\ue011\ue013" and step < 0:
                    j = s.rindex("\ue010" if ch == "\ue011" else "\ue012", 0, j) - 1
                    continue
                if ch in "\ue020\ue021":
                    j += step
                    continue
                return " " if ch in (SOFT, HARD, "\n") else ch
            return None

        def flanking_ok(i: int) -> bool:
            (os_, oe), (cs, ce) = positions[i]["open"], positions[i]["close"]
            before, after_open = neighbor(os_ - 1, -1), neighbor(oe, 1)
            before_close, after = neighbor(cs - 1, -1), neighbor(ce, 1)
            ws = lambda c: c is None or c.isspace()
            opener = not ws(after_open) and (not _is_punct(after_open) or ws(before) or _is_punct(before))
            closer = not ws(before_close) and (not _is_punct(before_close) or ws(after) or _is_punct(after))
            return opener and closer

        choice = {}
        for i, mark in enumerate(self.em):
            if mark in ("**", "*") and i in positions and len(positions[i]) == 2 and flanking_ok(i):
                choice[i] = (mark, mark)
            else:
                tag = {"**": "b", "*": "i"}.get(mark, mark)
                choice[i] = (f"<{tag}>", f"</{tag}>")
        return _EM.sub(lambda m: choice[int(m.group(1) or m.group(2))][0 if m.group(1) else 1], s)


def _table_html(el) -> str:
    from lxml import html as lhtml
    import copy

    t = copy.deepcopy(el)
    for bad in t.xpath(".//script|.//style"):
        bad.getparent().remove(bad)
    for node in t.iter():
        if not isinstance(node.tag, str):
            continue
        for attr in list(node.attrib):
            if attr not in ("colspan", "rowspan", "align"):
                del node.attrib[attr]
    t.tail = None
    out = lhtml.tostring(t, encoding="unicode", method="html")
    return re.sub(r"\s*\n\s*", " ", out)


def _blocks(s: str) -> str:
    """Turn the block-break markers (and the newlines around them) into one or two newlines."""
    def run(m: re.Match) -> str:
        chunk = m.group(0)
        newlines = chunk.count("\n")
        if HARD in chunk or newlines >= 2:
            return "\n\n"
        return "\n"
    s = re.sub(f"[ \t]*(?:[\n{SOFT}{HARD}][ \t]*)+", run, s)
    return s


_LINE_START = re.compile(r"^([#>+=-]|\d+(?=[.)](\s|$)))")


def _escape_line_starts(s: str) -> str:
    out = []
    for line in s.split("\n"):
        if line.startswith((STRUCT, PRE)):
            out.append(line[1:])
            continue
        line = line.lstrip(" \t")
        m = _LINE_START.match(line)
        if m:
            if m.group(1)[0].isdigit():
                n = len(m.group(1))
                line = line[:n] + "\\" + line[n:]
            else:
                line = "\\" + line
        out.append(line)
    return "\n".join(out).replace(STRUCT, "").replace(PRE, "")


# -- migrating -----------------------------------------------------------------------------
@dataclass
class Report:
    notes: int = 0
    cards: int = 0
    reviews: int = 0
    media_copied: int = 0
    skipped: int = 0  # cards imported before
    missing_media: list[str] = field(default_factory=list)
    fallback_types: dict[str, int] = field(default_factory=dict)
    parameters: list[float] | None = None
    retention: float | None = None
    errors: list[str] = field(default_factory=list)


_IMG_SRC = re.compile(r'<img[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.I)


def _media_names(values: list[str]) -> list[str]:
    names = []
    for v in values:
        for m in _IMG_SRC.finditer(v):
            names.append(html.unescape(next(g for g in m.groups() if g is not None)).strip())
    return [n for n in dict.fromkeys(names) if n and "://" not in n and not n.startswith("data:")]


def migrate(
    db: Database, collection: Collection, deck_ids, *, scheduling: bool = True, parameters: bool = True,
    prefix: str = "", media_to: Path | None = None, progress: Callable[[int, int, str], None] | None = None,
    now: float | None = None,
) -> Report:
    """Bring the cards in the given Anki decks (each deck's own cards, not its subdecks') into
    the app. Cards already migrated are skipped, so it can be run again (e.g. for more decks)."""
    now = time.time() if now is None else now
    media_to = Path(media_to) if media_to else media_dir()
    media_to.mkdir(parents=True, exist_ok=True)
    report = Report()
    deck_ids = [int(d) for d in deck_ids]
    if not deck_ids:
        return report
    decks = {d.id: d for d in collection.decks()}
    types = collection.note_types()
    marks = ",".join("?" * len(deck_ids))
    home = "CASE WHEN c.odid THEN c.odid ELSE c.did END"
    cards = collection.conn.execute(
        f"SELECT c.*, {home} AS home FROM cards c WHERE {home} IN ({marks}) ORDER BY c.nid, c.ord", deck_ids,
    ).fetchall()
    by_note: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for c in cards:
        by_note[c["nid"]].append(c)
    revlog: dict[int, list[sqlite3.Row]] = defaultdict(list)
    if scheduling:
        for r in collection.conn.execute(
            f"SELECT r.* FROM revlog r JOIN cards c ON c.id=r.cid WHERE {home} IN ({marks}) "
            "AND r.ease BETWEEN 1 AND 4 AND r.type IN (0,1,2,3) ORDER BY r.id", deck_ids,
        ):
            revlog[r["cid"]].append(r)
    deck_cache: dict[int, int] = {}

    def deck_for(anki_deck: int) -> int:
        if anki_deck not in deck_cache:
            name = decks[anki_deck].name if anki_deck in decks else "Default"
            deck_cache[anki_deck] = db.ensure_deck(f"{prefix}::{name}" if prefix.strip() else name)
        return deck_cache[anki_deck]

    sched = srs.scheduler(db)
    imported: list[tuple[int, int]] = []  # (our card id, Anki card id), for the review log
    media: set[str] = set()
    configs: Counter = Counter()
    fallback: Counter = Counter()
    note_ids = list(by_note)
    db.conn.commit()
    try:
        for done, nid in enumerate(note_ids):
            if progress and done % 50 == 0:
                progress(done, len(note_ids), f"Migrating notes… {done} of {len(note_ids)}")
            todo = []
            for c in by_note[nid]:
                if db.card_by_anki_id(c["id"]) is not None:
                    report.skipped += 1
                else:
                    todo.append(c)
            if not todo:
                continue
            note = collection.conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            nt = types.get(note["mid"]) if note else None
            if note is None or nt is None:
                report.errors.append(f"Anki note {nid}: its note type is missing")
                continue
            values = note["flds"].split("\x1f")
            existing = db.note_by_anki_id(nid)
            try:
                if existing is None:
                    kind, fields = note_fields(nt, values)
                    note_id = db.add_card_note(
                        kind, fields, deck_for(todo[0]["home"]), tags=note["tags"].split(), anki_id=nid,
                        created_at=nid / 1000, now=now,
                    )
                    db.conn.execute("UPDATE card_notes SET updated_at=? WHERE id=?", (note["mod"], note_id))
                    report.notes += 1
                    if nt.generic:
                        fallback[nt.name] += 1
                else:
                    note_id, kind, fields = existing["id"], existing["kind"], json.loads(existing["fields"])
            except (cardmodel.NoteError, ValueError, KeyError) as e:
                report.errors.append(f"Anki note {nid} ({nt.name}): {e}")
                continue
            ords = set(cardmodel.card_ords(kind, fields))
            ours = {c["ord"]: c["id"] for c in db.note_cards(note_id)}
            wanted = set()
            for c in todo:
                if c["ord"] not in ords:
                    report.errors.append(f"Anki card {c['id']} (note {nid}, {nt.name}): card {c['ord'] + 1} "
                                         "has no counterpart here; skipped")
                    continue
                deck_id = deck_for(c["home"])
                card_id = ours.get(c["ord"]) if existing is None else db.ensure_card(note_id, c["ord"], deck_id, now)
                wanted.add(c["ord"])
                created = c["id"] / 1000
                columns = _schedule(c, revlog.get(c["id"], []), collection.crt, sched, created) if scheduling else None
                if columns is None:
                    columns = srs.new_schedule_columns(created)
                db.update_card(
                    card_id, deck_id=deck_id, anki_id=c["id"], created_at=created, suspended=int(c["queue"] == -1),
                    flag=c["flags"] & 7, updated_at=c["mod"], **columns,
                )
                imported.append((card_id, c["id"]))
                configs[decks[c["home"]].config_id if c["home"] in decks else 1] += 1
                report.cards += 1
            if existing is None:
                extra = [cid for o, cid in ours.items() if o not in wanted]
                if extra:
                    db.delete_cards(extra)
            media.update(_media_names(values))
            if kind == "occlusion" and fields.get("image"):
                media.add(fields["image"])
        if progress:
            progress(len(note_ids), len(note_ids), "Copying the review history…")
        if scheduling and imported:
            with db.conn:
                for card_id, anki_id in imported:
                    for r in revlog.get(anki_id, []):
                        db.conn.execute(
                            "INSERT INTO reviews (card_id, rating, source, response, verdict, reviewed_at, duration) "
                            "VALUES (?,?,'anki','',NULL,?,?)",
                            (card_id, r["ease"], r["id"] / 1000, max(0, min(r["time"], 120_000))),
                        )
                        report.reviews += 1
    finally:
        db.conn.commit()
    report.fallback_types = dict(fallback)
    if progress:
        progress(len(note_ids), len(note_ids), "Copying images…")
    _copy_media(media, collection.media_dir, media_to, report)
    if parameters and imported and collection.fsrs_enabled():
        _take_parameters(db, collection, configs, report)
    return report


def _schedule(c, logs, crt: int, sched, created: float) -> dict | None:
    """The cards-table schedule columns for an Anki card, or None for a new card."""
    ctype = c["type"]
    if ctype not in (1, 2, 3):
        return None
    due_value = c["odue"] if c["odid"] and c["odue"] else c["due"]
    if ctype == 2 or (ctype in (1, 3) and due_value < 1_000_000_000):
        due = crt + due_value * 86400 + 12 * 3600
    else:
        due = float(due_value)
    try:
        data = json.loads(c["data"] or "{}")
    except ValueError:
        data = {}
    state = State(ctype)
    step = 0 if state in (State.Learning, State.Relearning) else None
    last = data.get("lrt") or (logs[-1]["id"] / 1000 if logs else None)
    s, d = data.get("s"), data.get("d")
    if s is None or d is None:
        if not logs:
            return None
        replay = Card(card_id=c["id"])
        for r in logs:
            replay, _ = sched.review_card(replay, Rating(r["ease"]), srs.dt(r["id"] / 1000))
        s, d = replay.stability, replay.difficulty
        last = last or (replay.last_review.timestamp() if replay.last_review else None)
    if last is None:
        last = max(min(due - max(c["ivl"], 1) * 86400, time.time()), created)
    card = Card(card_id=c["id"], state=state, step=step, stability=float(s), difficulty=float(d),
                due=srs.dt(due), last_review=srs.dt(float(last)))
    return {**srs.schedule_columns(card), "reps": max(c["reps"], 1), "lapses": c["lapses"]}


def _copy_media(names, source: Path, target: Path, report: Report) -> None:
    for name in sorted(names):
        if "/" in name or "\\" in name or name in (".", "..") or name.startswith(".."):
            report.missing_media.append(name)
            continue
        src, dst = source / name, target / name
        if not src.is_file():
            report.missing_media.append(name)
            continue
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(src, dst)
        report.media_copied += 1


def _take_parameters(db: Database, collection: Collection, configs: Counter, report: Report) -> None:
    """Use the FSRS weights and desired retention of the preset most of the cards came under."""
    presets = collection.presets()
    for config_id, _ in configs.most_common():
        preset = presets.get(config_id)
        if not preset:
            continue
        weights = preset["parameters"]
        if weights:
            try:
                srs.set_parameters(db, weights)
                report.parameters = list(srs.fsrs6_parameters(weights))
            except ValueError as e:
                report.errors.append(f"Anki's FSRS parameters ({preset['name']}) couldn't be used: {e}")
        if preset["desired_retention"]:
            db.set_setting(srs.RETENTION_SETTING, str(preset["desired_retention"]))
            report.retention = preset["desired_retention"]
        return
