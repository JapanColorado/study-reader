import json

import pytest

from study_reader import web_import as wi
from study_reader.db import Database


def fake_forum(monkeypatch, responses):
    """Answer GraphQL queries by the first key word found in them."""
    asked = []

    def http_get(url, data=None, headers=None):
        query = json.loads(data)["query"]
        asked.append(query)
        for key, value in responses.items():
            if key in query:
                return json.dumps({"data": value}).encode(), "application/json"
        raise AssertionError(f"unexpected query {query}")

    monkeypatch.setattr(wi, "http_get", http_get)
    return asked


def test_forum_urls():
    t = wi.forum_target
    assert t("https://www.lesswrong.com/posts/abc123/some-slug") == ("post", "abc123")
    assert t("https://www.lesswrong.com/s/seq1/p/post9") == ("post", "post9")
    assert t("https://www.lesswrong.com/s/seq1") == ("sequence", "seq1")
    assert t("https://www.lesswrong.com/w/bayes-rule?lens=guide") == ("wiki", "bayes-rule")
    assert t("https://www.lesswrong.com/tag/ai") == ("wiki", "ai")
    assert t("https://www.lesswrong.com/rationality/the-useful-idea-of-truth") == ("post-slug", "the-useful-idea-of-truth")
    assert t("https://www.lesswrong.com/") is None
    assert wi._forum("https://www.greaterwrong.com/posts/x") == "https://www.lesswrong.com"
    assert wi._forum("https://example.com/posts/x") is None
    assert wi.normalize_url("lesswrong.com/w/x?lens=guide#section") == "https://lesswrong.com/w/x"
    assert wi.normalize_url("https://example.com/a?id=3") == "https://example.com/a?id=3"
    with pytest.raises(wi.WebImportError):
        wi.normalize_url("ftp://example.com/x")


def test_wiki_page_keeps_every_tab_in_order(monkeypatch):
    fake_forum(monkeypatch, {"tagBySlug": {"tags": {"results": [{
        "name": "Bayes' rule", "subtitle": None,
        "description": {"html": '<h2>Intro</h2><p>See <a href="/w/odds">odds</a>.</p>'},
        "lenses": [
            {"title": "Bayes' rule: Guide", "tabTitle": "Guide", "tabSubtitle": None, "index": 1,
             "contents": {"html": "<p>The guide.</p>"}},
            {"title": "Bayes' rule", "tabTitle": "Definition", "tabSubtitle": "formal", "index": 0,
             "contents": {"html": "<p>The definition.</p>"}},
        ],
    }]}}})
    article = wi.fetch("https://www.lesswrong.com/w/bayes-rule")
    assert article.title == "Bayes' rule"
    assert [s.title for s in article.sections] == ["Main", "Definition (formal)", "Guide"]
    assert [s.byline for s in article.sections] == ["", "", "Bayes' rule: Guide"]
    assert 'href="https://www.lesswrong.com/w/odds"' in article.sections[0].html

    doc, toc = wi.build_html(article)
    assert toc == [(1, "Main"), (2, "Intro"), (1, "Definition (formal)"), (1, "Guide")]
    assert doc.count('class="section first"') == 1 and doc.count('class="section"') == 2


