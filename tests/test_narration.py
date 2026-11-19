"""Claude's narration of pages for reading aloud (narration.py) and how reading waits for it."""

from types import SimpleNamespace

import pymupdf
import pytest

from study_reader import narration
from study_reader.db import Database
from study_reader.document import Document
from study_reader.narration import WAIT, NarratedText, parse
from study_reader.speech_pages import BookText

from test_read_aloud import fake_engine  # noqa: F401 - a fixture

PAGES = [
    ["The plot shows the rate.", "0 10 20 30", "A rate of x2 is fast.", "Figure 1: The rate.", "It car-"],
    ["ries on here. The end of it."],
    ["Last page words."],
]
# What Claude says for each page: the tick labels (line 2) left out, the equation in words.
REPLIES = [
    "1| The plot shows the rate.\n3| A rate of x squared is fast.\n4| Figure 1: The rate.\n5| It car- >>",
    "1| >> ries on here. The end of it.",
    "1| Last page words",
]


@pytest.fixture
def pdf(tmp_path):
    doc = pymupdf.open()
    for lines in PAGES:
        page = doc.new_page(width=400, height=500)
        for k, line in enumerate(lines):
            page.insert_text((40, 100 + 60 * k), line, fontsize=12)
    path = tmp_path / "plot.pdf"
    doc.save(path)
    return str(path)


def narrated(pdf, replies=REPLIES, request=None):
    text = NarratedText(BookText(Document(pdf)), request=request)
    for page, reply in enumerate(replies):
        if reply is not None:
            assert text.accept(page, text.key(page), reply)
    return text


def test_parse_reads_ranges_lists_wrapped_lines_and_none():
    assert parse("3-5,9| One.\n[7]| Two\nwraps here.\n40-90| Past the end.", 10) == [
        ([3, 4, 5, 9], "One."), ([7], "Two wraps here."), ([], "Past the end."),
    ]
    assert parse("none", 10) == [] and parse("None.", 10) == []
    assert parse("I can't read this page.", 10) is None
    assert parse("```\n1| Fenced.\n```", 3) == [([1], "Fenced.")]


def test_a_request_holds_the_picture_the_numbered_lines_and_the_previous_pages_end(pdf):
    text = NarratedText(BookText(Document(pdf)))
    image, body = text.blocks(1)
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert "<page>\n1 @10,17| ries on here. The end of it.\n</page>" in body["text"]
    assert "<previous_page_end>" in body["text"] and "It car-" in body["text"]
    assert "previous_page_end" not in text.blocks(0)[1]["text"]
    assert text.blocks(0)[1]["text"].count("\n") == 6  # five lines between the tags


def test_narration_is_read_in_claudes_order_with_sentences_joined_across_pages(pdf):
    said = [u.text for u in narrated(pdf).all()]
    assert said == [
        "The plot shows the rate.", "A rate of x squared is fast.", "Figure 1: The rate.",
        "It carries on here.", "The end of it.", "Last page words.",
    ]


def test_words_light_up_where_they_are_printed_and_rewritten_ones_what_they_replace(pdf):
    text = narrated(pdf)
    doc = text.book.doc
    u = text.script(0).items[1]
    assert u.text == "A rate of x squared is fast."
    words = {w.text: (w.x0, w.y0, w.x1, w.y1) for w in doc.words(0)}
    assert u.word_at(u.text.index("rate")) == {0: [words["rate"]]}
    assert u.word_at(u.text.index("squared")) == {0: [words["x2"]]}  # "x squared" stands for "x2"
    assert u.word_at(u.text.index("fast")) == {0: [words["fast."]]}
    # The sentence's highlight is its own line, not the tick labels above it.
    (rect,) = u.rects[0]
    assert rect[1] > words["30"][3]
    # The sentence cut by the page break covers both pages.
    joined = list(text.all())[3]
    assert set(joined.rects) == {0, 1} and joined.word_at(joined.text.index("here")) == {
        1: [next((w.x0, w.y0, w.x1, w.y1) for w in doc.words(1) if w.text == "here.")]}


