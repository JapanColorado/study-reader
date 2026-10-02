"""Web pages as articles: LessWrong-family sites through their GraphQL API, anything
else through readability. `build_html` turns an `Article` into one HTML document that
`web_render` prints to a PDF snapshot, which then opens like any other book."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import lxml.html
from readability import Document as Readability

ASSETS = Path(__file__).parent / "assets"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 30
# Top-scoring comments kept under a post (their parents come along for context).
MAX_COMMENTS = 30
FETCH_COMMENTS = 500

# ForumMagnum sites share one GraphQL API; GreaterWrong mirrors LessWrong.
FORUMS = {
    "lesswrong.com": "https://www.lesswrong.com",
    "greaterwrong.com": "https://www.lesswrong.com",
    "alignmentforum.org": "https://www.alignmentforum.org",
    "forum.effectivealtruism.org": "https://forum.effectivealtruism.org",
}


@dataclass
class Section:
    """A chapter: a sequence post, a wiki tab, or the comments. An untitled section
    continues the document title (a single post's body)."""
    title: str
    html: str
    byline: str = ""


@dataclass
class Article:
    title: str
    url: str
    byline: str = ""
    intro: str = ""  # HTML shown under the title (a sequence's description)
    sections: list[Section] = field(default_factory=list)


@dataclass
class Download:
    """The URL pointed straight at a PDF or EPUB."""
    data: bytes
    ext: str
    title: str


class WebImportError(Exception):
    pass


# -- fetching -------------------------------------------------------------------

def http_get(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[bytes, str]:
    """(body, content type). Tests replace this."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(), r.headers.get("Content-Type", "")


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise WebImportError(f"Not a web address: {url}")
    # Forum pages are whole regardless of query (?lens=, ?commentId=), so one URL each.
    query = "" if parts.netloc.lower().removeprefix("www.") in FORUMS else parts.query
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def fetch(url: str, status=lambda text: None) -> Article | Download:
    url = normalize_url(url)
    forum = _forum(url)
    if forum:
        article = _fetch_forum(forum, url, status)
        if article:
            return article
    status("Downloading page…")
    body, ctype = http_get(url)
    path = urllib.parse.urlsplit(url).path.lower()
    if "pdf" in ctype or body[:5] == b"%PDF-":
        return Download(body, "pdf", _title_from_path(path))
    if "epub" in ctype or path.endswith(".epub"):
        return Download(body, "epub", _title_from_path(path))
    return _generic(url, body, ctype)


def _title_from_path(path: str) -> str:
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return name.rsplit(".", 1)[0] or "Download"


# -- LessWrong & friends -----------------------------------------------------------

def _forum(url: str) -> str | None:
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    return FORUMS.get(host)


def forum_target(url: str) -> tuple[str, str] | None:
    """What a forum URL points at: ("post", id), ("post-slug", slug), ("sequence", id),
    ("wiki", slug), or None for pages that aren't articles (front page, users, …)."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    if len(parts) >= 4 and parts[0] == "s" and parts[2] == "p":
        return ("post", parts[3])
    if len(parts) >= 2 and parts[0] == "posts":
        return ("post", parts[1])
    if len(parts) >= 2 and parts[0] == "s":
        return ("sequence", parts[1])
    if len(parts) >= 2 and parts[0] in ("w", "tag", "tags", "topics", "wiki"):
        return ("wiki", parts[1])
    if len(parts) == 2 and parts[0] in ("rationality", "codex", "hpmor", "highlights"):
        return ("post-slug", parts[1])
    return None


def _gql(site: str, query: str) -> dict:
    body, _ = http_get(
        f"{site}/graphql", json.dumps({"query": query}).encode(), {"Content-Type": "application/json"}
    )
    out = json.loads(body)
    if out.get("errors"):
        raise WebImportError(out["errors"][0].get("message", "GraphQL error"))
    return out["data"]


def _q(s: str) -> str:
    return json.dumps(s)


POST_FIELDS = "_id title slug postedAt pageUrl user { displayName } coauthors { displayName } contents { html }"


def _fetch_forum(site: str, url: str, status) -> Article | None:
    target = forum_target(url)
    if target is None:
        return None
    kind, key = target
    if kind == "sequence":
        status("Fetching sequence…")
        return _sequence(site, url, key)
    if kind == "wiki":
        status("Fetching wiki page…")
        return _wiki(site, url, key)
    status("Fetching post…")
    if kind == "post":
        post = _gql(site, f"{{ post(input: {{selector: {{_id: {_q(key)}}}}}) {{ result {{ {POST_FIELDS} }} }} }}")
        post = post["post"]["result"]
    else:
        found = _gql(site, f'{{ posts(input: {{terms: {{view: "slugPost", slug: {_q(key)}}}}}) {{ results {{ {POST_FIELDS} }} }} }}')
        post = (found["posts"]["results"] or [None])[0]
    if not post:
        raise WebImportError("Post not found")
    article = Article(post["title"], url, _byline(post), sections=[Section("", _post_html(post, site))])
    status("Fetching comments…")
    comments = _top_comments(site, post["_id"])
    if comments:
        article.sections.append(Section("Comments", comments))
    return article


def _sequence(site: str, url: str, seq_id: str) -> Article:
    data = _gql(site, f"""{{ sequence(input: {{selector: {{_id: {_q(seq_id)}}}}}) {{ result {{
        title user {{ displayName }} contents {{ html }}
        chapters {{ title subtitle contents {{ html }} posts {{ {POST_FIELDS} }} }} }} }} }}""")
    seq = data["sequence"]["result"]
    if not seq:
        raise WebImportError("Sequence not found")
    article = Article(seq["title"], url, (seq.get("user") or {}).get("displayName", ""),
                      _clean_forum_html((seq.get("contents") or {}).get("html") or "", site))
    for ch in seq["chapters"] or []:
        intro = _clean_forum_html((ch.get("contents") or {}).get("html") or "", site)
        if ch.get("title") or intro:
            # A titled chapter becomes a divider page; its posts follow it.
            sub = f'<p class="subtitle">{html.escape(ch["subtitle"])}</p>' if ch.get("subtitle") else ""
            article.sections.append(Section(ch.get("title") or "Introduction", sub + intro, ""))
        for post in ch["posts"] or []:
            article.sections.append(Section(post["title"], _post_html(post, site), _byline(post)))
    return article


def _wiki(site: str, url: str, slug: str) -> Article:
    """A wiki page's main text and each of its tabs ("lenses")."""
    data = _gql(site, f"""{{ tags(input: {{terms: {{view: "tagBySlug", slug: {_q(slug)}}}}}) {{ results {{
        name subtitle description {{ html }}
        lenses {{ title tabTitle tabSubtitle index contents {{ html }} }} }} }} }}""")
    tags = data["tags"]["results"]
    if not tags:
        raise WebImportError("Wiki page not found")
    tag = tags[0]
    main = _clean_forum_html((tag.get("description") or {}).get("html") or "", site)
    lenses = sorted(tag.get("lenses") or [], key=lambda l: l.get("index") or 0)
    article = Article(tag["name"], url, tag.get("subtitle") or "")
    if not lenses:
        article.sections.append(Section("", main))
        return article
    article.sections.append(Section("Main", main))
    for lens in lenses:
        title = lens.get("tabTitle") or lens.get("title") or "Tab"
        if lens.get("tabSubtitle"):
            title += f" ({lens['tabSubtitle']})"
        body = _clean_forum_html((lens.get("contents") or {}).get("html") or "", site)
        sub = lens.get("title") if lens.get("title") and lens.get("title") != tag["name"] else ""
        article.sections.append(Section(title, body, sub or ""))
    return article


def _top_comments(site: str, post_id: str) -> str:
    """The best comments, threaded, each with the comments it replies to."""
    data = _gql(site, f"""{{ comments(input: {{terms: {{view: "postCommentsTop", postId: {_q(post_id)},
        limit: {FETCH_COMMENTS}}}}}) {{ results {{ _id parentCommentId baseScore postedAt deleted
        user {{ displayName }} contents {{ html }} }} }} }}""")
    comments = {c["_id"]: c for c in data["comments"]["results"] if not c.get("deleted")}
    keep: set[str] = set()
    for c in sorted(comments.values(), key=lambda c: -(c.get("baseScore") or 0))[:MAX_COMMENTS]:
        while c and c["_id"] not in keep:
            keep.add(c["_id"])
            c = comments.get(c.get("parentCommentId") or "")
    children: dict[str | None, list[dict]] = {}
    for cid in keep:
        c = comments[cid]
        parent = c.get("parentCommentId") if c.get("parentCommentId") in keep else None
        children.setdefault(parent, []).append(c)

    best: dict[str, int] = {}

    def best_in(c: dict) -> int:
        """A thread ranks by its best comment, so a great reply lifts its parent."""
        if c["_id"] not in best:
            best[c["_id"]] = max([c.get("baseScore") or 0, *map(best_in, children.get(c["_id"], []))])
        return best[c["_id"]]

    def render(parent: str | None) -> str:
        out = []
        for c in sorted(children.get(parent, []), key=lambda c: -best_in(c)):
            meta = " · ".join(filter(None, [
                html.escape((c.get("user") or {}).get("displayName") or "[deleted]"),
                f"{c.get('baseScore') or 0} karma", (c.get("postedAt") or "")[:10],
            ]))
            body = _clean_forum_html((c.get("contents") or {}).get("html") or "", site)
            out.append(f'<div class="comment"><p class="meta">{meta}</p>{body}{render(c["_id"])}</div>')
        return "".join(out)

    return render(None)


def _byline(post: dict) -> str:
    names = [a["displayName"] for a in [post.get("user") or {}, *(post.get("coauthors") or [])] if a.get("displayName")]
    date = (post.get("postedAt") or "")[:10]
    return " · ".join(filter(None, [", ".join(names), date]))


def _post_html(post: dict, site: str) -> str:
    return _clean_forum_html((post.get("contents") or {}).get("html") or "", site)


def _clean_forum_html(fragment: str, site: str) -> str:
    if not fragment.strip():
        return ""
    root = lxml.html.fragment_fromstring(fragment, create_parent="div")
    root.make_links_absolute(site + "/")
    _normalize_math(root)
    _tidy(root)
    return _inner(root)


# -- generic pages ----------------------------------------------------------------

def _generic(url: str, body: bytes, ctype: str) -> Article:
    charset = re.search(r"charset=([\w-]+)", ctype)
    text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    page = lxml.html.document_fromstring(text, base_url=url)
    _normalize_math(page)  # before readability drops the scripts some sites keep TeX in
    title = _meta(page, "og:title") or (page.findtext(".//title") or "").strip()
    # "Article - Site Name": drop a short trailing site name.
    m = re.match(r"^(.{8,}?)\s+[-|–—·]\s+(\S+(?:\s\S+){0,2})$", title)
    if m:
        title = m.group(1)
    byline = _meta(page, "author") or _meta(page, "article:author")
    doc = Readability(lxml.html.tostring(page, encoding="unicode"), url=url)
    content = lxml.html.fragment_fromstring(doc.summary(html_partial=True), create_parent="div")
    content.make_links_absolute(url)
    _tidy(content)
    body_html = _inner(content)
    if not lxml.html.fragment_fromstring(body_html or "<p></p>", create_parent="div").text_content().strip():
        raise WebImportError("Couldn't find any article text on this page")
    return Article(title or doc.short_title() or url, url, byline, sections=[Section("", body_html)])


def _meta(page, name: str) -> str:
    for attr in ("property", "name"):
        el = page.find(f'.//meta[@{attr}="{name}"]')
        if el is not None and el.get("content"):
            return el.get("content").strip()
    return ""


# -- cleanup ------------------------------------------------------------------------

def _math_span(tex: str, display: bool):
    el = lxml.html.Element("span", {"class": "math", "data-tex": tex.strip()})
    if display:
        el.set("data-display", "")
    return el


def _replace(old, new) -> None:
    new.tail = old.tail
    old.getparent().replace(old, new)


def _normalize_math(root) -> None:
    """Rendered math (MathJax CHTML, KaTeX, MathML, MathJax 2 scripts) back to TeX in
    `<span class="math" data-tex>`, which the page re-renders with MathJax."""
    for el in root.xpath('//span[contains(concat(" ", @class, " "), " math-tex ")]'):
        label = el.xpath('.//*[contains(@class, "mjx-math")]/@aria-label')
        if label:
            display = bool(el.xpath('.//*[contains(@class, "MJXc-display") or contains(@class, "mjpage__block")]'))
            _replace(el, _math_span(label[0], display))
    for el in root.xpath('//*[contains(concat(" ", @class, " "), " katex-display ")] | '
                         '//*[contains(concat(" ", @class, " "), " katex ")][not(ancestor::*[contains(@class, "katex")])]'):
        if el.getparent() is None:
            continue
        tex = el.xpath('.//annotation[@encoding="application/x-tex"]/text()')
        if tex:
            _replace(el, _math_span(tex[0], "katex-display" in el.get("class", "")))
    for el in root.xpath("//math"):
        if el.getparent() is None:
            continue
        tex = el.xpath('.//annotation[@encoding="application/x-tex"]/text()') or el.xpath("@alttext")
        if tex:
            # Wikipedia hides the MathML next to a picture of it; replace both.
            wrapper = el.xpath('ancestor::*[contains(concat(" ", @class, " "), " mwe-math-element ")]')
            _replace(wrapper[0] if wrapper else el, _math_span(tex[0], el.get("display") == "block"))
    for el in root.xpath('//script[starts-with(@type, "math/tex")]'):
        for prev in el.itersiblings(preceding=True):
            if "MathJax" not in prev.get("class", "") and "MathJax" not in prev.get("id", ""):
                break
            prev.drop_tree()
        _replace(el, _math_span(el.text or "", "mode=display" in el.get("type", "")))


# Page furniture that readability sometimes keeps.
DROP_CLASSES = ("mw-editsection", "noprint", "share", "social", "related-posts", "newsletter")
DROP = ("script", "style", "noscript", "iframe", "form", "button", "input", "select", "textarea", "link", "meta")


def _tidy(root) -> None:
    for el in root.xpath("|".join(f"//{t}" for t in DROP)):
        el.drop_tree()
    for cls in DROP_CLASSES:
        for el in root.xpath(f'//*[contains(concat(" ", @class, " "), " {cls} ")]'):
            el.drop_tree()
    for el in root.xpath("//img"):
        src = el.get("data-src") or el.get("src") or ""
        if not src or src.startswith("data:image/gif"):
            # Lazy-loading placeholders: take the real image from the srcset if there is one.
            srcset = el.get("data-srcset") or el.get("srcset") or ""
            src = srcset.split(",")[-1].strip().split(" ")[0] if srcset else src
        if not src:
            el.drop_tree()
            continue
        el.set("src", src)
        for attr in ("srcset", "data-srcset", "sizes", "loading", "width", "height", "style"):
            el.attrib.pop(attr, None)
    for el in root.xpath("//picture/source"):
        el.drop_tree()
    for el in root.xpath("//*[@style]"):
        del el.attrib["style"]


def _inner(root) -> str:
    return (html.escape(root.text) if root.text else "") + "".join(
        lxml.html.tostring(c, encoding="unicode") for c in root
    )


# -- the printable document ---------------------------------------------------------

CSS = """
html { font-family: "Noto Serif", "DejaVu Serif", Georgia, serif; font-size: 11.5pt; line-height: 1.5;
       color: #1b1b1b; font-variant-ligatures: none; }
body { margin: 0; }
h1, h2, h3, h4, h5, h6, .byline, .meta, .source, .subtitle { font-family: "Noto Sans", "DejaVu Sans", sans-serif; }
h1 { font-size: 1.7em; line-height: 1.2; margin: 0 0 .3em; }
h2 { font-size: 1.35em; margin: 1.4em 0 .4em; }
h3 { font-size: 1.15em; margin: 1.2em 0 .3em; }
h4, h5, h6 { font-size: 1em; margin: 1em 0 .3em; }
h1, h2, h3, h4 { break-after: avoid; }
.doc-title { font-size: 2em; }
.byline, .source, .subtitle, .meta { color: #666; font-size: .9em; margin: 0 0 .4em; }
.source { word-break: break-all; margin-bottom: 1.6em; }
.section { break-before: page; }
.section.first { break-before: auto; }
a { color: #2a5db0; text-decoration: none; }
img, video, svg { max-width: 100%; height: auto; }
/* Anything wider than the page would make Chromium shrink every page to fit it. */
body { overflow-wrap: break-word; }
table { display: block; max-width: 100%; overflow: hidden; }
mjx-container { max-width: 100%; }
figure { margin: 1em 0; text-align: center; }
figcaption { font-size: .9em; color: #555; }
blockquote { margin: 1em 0; padding-left: 1em; border-left: 3px solid #ccc; color: #333; }
pre, code { font-family: "Noto Sans Mono", "DejaVu Sans Mono", monospace; font-size: .88em; }
pre { white-space: pre-wrap; background: #f4f4f4; padding: .6em .8em; border-radius: 4px; }
table { border-collapse: collapse; margin: 1em 0; font-size: .92em; }
td, th { border: 1px solid #ccc; padding: .25em .5em; vertical-align: top; }
hr { border: 0; border-top: 1px solid #ccc; margin: 1.5em 0; }
.comment { border-left: 2px solid #ddd; padding-left: .9em; margin: 1em 0; }
.comment .comment { margin-left: .2em; }
.comment > .meta { margin-bottom: .2em; }
.math[data-display] { display: block; text-align: center; margin: .8em 0; }
mjx-assistive-mml { display: none; }
.tex-src { display: inline-block; vertical-align: top; height: 0; font-size: .5px; line-height: .6px;
           white-space: normal; overflow-wrap: anywhere; color: rgba(0, 0, 0, .01); font-family: sans-serif; }
.math[data-display] .tex-src { display: block; }
h1 a, h2 a, h3 a, h4 a, h5 a, h6 a { color: inherit; }
"""

# MathJax's SVG output draws glyphs as paths, so the only text a formula leaves in the
# PDF is its TeX source, printed nearly invisibly in the flow right where the formula is.
RENDER_MATH = r"""
function typeset(el, tex, display) {
  el.textContent = '';
  try { el.append(MathJax.tex2svg(tex, {display})); }
  catch (e) { el.textContent = tex; }
  const src = document.createElement('span');
  src.className = 'tex-src';
  src.textContent = display ? ' $$' + tex + '$$ ' : '$' + tex + '$';
  if (!display) {
    // Tucked under the formula, wrapping at its width, so it neither moves the text
    // nor sticks out past the page edge (which would make Chromium shrink every page).
    const w = Math.max(el.getBoundingClientRect().width, 12);
    src.style.width = w + 'px';
    src.style.marginLeft = -w + 'px';
  }
  el.append(src);
}
window.MathJax = {
  startup: {typeset: false, ready() {
    MathJax.startup.defaultReady();
    MathJax.startup.promise.then(() => {
      for (const el of document.querySelectorAll('.math'))
        typeset(el, el.dataset.tex, el.hasAttribute('data-display'));
      // Delimiters left as text by pages that render math client-side.
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const hits = [];
      for (let n; (n = walker.nextNode()); )
        if (/\\\(|\\\[|\$\$/.test(n.data) && !n.parentElement.closest('pre, code, .math')) hits.push(n);
      for (const n of hits) {
        const parts = n.data.split(/(\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]|\$\$[\s\S]+?\$\$)/);
        if (parts.length < 2) continue;
        const frag = document.createDocumentFragment();
        parts.forEach((p, i) => {
          if (i % 2 === 0) { frag.append(p); return; }
          const display = !p.startsWith('\\(');
          const el = document.createElement('span');
          el.className = 'math';
          if (display) el.setAttribute('data-display', '');
          typeset(el, p.slice(2, -2), display);
          frag.append(el);
        });
        n.replaceWith(frag);
      }
      window.studyReaderReady = true;
    });
  }},
  svg: {fontCache: 'local'},
  options: {enableAssistiveMml: false},
};
"""


def _demote(fragment: str, by: int) -> str:
    """Shift a fragment's headings down so they nest under the section title."""
    if not by:
        return fragment
    return re.sub(r"<(/?)h([1-6])\b", lambda m: f"<{m.group(1)}h{min(int(m.group(2)) + by, 6)}", fragment)


def build_html(article: Article) -> tuple[str, list[tuple[int, str]]]:
    """The document to print, and its table of contents as (level, heading text)."""
    out = [f'<h1 class="doc-title">{html.escape(article.title)}</h1>']
    if article.byline:
        out.append(f'<p class="byline">{html.escape(article.byline)}</p>')
    out.append(f'<p class="source"><a href="{html.escape(article.url)}">{html.escape(article.url)}</a></p>')
    if article.intro:
        out.append(article.intro)
    started = bool(article.intro)  # has anything followed the title yet?
    for s in article.sections:
        if not s.title:
            out.append(f'<div class="body">{_demote(s.html, 1)}</div>')
            started = True
            continue
        # Each titled section starts a page, except one directly under the title.
        cls = "section" if started else "section first"
        started = True
        byline = f'<p class="byline">{html.escape(s.byline)}</p>' if s.byline else ""
        out.append(f'<div class="{cls}"><h1>{html.escape(s.title)}</h1>{byline}{_demote(s.html, 1)}</div>')
    body = "\n".join(out)

    toc: list[tuple[int, str]] = []
    tree = lxml.html.fragment_fromstring(body, create_parent="div")
    for h in tree.iter("h1", "h2", "h3", "h4"):
        if "doc-title" in h.get("class", ""):
            continue
        text = " ".join(h.text_content().split())
        if text:
            toc.append((int(h.tag[1]), text))

    mathjax = (ASSETS / "mathjax" / "tex-svg-full.js").as_uri()
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(article.title)}</title>
<style>{CSS}</style><script>{RENDER_MATH}</script><script src="{mathjax}" defer></script></head>
<body>{body}</body></html>"""
    return doc, _fix_levels(toc)


def _fix_levels(toc: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Nest by heading rank without skipping levels (PyMuPDF's set_toc insists): an h4
    straight under an h1 is level 2, and a lone run of h3s is level 1."""
    out: list[tuple[int, str]] = []
    open_ranks: list[int] = []
    for rank, text in toc:
        while open_ranks and open_ranks[-1] >= rank:
            open_ranks.pop()
        open_ranks.append(rank)
        out.append((len(open_ranks), text))
    return out


def snapshot_name(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "page"
    return f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