def test_post_with_top_comments(monkeypatch):
    monkeypatch.setattr(wi, "MAX_COMMENTS", 2)

    def comment(cid, parent, score):
        return {"_id": cid, "parentCommentId": parent, "baseScore": score, "postedAt": "2024-01-02T00:00:00Z",
                "deleted": False, "user": {"displayName": f"user-{cid}"}, "contents": {"html": f"<p>text-{cid}</p>"}}

    fake_forum(monkeypatch, {
        "post(input": {"post": {"result": {
            "_id": "p1", "title": "A Post", "postedAt": "2024-01-01T00:00:00Z",
            "user": {"displayName": "Author"}, "coauthors": [{"displayName": "Co"}],
            "contents": {"html": "<h1>Part one</h1><p>Body.</p>"},
        }}},
        "postCommentsTop": {"comments": {"results": [
            comment("root", None, 5), comment("reply", "root", 50), comment("other", None, 20), comment("low", None, 1),
        ]}},
    })
    article = wi.fetch("https://www.lesswrong.com/posts/p1/a-post")
    assert article.byline == "Author, Co · 2024-01-01"
    assert [s.title for s in article.sections] == ["", "Comments"]
    comments = article.sections[1].html
    # The two best comments, plus the parent the best one replies to, threaded.
    assert "text-low" not in comments
    assert comments.index("text-root") < comments.index("text-reply") < comments.index("text-other")
    assert "user-reply · 50 karma · 2024-01-02" in comments

    _, toc = wi.build_html(article)
    assert toc == [(1, "Part one"), (1, "Comments")]


def test_sequence_chapters(monkeypatch):
    post = lambda i: {"_id": f"p{i}", "title": f"Post {i}", "postedAt": "2020-05-05", "user": {"displayName": "EY"},
                      "contents": {"html": f"<p>Post body {i}</p>"}}
    fake_forum(monkeypatch, {"sequence(input": {"sequence": {"result": {
        "title": "A Sequence", "user": {"displayName": "EY"}, "contents": {"html": "<p>About it.</p>"},
        "chapters": [
            {"title": None, "subtitle": None, "contents": None, "posts": [post(1)]},
            {"title": "Part II", "subtitle": "Later", "contents": None, "posts": [post(2)]},
        ],
    }}}})
    article = wi.fetch("https://www.lesswrong.com/s/seq1")
    assert article.intro == "<p>About it.</p>"
    assert [s.title for s in article.sections] == ["Post 1", "Part II", "Post 2"]
    assert article.sections[0].byline == "EY · 2020-05-05"


def test_math_back_to_tex():
    mathjax = ('<p>x <span class="math-tex"><span class="mjpage"><span class="mjx-chtml">'
               '<span class="mjx-math" aria-label="H_1"><span>H</span></span></span></span></span> y</p>'
               '<span class="math-tex"><span class="mjpage mjpage__block"><span class="mjx-chtml MJXc-display">'
               '<span class="mjx-math" aria-label="\\frac{a}{b}"></span></span></span></span>')
    out = wi._clean_forum_html(mathjax, "https://www.lesswrong.com")
    assert '<span class="math" data-tex="H_1"></span> y' in out
    assert 'data-tex="\\frac{a}{b}" data-display' in out

    import lxml.html
    page = lxml.html.fragment_fromstring(
        '<div><span class="katex"><span class="katex-mathml"><math><semantics><mi>x</mi>'
        '<annotation encoding="application/x-tex">x^2</annotation></semantics></math></span>'
        '<span class="katex-html">x2</span></span>'
        '<span class="mwe-math-element"><span style="display:none"><math alttext="{\\displaystyle A}"><mi>A</mi></math>'
        '</span><img class="mwe-math-fallback-image-inline" src="a.svg"></span>'
        '<span class="MathJax_Preview">x</span><script type="math/tex; mode=display">y=1</script></div>'
    )
    wi._normalize_math(page)
    spans = page.xpath('//span[@class="math"]')
    assert [(s.get("data-tex"), s.get("data-display") is not None) for s in spans] == [
        ("x^2", False), ("{\\displaystyle A}", False), ("y=1", True)]
    assert not page.xpath("//img") and not page.xpath('//*[@class="MathJax_Preview"]')