def test_reading_waits_for_a_page_and_asks_ahead(pdf):
    asked = []
    text = narrated(pdf, [None, None, None], request=asked.append)
    source = text.from_view(0, 0)
    assert next(source) is WAIT and asked == [0]
    assert next(source) is WAIT and asked == [0, 0]  # the caller keeps what's in flight to one
    text.accept(0, text.key(0), REPLIES[0])
    assert next(source).text == "The plot shows the rate."
    assert asked[2:] == [1, 2]
    next(source), next(source)
    assert next(source) is WAIT  # the last sentence runs on to a page that isn't here
    text.fail(1)  # read as extracted
    assert next(source).text == "It carries on here."


def test_a_failed_page_is_read_as_extracted_and_a_bad_reply_is_refused(pdf):
    text = narrated(pdf, [None, REPLIES[1], REPLIES[2]], request=lambda page: None)
    assert not text.accept(0, text.key(0), "Sorry, I can't.")
    assert not text.accept(1, "another key", REPLIES[1])
    assert [u.text for u in text.script(0).items][:2] == ["The plot shows the rate.", "0 10 20 30"]


def test_starting_from_the_view_or_a_word_and_going_back(pdf):
    text = narrated(pdf)
    doc = text.book.doc
    y = next(w for w in doc.words(0) if w.text == "x2").y0
    assert next(text.from_view(0, y)).text == "A rate of x squared is fast."
    assert next(text.from_view(0, 480)).text == "It carries on here."  # nothing below: the next page
    ticks = next(i for i, w in enumerate(doc.words(0)) if w.text == "20")
    assert next(text.from_word(0, ticks)).text == "A rate of x squared is fast."  # a label: what's below
    here = next(i for i, w in enumerate(doc.words(1)) if w.text == "here.")
    start = next(text.from_word(1, here))
    assert start.text == "It carries on here."  # the whole sentence, from the page before
    assert text.before(start).text == "Figure 1: The rate."
    end = list(text.all())[4]
    assert text.before(end).text == "It carries on here."
    assert text.before(next(text.all())) is None


def test_stored_narration_is_dropped_when_the_page_text_changed(pdf):
    text = NarratedText(BookText(Document(pdf)), {0: ("stale", REPLIES[0])})
    assert text.script(0).items[1].text == "0 10 20 30"  # no request: as extracted


def test_database_keeps_narrations_until_the_book_goes(tmp_path, pdf):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h1", pdf, "Plot", 3)["id"]
    db.set_narration("h1", 0, "k", "1| One.")
    db.set_narration("h1", 0, "k2", "1| Two.")
    assert db.narrations("h1") == {0: ("k2", "1| Two.")} and db.narrations("h2") == {}
    db.delete_book(book)
    assert db.narrations("h1") == {}


def test_render_export_uses_the_narration(pdf):
    from study_reader.render_dialog import book_sentences

    text = narrated(pdf)
    seen = []
    said = book_sentences(pdf, lambda p, n: seen.append((p, n)), dict(text.texts))
    assert said[1] == "A rate of x squared is fast." and "It carries on here." in said
    assert seen[0] == (0, 3)
    assert book_sentences(pdf)[1] == "0 10 20 30"


# -- the controller -------------------------------------------------------------------------

class FakeWorker:
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class Signals(QObject):
            narrated = Signal(str, int, str, str)
            narrationFailed = Signal(str, int, str)

        self._signals = Signals()
        self.narrated, self.narrationFailed = self._signals.narrated, self._signals.narrationFailed
        self.asked, self.stopped = [], 0

    def submit_narration(self, book, page, key, blocks):
        self.asked.append((book, page, key))

    def stop_narrating(self):
        self.stopped += 1

    def answer(self, n=0, reply=None):
        book, page, key = self.asked.pop(n)
        self.narrated.emit(book, page, key, REPLIES[page] if reply is None else reply)
        return page


