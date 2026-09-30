import asyncio

import pytest

from study_reader import chat_service as cs
from study_reader import claude_client as cc
from study_reader.db import Database
from study_reader.document import Document, Word, file_hash, line_rects, nearest_word, select_range, words_text


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
    assert [b["type"] for b in blocks] == ["text", "text", "text", "image", "text"]
    assert blocks[0]["text"] == "ctx" and "page 5" in blocks[1]["text"] and blocks[-1]["text"] == "Why?"
    assert blocks[3]["source"]["media_type"] == "image/png"


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
        monkeypatch.setattr(cc, "run_turn", self.run_turn)
        monkeypatch.setattr(cc, "session_exists", lambda sid, cwd: sid in self.sessions)

    async def run_turn(self, blocks, *, cwd, model=None, resume=None, fork=False, on_delta=None):
        text = "\n".join(b.get("text", "") for b in blocks)
        self.calls.append({"text": text, "resume": resume, "fork": fork})
        if "<book>" in text and self.fail_priming:
            raise cc.ClaudeError(self.fail_priming)
        self.n += 1
        sid = resume if resume and not fork else f"s{self.n}"
        self.sessions.add(sid)
        if on_delta:
            on_delta("ans")
        return cc.TurnResult(sid, "answer")


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
    assert ("base", 1, "s1", "full") in events and ("done", 1, "s2", "answer") in events
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
    assert fake.calls == [{"text": "Q?", "resume": "chat", "fork": False}]


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