def test_generic_article(monkeypatch):
    para = "<p>" + "A sentence about zorblatts, with commas, that goes on. " * 12 + "</p>"
    page = f"""<html><head><title>Zorblatt Care - Marmalade Weekly</title>
        <meta name="author" content="Pat Q"></head><body>
        <nav><a href="/">Home</a><a href="/about">About</a></nav>
        <article><h1>Zorblatt Care</h1>{para}<p><img src="/img/z.png" srcset="/img/z2.png 2x" loading="lazy"></p>
        <h2>Rinsing<span class="mw-editsection">[edit]</span></h2>{para}<script>track()</script></article>
        <footer>Copyright</footer></body></html>"""
    monkeypatch.setattr(wi, "http_get", lambda url, data=None, headers=None: (page.encode(), "text/html; charset=utf-8"))
    article = wi.fetch("https://zorb.example/care")
    assert article.title == "Zorblatt Care" and article.byline == "Pat Q"
    body = article.sections[0].html
    assert '<img src="https://zorb.example/img/z.png">' in body
    assert "track()" not in body and "[edit]" not in body and "Copyright" not in body


def test_direct_pdf_link(monkeypatch, pdf_path):
    data = open(pdf_path, "rb").read()
    monkeypatch.setattr(wi, "http_get", lambda url, data_=None, headers=None: (data, "application/pdf"))
    result = wi.fetch("https://example.com/papers/zorblatt.pdf")
    assert isinstance(result, wi.Download) and result.ext == "pdf" and result.title == "zorblatt"


def test_web_snapshots_are_found_by_url(tmp_path):
    db = Database(tmp_path / "t.db")
    first = db.upsert_book("h1", "/x/a.pdf", "A", 3)
    db.update_book(first["id"], source_url="https://lesswrong.com/w/a")
    second = db.upsert_book("h2", "/x/a2.pdf", "A", 3)
    db.update_book(second["id"], source_url="https://lesswrong.com/w/a")
    assert [b["path"] for b in db.books_from_url("https://lesswrong.com/w/a")] == ["/x/a2.pdf", "/x/a.pdf"]
    assert db.books_from_url("https://lesswrong.com/w/b") == []


PRINT_SNAPSHOT = """
import sys
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from study_reader import web_import as wi, web_render

app = QApplication([])
article = wi.Article("Zorblatt Wiki", "https://www.lesswrong.com/w/zorblatt", sections=[
    wi.Section("Main", '<p>A zorblatt has <span class="math" data-tex="x^2"></span> legs.</p>'),
    wi.Section("Guide", "<h2>Rinsing</h2><p>Use cold tea.</p>"),
])
wi.fetch = lambda url, status: article
importer = web_render.WebImporter(Path(sys.argv[1]))
loop = QEventLoop()
importer.finished.connect(lambda path, url: (print("OK", path, url, importer.busy), loop.quit()))
importer.failed.connect(lambda message: (print("FAILED", message), loop.quit()))
QTimer.singleShot(60_000, loop.quit)
importer.start("https://www.lesswrong.com/w/zorblatt?lens=guide")
loop.exec()
"""


def test_snapshot_prints_with_contents_and_tex(tmp_path):
    """The real QtWebEngine print: page breaks, a table of contents, TeX in the text layer.
    In its own process, so other tests' windows can't upset QtWebEngine."""
    import os
    import subprocess
    import sys

    from study_reader.document import Document

    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"}
    out = subprocess.run([sys.executable, "-c", PRINT_SNAPSHOT, str(tmp_path / "web")],
                         env=env, capture_output=True, text=True, timeout=120).stdout
    line = next((l for l in out.splitlines() if l.startswith(("OK", "FAILED"))), out)
    assert line.startswith("OK"), line
    _, path, url, busy = line.split(" ")
    assert url == "https://www.lesswrong.com/w/zorblatt" and busy == "False"
    doc = Document(path)
    assert doc.title == "Zorblatt Wiki" and doc.page_count == 2  # each tab starts a page
    assert doc.toc() == [(1, "Main", 0), (1, "Guide", 1), (2, "Rinsing", 1)]
    assert "$x^2$" in doc.page_text(0) and "cold tea" in doc.page_text(1)
    assert not list((tmp_path / "web").glob("*.html"))
