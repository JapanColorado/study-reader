import asyncio

import pytest

from study_reader import chat_service as cs
from study_reader import claude_client as cc
from study_reader.db import Database
from study_reader.document import Document, Word, file_hash, line_rects, nearest_word, select_range, words_text
from study_reader.theme import darken_page


# -- db ---------------------------------------------------------------------------
def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h1", "/a.pdf", "A", 10)
    assert db.upsert_book("h1", "/moved/a.pdf", "A", 10)["id"] == book["id"]  # same file, new path
    assert db.book(book["id"])["path"] == "/moved/a.pdf"

    chat = db.create_chat(book["id"], "Q")
    db.add_message(chat, "user", "why?", [{"kind": "text", "page": 3, "text": "quote", "rects": [[1, 2, 3, 4]]}])
    db.add_message(chat, "assistant", "because")
    msgs = db.messages(chat)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    snip = msgs[0]["snippets"][0]
    assert snip["page"] == 3 and snip["rects"] == [[1, 2, 3, 4]]
    assert db.snippet(snip["id"])["text"] == "quote"

    db.update_book(book["id"], base_session_id="s", context_mode="full", last_page=4)
    assert db.book(book["id"])["context_mode"] == "full"
    db.set_setting("model", "opus")
    assert db.get_setting("model") == "opus" and db.get_setting("nope", "x") == "x"

    db.delete_book(book["id"])
    assert db.chats(book["id"]) == [] and db.messages(chat) == []