@pytest.fixture
def reading(pdf, tmp_path, monkeypatch, fake_engine):  # noqa: F811
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.read_aloud import ReadAloud
    from study_reader.reader_view import ReaderView

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    db.set_setting(narration.ENABLED_SETTING, "1")
    view = ReaderView()
    view.resize(500, 600)
    view.show()
    doc = Document(pdf)
    view.set_document(doc)
    worker = FakeWorker()
    ctl = ReadAloud(db, view, worker=worker)
    ctl.set_document(doc, file_hash="h1")
    return SimpleNamespace(app=app, db=db, view=view, doc=doc, ctl=ctl, worker=worker, engines=fake_engine)


def test_reading_starts_once_the_page_is_narrated_and_keeps_the_reply(reading):
    ctl, worker = reading.ctl, reading.worker
    ctl.start_from_view()
    engine = reading.engines[-1]
    assert ctl.active and engine.queue == [] and [a[1] for a in worker.asked] == [0]
    assert ctl.bar.status.text() == "Preparing the narration…"
    worker.answer()
    assert [t for _, t in engine.queue] == ["The plot shows the rate.", "A rate of x squared is fast."]
    assert ctl.bar.status.text() == "" and reading.view._speech  # the first sentence is shown
    assert [a[1] for a in worker.asked] == [1, 2]
    assert 0 in reading.db.narrations("h1")
    # Reading reaches the sentence that runs on to page 2 before page 2 is here, and waits.
    engine.play_one(), engine.play_one()
    assert engine.play_one() == "Figure 1: The rate." and engine.queue == [] and ctl.active
    worker.answer()
    assert [t for _, t in engine.queue] == ["It carries on here.", "The end of it."]
    worker.answer()
    while engine.queue:
        engine.play_one()
    assert not ctl.active


def test_a_stored_narration_needs_no_request(reading):
    ctl, worker = reading.ctl, reading.worker
    text = ctl.narrator.text
    for page, reply in enumerate(REPLIES):
        reading.db.set_narration("h1", page, text.key(page), reply)
    ctl.set_document(reading.doc, file_hash="h1")
    ctl.start_from_view()
    assert worker.asked == [] and reading.engines[-1].queue[0][1] == "The plot shows the rate."


def test_a_page_claude_fails_on_is_read_as_extracted(reading):
    ctl, worker = reading.ctl, reading.worker
    ctl.start_from_view()
    book, page, _ = worker.asked.pop(0)
    worker.narrationFailed.emit(book, page, "offline")
    engine = reading.engines[-1]
    assert engine.queue[0][1] == "The plot shows the rate." and engine.queue[1][1] == "0 10 20 30"
    assert "offline" in ctl.bar.status.text()
    assert reading.db.narrations("h1") == {}


def test_turning_narration_off_while_reading_carries_on_from_the_sentence(reading):
    ctl, worker = reading.ctl, reading.worker
    ctl.start_from_view()
    worker.answer()
    engine = reading.engines[-1]
    engine.play_one()
    engine.started.emit(engine.queue[0][0])  # now saying "A rate of x squared is fast."
    ctl.set_narration(False)
    assert reading.db.get_setting(narration.ENABLED_SETTING) == "0" and not ctl.bar.narrate.isChecked()
    assert engine.queue[0][1] == "A rate of x2 is fast."
    ctl.set_narration(True)
    assert engine.queue[0][1] == "A rate of x squared is fast."


def test_clicking_and_previous_follow_the_narration(reading):
    ctl, worker = reading.ctl, reading.worker
    ctl.start_from_view()
    for _ in range(3):
        worker.answer()
    engine = reading.engines[-1]
    w = next(w for w in reading.doc.words(1) if w.text == "end")
    ctl.jump_to(1, w.x0 + 1, w.y0 + 1)
    assert engine.queue[0][1] == "The end of it."
    ctl.prev_sentence()
    assert engine.queue[0][1] == "It carries on here."
    ctl.prev_sentence()
    assert engine.queue[0][1] == "Figure 1: The rate."