def test_truncating_a_chat(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h1", "/a.pdf", "A", 10)
    chat = db.create_chat(book["id"], "Q")
    db.add_message(chat, "user", "q1")
    db.add_message(chat, "assistant", "a1", session_id="s", session_uuid="u")
    q2 = db.add_message(chat, "user", "q2", [{"kind": "text", "page": 1, "text": "x"}])
    db.add_message(chat, "assistant", "a2")
    db.delete_messages_from(chat, q2)
    msgs = db.messages(chat)
    assert [m["text"] for m in msgs] == ["q1", "a1"] and (msgs[1]["session_id"], msgs[1]["session_uuid"]) == ("s", "u")
    assert db.book_snippets(book["id"]) == []


def test_book_snippets_and_chat_model(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h1", "/a.pdf", "A", 10)
    other = db.upsert_book("h2", "/b.pdf", "B", 10)
    a = db.create_chat(book["id"], "first", "opus")
    b = db.create_chat(book["id"], "second")
    db.add_message(a, "user", "q", [{"kind": "text", "page": 1, "text": "x", "rects": [[0, 0, 5, 5]]}])
    db.add_message(b, "user", "q", [{"kind": "image", "page": 2, "image_path": "/i.png", "rects": [[1, 1, 9, 9]]}])
    db.add_message(db.create_chat(other["id"], "elsewhere"), "user", "q", [{"kind": "text", "page": 0, "text": "y"}])
    snips = db.book_snippets(book["id"])
    assert [(s["chat_id"], s["page"], s["rects"]) for s in snips] == [(a, 1, [[0, 0, 5, 5]]), (b, 2, [[1, 1, 9, 9]])]
    assert db.chat(a)["model"] == "opus" and db.chat(b)["model"] == ""
    db.update_chat(b, title="renamed", model="haiku")
    assert (db.chat(b)["title"], db.chat(b)["model"]) == ("renamed", "haiku")
    db.delete_chat(a)
    assert [s["chat_id"] for s in db.book_snippets(book["id"])] == [b]


def test_old_database_gets_chat_model_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0, last_page INTEGER NOT NULL DEFAULT 0, zoom REAL NOT NULL DEFAULT 0,
            base_session_id TEXT, context_mode TEXT, opened_at REAL NOT NULL);
        CREATE TABLE chats (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL, title TEXT NOT NULL, session_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, role TEXT NOT NULL,
            text TEXT NOT NULL, created_at REAL NOT NULL);
        INSERT INTO books VALUES (1, 'h', '/a.pdf', 'A', 1, 0, 0, NULL, NULL, 0);
        INSERT INTO chats VALUES (1, 1, 'old chat', NULL, 0, 0);
        INSERT INTO settings VALUES ('model', 'sonnet');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.chat(1)["model"] == "sonnet"  # keeps the model it was used with
    db.add_message(1, "assistant", "a", session_id="s", session_uuid="u")  # messages gained branch points
    assert db.book(1)["crop_margins"] == 0
    assert (db.book(1)["show_toc"], db.book(1)["right_panel"]) == (1, "chat")


# -- document ---------------------------------------------------------------------
def test_pdf_document(pdf_path):
    doc = Document(pdf_path)
    assert doc.page_count == 3 and doc.title == "The Zorblatt Manual"
    assert doc.toc() == [(1, "Chapter 1", 0), (1, "Chapter 2", 1), (1, "Chapter 3", 2)]
    text = doc.full_text()
    assert "[[page 2]]" in text and "41 seconds" in text
    assert doc.render(0, 2.0).width == 800
    assert doc.render(0, 2.0, clip=(0, 0, 100, 50)).height == 100
    assert len(file_hash(pdf_path)) == 64


def test_content_box_hugs_the_text(pdf_path):
    x0, y0, x1, y1 = Document(pdf_path).content_box()
    # Text sits in (40, 40, 360, 260) on a 400x300 page; the short lines leave the right side empty.
    assert 30 < x0 < 50 and 30 < y0 < 55 and x1 < 360 and y1 < 260


def test_reader_crop_maps_page_points(pdf_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication
    from study_reader.reader_view import ReaderView

    app = QApplication.instance() or QApplication([])
    view = ReaderView()
    view.resize(600, 120)  # shorter than a cropped page, so every page stays scrollable
    view.show()
    view.set_document(Document(pdf_path), 1)
    full_zoom = view.zoom
    view.set_crop(True)
    assert view.crop and view.fit_mode and view.zoom > full_zoom
    assert view.current_page() == 1
    x0, y0, x1, y1 = view._clips[1]
    rect = view._page_rect(1)
    assert rect.x() == 0 and abs(rect.width() - view.viewport().width()) < 1  # no side margins
    page, x, y = view._hit(rect.topLeft() + QPointF(10 * view.zoom, 10 * view.zoom))
    assert page == 1 and abs(x - (x0 + 10)) < 1e-6 and abs(y - (y0 + 10)) < 1e-6
    assert view._to_view(1, rect, (x, y, x + 5, y + 5)).topLeft() == rect.topLeft() + QPointF(10 * view.zoom, 10 * view.zoom)
    view.set_crop(False)
    assert view._clips[1] == (0.0, 0.0, 400.0, 300.0) and view.current_page() == 1


def test_asking_highlights_and_shows_chats(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Database(tmp_path / "t.db"))
    win.show()
    win.open_book(pdf_path)
    win.calc_action.trigger()  # the calculator has the right-hand slot
    assert win.right.currentWidget() is win.calc

    words = win.doc.words(0)
    win.reader._selection = {0: words[:3]}
    win.reader.choose(True)
    assert win.chat_action.isChecked() and win.right.currentWidget() is win.chat
    assert [m["kind"] for m in win.reader._marks[0]] == ["text"]  # marked before anything is sent

    win.chat._remove_pending(win.chat.pending_snippets()[0])
    assert not win.reader._marks


def test_sidebars_stay_collapsed_when_book_is_reopened(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Database(tmp_path / "t.db"))
    win.show()
    win.open_book(pdf_path)
    assert win.chat_action.isChecked() and win.right.isVisible()  # new books open with chats
    win.toc_action.setEnabled(True)  # the test PDF may have no contents to show
    win.toc_action.trigger()
    win.chat_action.trigger()
    assert not win.right.isVisible()

    win.show_library()
    win.open_book(pdf_path)
    assert not win.toc_action.isChecked() and not win.sidebar.isVisible()
    assert not win.chat_action.isChecked() and not win.calc_action.isChecked() and not win.right.isVisible()

    win.calc_action.trigger()
    win.show_library()
    win.open_book(pdf_path)
    assert win.calc_action.isChecked() and win.right.currentWidget() is win.calc and win.right.isVisible()


def test_edit_and_regenerate_branch_the_chat(chat_panel):
    panel, worker, db, book_id = chat_panel

    def ask(text):
        panel.input.setPlainText(text)
        panel.send()
        n = len(worker.jobs)
        worker.done.emit(panel.chat_id, f"s{n}", f"a{n}", f"u{n}")

    panel.add_snippet({"kind": "text", "page": 1, "text": "quote", "rects": []}, new_chat=True)
    ask("q1")
    ask("q2")
    chat = panel.chat_id
    assert worker.jobs[1].chat_session_id == "s1" and worker.jobs[1].resume_at is None
    q1, a1, q2, a2 = db.messages(chat)

    # Regenerate the last reply: forks at the reply before its question.
    panel.regenerate(a2["id"])
    job = worker.jobs[-1]
    assert (job.question, job.chat_session_id, job.resume_at) == ("q2", "s1", "u1")
    assert job.history == [("user", "q1"), ("assistant", "a1")]
    assert [m["text"] for m in db.messages(chat)] == ["q1", "a1", "q2"] and db.chat(chat)["session_id"] is None
    worker.done.emit(chat, "s3", "a3", "u3")

    # Edit the first question: its snippet comes along, the draft is set aside, and sending starts over.
    panel.input.setPlainText("draft")
    panel.edit_message(q1["id"])
    assert panel.input.toPlainText() == "q1" and len(panel._pending) == 1 and panel.pending_snippets() == []
    panel.input.setPlainText("q1 again")
    panel.send()
    job = worker.jobs[-1]
    assert (job.question, job.chat_session_id, job.resume_at, job.history) == ("q1 again", None, None, [])
    assert [s.text for s in job.snippets] == ["quote"]
    msgs = db.messages(chat)
    assert [m["text"] for m in msgs] == ["q1 again"] and msgs[0]["snippets"][0]["text"] == "quote"
    assert panel.input.toPlainText() == "draft" and panel._editing is None and not panel.edit_bar.isVisibleTo(panel)

    # Escape leaves editing without changing anything.
    worker.done.emit(chat, "s5", "a5", "u5")
    panel.edit_message(msgs[0]["id"])
    panel.cancel_edit()
    assert panel.input.toPlainText() == "draft" and len(db.messages(chat)) == 2


def test_transcript_navigation_routes_links_and_confines_visualizations(monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from study_reader import transcript_view as tv

    opened = []
    monkeypatch.setattr(tv.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    emitted = []
    page = SimpleNamespace(**{
        name: SimpleNamespace(emit=lambda n, name=name: emitted.append((name, n)))
        for name in ("jump", "openViz", "edit", "regenerate", "page")
    })
    nav = tv._Page.acceptNavigationRequest
    Nav = QWebEnginePage.NavigationType
    clicked, scripted = Nav.NavigationTypeLinkClicked, Nav.NavigationTypeOther

    assert not nav(page, QUrl("jump://snippet/7"), clicked, True)
    assert not nav(page, QUrl("viz://open/2"), clicked, True)
    assert not nav(page, QUrl("edit://message/3"), clicked, True)
    assert not nav(page, QUrl("regen://message/4"), clicked, True)
    assert not nav(page, QUrl("page://go/41"), clicked, True)
    assert emitted == [("jump", 7), ("openViz", 2), ("edit", 3), ("regenerate", 4), ("page", 41)]
    # Inside a visualization's frame: its srcdoc loads, nothing else navigates or reaches the app.
    assert nav(page, QUrl("about:srcdoc"), scripted, False)
    assert not nav(page, QUrl("viz://open/0"), clicked, False)
    assert not nav(page, QUrl("https://example.com/leak"), scripted, False)
    assert not nav(page, QUrl("https://example.com/doc"), clicked, False)
    assert len(emitted) == 5 and opened == ["https://example.com/doc"]


def test_epub_document(epub_path):
    doc = Document(epub_path)
    assert doc.page_count >= 1 and doc.title == "Zorblatt EPUB"
    assert "marmalade" in doc.full_text()
    assert doc.words(0)


def test_text_window(pdf_path):
    doc = Document(pdf_path)
    assert doc.text_window(1, 10).startswith("[[page 2]]")
    assert doc.text_window(1, 10).count("[[page") == 1
    assert doc.text_window(1, 10_000).count("[[page") == 3


def test_selection_within_page(pdf_path):
    doc = Document(pdf_path)
    words = doc.words(0)
    a = next(w for w in words if w.text == "zorblatt")
    b = next(w for w in words if w.text == "device")
    forward = select_range(doc, (0, a.x0 + 1, a.y0 + 1), (0, b.x1 - 1, b.y1 - 1))
    backward = select_range(doc, (0, b.x1 - 1, b.y1 - 1), (0, a.x0 + 1, a.y0 + 1))
    assert words_text(forward[0]) == "zorblatt is a seven-legged device" == words_text(backward[0])


def test_selection_across_pages(pdf_path):
    doc = Document(pdf_path)
    last = doc.words(0)[-1]
    sel = select_range(doc, (0, last.x0 + 1, last.y0 + 1), (2, 45, 45))
    assert sorted(sel) == [0, 1, 2]
    assert words_text(sel[0]) == "marmalade."
    assert "41 seconds" in words_text(sel[1])
    assert words_text(sel[2]) == "Chapter"


def test_geometry_helpers():
    ws = [
        Word(0, 0, 10, 10, "hyphen-", 0, 0), Word(0, 12, 10, 22, "ated", 0, 1),
        Word(12, 12, 30, 22, "word", 0, 1), Word(0, 40, 10, 50, "Next", 1, 0),
    ]
    assert words_text(ws) == "hyphenated word\nNext"
    assert line_rects(ws) == [(0, 0, 10, 10), (0, 12, 30, 22), (0, 40, 10, 50)]
    assert nearest_word(ws, 200, 15) == 2  # right margin picks the word on that line
    assert nearest_word([], 0, 0) is None


# -- claude client ------------------------------------------------------------------
def test_build_blocks(tmp_path):
    img = tmp_path / "s.png"
    img.write_bytes(b"\x89PNG")
    blocks = cc.build_blocks(
        "Why?", [cc.Snippet("text", 4, "a passage"), cc.Snippet("image", 0, image_path=str(img))], "ctx"
    )
    assert [b["type"] for b in blocks] == ["text", "text", "text", "text", "image", "text"]
    assert blocks[0]["text"] == cc.READER_INSTRUCTIONS  # every turn: resumed sessions keep their system prompt
    assert blocks[1]["text"] == "ctx" and "page 5" in blocks[2]["text"] and blocks[-1]["text"] == "Why?"
    assert blocks[4]["source"]["media_type"] == "image/png"


def test_api_key_is_dropped_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cc._prefer_subscription_login()
    assert "ANTHROPIC_API_KEY" not in cc.os.environ
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("STUDY_READER_USE_API_KEY", "1")
    cc._prefer_subscription_login()
    assert cc.os.environ["ANTHROPIC_API_KEY"] == "k"


# -- chat service ---------------------------------------------------------------------
class FakeClaude:
    """Stands in for the SDK: records calls and tracks which sessions exist."""

    def __init__(self, monkeypatch, fail_priming: str | None = None):
        self.calls, self.sessions, self.n, self.fail_priming = [], set(), 0, fail_priming
        self.uuids = set()  # reply uuids that can be branched from
        monkeypatch.setattr(cc, "run_turn", self.run_turn)
        monkeypatch.setattr(cc, "session_exists", lambda sid, cwd: sid in self.sessions)

    async def run_turn(self, blocks, *, cwd, model=None, resume=None, fork=False, resume_at=None, on_delta=None):
        text = "\n".join(b.get("text", "") for b in blocks if b.get("text") != cc.READER_INSTRUCTIONS)
        self.calls.append({"text": text, "resume": resume, "fork": fork, "resume_at": resume_at})
        if "<book>" in text and self.fail_priming:
            raise cc.ClaudeError(self.fail_priming)
        if resume_at and resume_at not in self.uuids:
            raise cc.ClaudeError(f"Message {resume_at} not found")
        self.n += 1
        sid = resume if resume and not fork else f"s{self.n}"
        self.sessions.add(sid)
        self.uuids.add(f"u{self.n}")
        if on_delta:
            on_delta("ans")
        return cc.TurnResult(sid, "answer", message_uuid=f"u{self.n}")


def run_job(service, events, **kw):
    asyncio.run(service.ask(cs.Job(question="Q?", **kw)))
    return events


@pytest.fixture
def service(tmp_path):
    events = []
    return cs.ChatService(lambda *a: events.append(a), tmp_path), events


def book(pdf_path, base=None, mode=None):
    return cs.BookRef(1, pdf_path, "Zorblatt", base, mode)


def test_first_chat_primes_then_forks(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path), chat_session_id=None)
    assert "<book>" in fake.calls[0]["text"] and "41 seconds" in fake.calls[0]["text"]
    assert fake.calls[1]["resume"] == "s1" and fake.calls[1]["fork"] is True
    assert ("base", 1, "s1", "full") in events and ("done", 1, "s2", "answer", "u2") in events
    assert ("delta", 1, "ans") in events

    # A second chat reuses the base session without priming again, even with stale book data.
    run_job(svc, events, chat_id=2, book=book(pdf_path), chat_session_id=None)
    assert len(fake.calls) == 3 and fake.calls[2]["resume"] == "s1" and fake.calls[2]["fork"] is True


def test_follow_up_resumes_own_session(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    fake.sessions |= {"base", "chat"}
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="chat",
            history=[("user", "old q"), ("assistant", "old a")])
    assert fake.calls == [{"text": "Q?", "resume": "chat", "fork": False, "resume_at": None}]


def test_branching_forks_at_an_earlier_reply(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    fake.sessions |= {"base", "chat"}
    fake.uuids.add("reply-1")
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="chat",
            resume_at="reply-1", history=[("user", "old q"), ("assistant", "old a")])
    assert fake.calls == [{"text": "Q?", "resume": "chat", "fork": True, "resume_at": "reply-1"}]
    assert events[-1] == ("done", 1, "s1", "answer", "u1")


def test_unloadable_branch_point_replays_history(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    fake.sessions |= {"base", "chat"}
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="chat",
            resume_at="pruned", history=[("user", "old q"), ("assistant", "old a")])
    assert [(c["resume"], c["fork"], c["resume_at"]) for c in fake.calls] == [("chat", True, "pruned"), ("base", True, None)]
    assert "Me: old q" in fake.calls[1]["text"] and events[-1][0] == "done"


def test_lost_chat_session_replays_history(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    fake.sessions.add("base")
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="gone",
            history=[("user", "old q"), ("assistant", "old a")])
    (call,) = fake.calls
    assert call["resume"] == "base" and call["fork"] is True
    assert "Me: old q" in call["text"] and "You: old a" in call["text"]


def test_lost_base_session_is_reprimed(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path, "pruned", "full"), chat_session_id=None)
    assert "<book>" in fake.calls[0]["text"] and fake.calls[1]["resume"] == "s1"


def test_oversized_book_uses_nearby_pages(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch)
    monkeypatch.setattr(cc, "MAX_BOOK_TOKENS", 5)
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path), chat_session_id=None, page=1)
    (call,) = fake.calls
    assert call["resume"] is None and call["fork"] is False
    assert "<book_excerpt>" in call["text"] and "41 seconds" in call["text"]
    assert ("base", 1, "", "partial") in events