def test_preparing_the_whole_book_asks_a_few_pages_at_a_time(reading, monkeypatch):
    ctl, worker = reading.ctl, reading.worker
    narrator = ctl.narrator
    monkeypatch.setattr(narrator, "AT_ONCE", 2)
    progress, done = [], []
    narrator.progress.connect(lambda d, n: progress.append((d, n)))
    narrator.prepared.connect(done.append)
    narrator.prepare()
    for _ in range(5):
        reading.app.processEvents()
    assert [a[1] for a in worker.asked] == [0, 1] and narrator.preparing
    worker.answer()
    for _ in range(5):
        reading.app.processEvents()
    assert [a[1] for a in worker.asked] == [1, 2]
    worker.answer(), worker.answer()
    for _ in range(5):
        reading.app.processEvents()
    assert done == [True] and progress[-1] == (3, 3) and len(reading.db.narrations("h1")) == 3
    # Nothing is left to ask for the second time round.
    narrator.prepare()
    for _ in range(8):
        reading.app.processEvents()
    assert done == [True, True] and worker.asked == []


def test_preparing_gives_up_after_failures_in_a_row_and_stops_with_the_book(reading, monkeypatch):
    ctl, worker = reading.ctl, reading.worker
    narrator = ctl.narrator
    monkeypatch.setattr(narrator, "GIVE_UP", 2)
    done = []
    narrator.prepared.connect(done.append)
    narrator.prepare()
    for _ in range(8):
        reading.app.processEvents()
    for _ in range(2):
        book, page, _ = worker.asked.pop(0)
        worker.narrationFailed.emit(book, page, "offline")
    assert done == [False] and not narrator.preparing and narrator.gave_up
    narrator.prepare()
    reading.app.processEvents()
    ctl.set_document(None)
    assert done == [False, False] and worker.stopped == 1 and narrator.text is None


def test_the_service_narrates_in_a_session_it_deletes(tmp_path, monkeypatch):
    import asyncio

    from study_reader import chat_service, claude_client as cc

    calls, forgotten, events = [], [], []

    async def run_turn(blocks, **kw):
        calls.append((blocks, kw))
        if blocks == ["bad"]:
            raise cc.ClaudeError("no login")
        return cc.TurnResult(session_id="s1", text="1| Hello.", usage={})

    monkeypatch.setattr(cc, "run_turn", run_turn)
    monkeypatch.setattr(cc, "forget_session", lambda sid, cwd: forgotten.append((sid, cwd)))
    service = chat_service.ChatService(lambda *a: events.append(a), tmp_path)
    asyncio.run(service.narrate("h1", 4, "k", ["page"]))
    asyncio.run(service.narrate("h1", 5, "k", ["bad"]))
    assert events == [("narrated", "h1", 4, "k", "1| Hello."), ("narrationFailed", "h1", 5, "no login")]
    assert calls[0][1] == {"cwd": tmp_path / "narration", "model": narration.MODEL,
                           "system_prompt": narration.SYSTEM_PROMPT, "thinking": False}
    assert forgotten == [("s1", tmp_path / "narration")]


def test_main_window_narrates_the_whole_book(pdf, tmp_path, monkeypatch, fake_engine):  # noqa: F811
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    db.set_setting(narration.ENABLED_SETTING, "1")
    win = MainWindow(db)
    asked = []
    monkeypatch.setattr(win.worker, "submit_narration", lambda *a: asked.append(a))
    win.show()
    win.open_book(pdf)
    narrator = win.read_aloud.narrator
    win.narrate_book()
    for _ in range(10):
        app.processEvents()
    assert [a[1] for a in asked] == [0, 1, 2] and win._narration_progress is not None
    for book, page, key, blocks in asked:
        win.worker.narrated.emit(book, page, key, REPLIES[page])
    for _ in range(10):
        app.processEvents()
    assert win._narration_progress is None and not narrator.preparing
    assert len(db.narrations(db.book(win.book_id)["hash"])) == 3
    # Reading aloud now starts at once, from Claude's text.
    win.read_action.trigger()
    assert fake_engine[-1].queue[0][1] == "The plot shows the rate."
    assert win.read_aloud.bar.narrate.isChecked()
    win.read_aloud.bar.narrate.click()
    assert db.get_setting(narration.ENABLED_SETTING) == "0"
    win.narrate_book()  # stopping it closes the box
    win._narration_progress.canceled.emit()
    assert win._narration_progress is None
    win.close()