def test_priming_too_long_falls_back(monkeypatch, service, pdf_path):
    fake = FakeClaude(monkeypatch, fail_priming="Prompt is too long")
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path), chat_session_id=None)
    assert ("base", 1, "", "partial") in events and events[-1][0] == "done"
    assert "<book_excerpt>" in fake.calls[-1]["text"]


def test_other_failures_are_reported(monkeypatch, service, pdf_path):
    FakeClaude(monkeypatch, fail_priming="not logged in")
    svc, events = service
    run_job(svc, events, chat_id=1, book=book(pdf_path), chat_session_id=None)
    assert events[-1] == ("failed", 1, "not logged in")
    assert not any(e[0] == "base" for e in events)


# -- theme ------------------------------------------------------------------------
def test_darken_page_keeps_hue():
    # white, black, a blue and an orange; stride padded to check it is honored
    pixels = bytes([255, 255, 255, 0, 0, 0, 51, 119, 230, 242, 153, 51]) + b"\0\0"
    out = darken_page(pixels, 4, 1, 14, "#201e1c", "#dcd7ce")
    px = [tuple(out[i:i + 3]) for i in range(0, 12, 3)]
    assert px[0] == (0x20, 0x1E, 0x1C) and px[1] == (0xDC, 0xD7, 0xCE)
    assert px[2][2] > px[2][0] and px[3][0] > px[3][2]  # blue stays blue, orange stays orange
