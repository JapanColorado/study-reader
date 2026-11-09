from types import SimpleNamespace

import numpy as np
import pymupdf
import pytest

from study_reader import tts_engines
from study_reader.db import Database
from study_reader.tts_engines import PcmQueue, synthesize, to_format


# -- engine pieces (no Qt, no audio) -------------------------------------------------------

def test_pcm_queue_plays_segments_back_to_back_and_reports_words():
    q = PcmQueue(bytes_per_second=100, frame=1, hold=False)
    q.append(1, b"a" * 50, [(0.0, 0.2, 0, 3), (0.2, 0.5, 4, 9)])
    q.append(2, b"b" * 30, [])
    assert q.events(0) == []  # nothing handed out yet
    assert q.read(60) == b"a" * 50 + b"b" * 10
    assert q.events(0) == [("started", 1), ("word", 1, 0)]
    assert q.events(10) == []
    assert q.events(25) == [("word", 1, 4)]
    assert q.events(55) == [("finished", 1), ("started", 2)]  # 2 hasn't been fully handed out
    assert q.read(40) == b"b" * 20 + bytes(20)  # then silence
    assert q.events(80) == [("finished", 2)] and q.segs == []


def test_pcm_queue_keeps_only_the_segment_playing_when_dropping_the_rest():
    q = PcmQueue(bytes_per_second=100, frame=1, hold=False)
    q.append(1, b"a" * 50, [])
    q.append(2, b"b" * 50, [])
    q.read(10)
    q.drop_unstarted()
    assert [s.uid for s in q.segs] == [1]
    q.reset()
    assert q.segs == [] and q.total == 0 and q.hold


def test_pcm_queue_waits_for_a_buffer_before_playing():
    q = PcmQueue(bytes_per_second=100, frame=1)
    q.append(1, b"a" * 100, [])  # 1 s ready, more coming
    q.settle(waiting=2, buffer=3)
    assert q.read(10) == bytes(10) and q.events(10) == []
    q.append(2, b"b" * 200, [])
    q.settle(waiting=1, buffer=3)  # 3 s ready
    assert q.read(10) == b"a" * 10
    q.read(290)
    q.settle(waiting=1, buffer=3)  # ran dry with more coming: hold again
    assert q.hold and q.read(5) == bytes(5)
    q.append(3, b"c" * 50, [])
    q.settle(waiting=0, buffer=3)  # nothing more coming: play what there is
    assert q.read(5) == b"c" * 5


def test_a_long_sentence_splits_at_a_comma_keeping_its_highlights():
    from study_reader.speech_text import Utterance

    text = "Once the zorblatt has been rinsed, it should be left to dry in a cool, dark place."
    marks = [(i, i + len(w), {0: [(i, 0, i + 1, 1)]}) for i, w in _spans(text)]
    u = Utterance(text, (0, 1), (0, 9), {0: [(0, 0, 9, 9)]}, marks, 0.35)
    head, tail = u.split()
    assert head.text == "Once the zorblatt has been rinsed," and tail.text == "it should be left to dry in a cool, dark place."
    assert head.pause == 0 and tail.pause == 0.35 and head.rects == tail.rects == u.rects
    assert tail.word_at(tail.text.index("dry")) == u.word_at(text.index("dry"))
    assert head.word_at(head.text.index("rinsed")) == u.word_at(text.index("rinsed"))
    assert Utterance("Short, sentence.").split() == [Utterance("Short, sentence.")]


def _spans(text):
    import re

    return [(m.start(), m.group()) for m in re.finditer(r"\S+", text)]


def test_silence_never_splits_a_frame():
    q = PcmQueue(bytes_per_second=400, frame=4)
    assert len(q.read(10)) == 8


def test_to_format_converts_rate_channels_and_sample_type():
    samples = np.array([0.0, 0.5, -1.0, 2.0], dtype=np.float32)
    raw = np.frombuffer(to_format(samples, tts_engines.RATE, 1, "int16"), dtype="<i2")
    assert list(raw) == [0, 16383, -32767, 32767]
    stereo = np.frombuffer(to_format(samples, tts_engines.RATE * 2, 2, "float"), dtype="<f4")
    assert len(stereo) == 16 and stereo[0] == stereo[1]


def test_synthesize_joins_kokoro_results_and_aligns_their_tokens():
    tok = lambda text, a, b: SimpleNamespace(text=text, start_ts=a, end_ts=b)  # noqa: E731
    results = [
        SimpleNamespace(audio=np.ones(24000, dtype=np.float32), tokens=[tok("Hello", 0.1, 0.4), tok(",", 0.4, 0.5)]),
        SimpleNamespace(audio=None, tokens=[]),
        SimpleNamespace(audio=np.ones(12000, dtype=np.float32), tokens=[tok("world", 0.0, 0.3), tok("x", None, None)]),
    ]
    calls = []

    def pipeline(text, **kw):
        calls.append(kw)
        return iter(results)

    audio, words = synthesize(pipeline, "Hello, world x.", "af_heart", 1.3, 0.5)
    assert len(audio) == 24000 + 12000 + 12000  # plus half a second of silence
    assert words == [(0.1, 0.4, 0, 5), (1.0, 1.3, 7, 12)]
    assert calls == [{"voice": "af_heart", "speed": 1.3, "split_pattern": None}]


def test_speechd_rate_spans_its_range():
    assert tts_engines.speechd_rate(1.0) == 0
    assert tts_engines.speechd_rate(tts_engines.MAX_SPEED) == 1
    assert tts_engines.speechd_rate(0.1) == -1


# -- the controller, with a fake engine -------------------------------------------------------

@pytest.fixture
def fake_engine(monkeypatch):
    from PySide6.QtCore import QObject  # noqa: F401 - Qt first, for Engine's signals

    made = []

    class FakeEngine(tts_engines.Engine):
        lookahead = 1
        word_timing = True

        def __init__(self, name, parent=None):
            super().__init__(parent)
            self.name = name
            self.queue, self.clears, self.speed, self.voice = [], [], 1.0, ""
            self.skip = False
            self.skips_silence = name == "kokoro"
            self.paused = False
            self.rendered = set()  # texts "rendered on Colab"
            self.cache_dir = None
            made.append(self)

        def has(self, text):
            return text in self.rendered

        def cached(self):
            return len(self.rendered)

        def set_cache(self, folder):
            self.cache_dir = folder

        def voices(self):
            return [("v1", "Voice one"), ("v2", "Voice two")] if self.name == "kokoro" else []

        def enqueue(self, uid, text, pause):
            self.queue.append((uid, text))

        def clear(self, keep_current=False):
            self.clears.append(keep_current)
            self.queue = []

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

        def is_paused(self):
            return self.paused

        def set_speed(self, speed):
            self.speed = speed

        def set_voice(self, voice):
            self.voice = voice

        def set_skip_silence(self, on):
            self.skip = on

        # Play the first queued utterance to the end.
        def play_one(self):
            uid, text = self.queue.pop(0)
            self.started.emit(uid)
            self.word.emit(uid, len(text) - 1)
            self.finished.emit(uid)
            return text

    monkeypatch.setattr(tts_engines, "make_engine", lambda name, parent=None: FakeEngine(name, parent))
    monkeypatch.setattr(tts_engines, "kokoro_missing", lambda: None)
    import study_reader.read_aloud as ra

    monkeypatch.setattr(ra, "kokoro_missing", lambda: None)
    return made


@pytest.fixture
def story_pdf(tmp_path):
    doc = pymupdf.open()
    lines = ["One fish. Two fish.", "Red fish. Blue fish.", "Old fish. New fish."]
    for i in range(2):
        page = doc.new_page(width=400, height=500)
        for k, line in enumerate(lines):
            page.insert_text((40, 100 + 140 * k), f"{line} Page {i + 1}.", fontsize=12)
    path = tmp_path / "fish.pdf"
    doc.save(path)
    return str(path)


@pytest.fixture
def reading(story_pdf, tmp_path, monkeypatch, fake_engine):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.document import Document
    from study_reader.read_aloud import ReadAloud
    from study_reader.reader_view import ReaderView

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    view = ReaderView()
    view.resize(500, 300)
    view.show()
    doc = Document(story_pdf)
    view.set_document(doc)
    ctl = ReadAloud(db, view)
    ctl.set_document(doc)
    return SimpleNamespace(app=app, db=db, view=view, doc=doc, ctl=ctl, engines=fake_engine)


def test_reading_from_the_view_feeds_ahead_and_highlights(reading):
    ctl, view = reading.ctl, reading.view
    ctl.start_from_view()
    (engine,) = reading.engines
    assert ctl.active and view.speech_active and not ctl.bar.isHidden()
    assert [t for _, t in engine.queue] == ["One fish.", "Two fish."]  # the one playing and one ahead
    assert view._speech[0] == ctl._items[0].rects and view._speech[1] is None
    uid = engine.queue[0][0]
    engine.started.emit(uid)
    engine.word.emit(uid, 4)
    assert view._speech[1] == ctl._items[0].word_at(4)
    engine.finished.emit(uid)
    assert [t for _, t in engine.queue] == ["One fish.", "Two fish.", "Page 1."]
    assert ctl._cur == 1


def test_reading_runs_to_the_end_of_the_book_and_stops(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    spoken = []
    while engine.queue:
        spoken.append(engine.play_one())
    assert spoken[:4] == ["One fish.", "Two fish.", "Page 1.", "Red fish."]
    assert spoken[-1] == "Page 2." and len(spoken) == 18
    assert not ctl.active and reading.view._speech is None and ctl.bar.isHidden()


def test_a_selection_is_read_to_its_end_or_onward(reading):
    ctl, doc = reading.ctl, reading.doc
    words = doc.words(0)
    red = next(i for i, w in enumerate(words) if w.text == "Red")
    ctl.read_selection({0: words[red:red + 4]}, onward=False)
    (engine,) = reading.engines
    spoken = []
    while engine.queue:
        spoken.append(engine.play_one())
    assert spoken == ["Red fish.", "Blue fish."] and not ctl.active
    ctl.read_selection({0: words[red:red + 2]}, onward=True)
    assert [t for _, t in engine.queue] == ["Red fish.", "Blue fish."] and ctl._source is not None


def test_next_and_previous_sentence(reading, monkeypatch):
    import study_reader.read_aloud as ra

    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    ctl.next_sentence()
    assert engine.queue[0][1] == "Two fish." and ctl._cur == 1
    clock = [100.0]
    monkeypatch.setattr(ra.time, "monotonic", lambda: clock[0])
    engine.started.emit(engine.queue[0][0])
    clock[0] += 5  # well into the sentence: previous replays it
    ctl.prev_sentence()
    assert engine.queue[0][1] == "Two fish."
    ctl.prev_sentence()  # not started yet: back one
    assert engine.queue[0][1] == "One fish." and ctl._cur == 0
    ctl.prev_sentence()  # nothing before the first sentence of the book
    assert engine.queue[0][1] == "One fish."


def test_previous_reaches_before_where_reading_started(reading):
    ctl, view = reading.ctl, reading.view
    words = reading.doc.words(0)
    red = next(i for i, w in enumerate(words) if w.text == "Red")
    ctl.read_selection({0: words[red:red + 1]}, onward=True)
    (engine,) = reading.engines
    ctl.prev_sentence()
    assert engine.queue[0][1] == "Page 1." and ctl._items[0].text == "Page 1."
    uid = engine.queue[1][0]
    engine.started.emit(uid)  # the queued sentence after it keeps its place
    assert ctl._cur == 1 and ctl._items[1].text == "Red fish."
    assert view._speech[0] == ctl._items[1].rects


def test_speed_changes_requeue_what_hasnt_played_and_persist(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    engine.started.emit(engine.queue[0][0])
    ctl.set_speed(1.6)
    assert ctl.bar.speed_label.text() == "1.6×"
    ctl._speed_timer.stop()
    ctl._apply_speed()
    assert engine.speed == 1.6 and engine.clears[-1] is True  # the current sentence finishes
    assert [t for _, t in engine.queue] == ["Two fish."]
    assert reading.db.get_setting("tts_speed") == "1.6"
    ctl.set_speed(9)
    assert ctl.speed == tts_engines.MAX_SPEED


def test_skip_silence_requeues_what_hasnt_played_and_persists(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    assert not engine.skip and ctl.bar.skip.isVisibleTo(ctl.bar) and not ctl.bar.skip.isChecked()
    engine.started.emit(engine.queue[0][0])
    ctl.bar.skip.click()
    assert engine.skip and ctl.skip_silence and engine.clears[-1] is True  # the current sentence finishes
    assert [t for _, t in engine.queue] == ["Two fish."]
    assert reading.db.get_setting("tts_skip_silence") == "1"
    ctl.set_engine("speechd")
    ctl.start_from_view()
    assert not ctl.bar.skip.isVisibleTo(ctl.bar)  # the system voice can't
    from study_reader.read_aloud import ReadAloud

    again = ReadAloud(reading.db, reading.view)
    assert again.skip_silence and again.bar.skip.isChecked()


def test_voice_and_engine_choices_are_remembered(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    assert engine.voice == "v1" and ctl.bar.voice.count() == 2
    ctl.set_voice("v2")
    assert engine.voice == "v2" and reading.db.get_setting("tts_voice:kokoro") == "v2"
    assert engine.queue[0][1] == "One fish."  # the sentence starts over in the new voice
    ctl.set_engine("speechd")
    assert reading.db.get_setting("tts_engine") == "speechd"
    assert reading.engines[-1].name == "speechd" and reading.engines[-1].queue[0][1] == "One fish."
    assert ctl.bar.voice.isHidden()


def test_kokoro_failing_falls_back_to_the_system_voice(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    kokoro = reading.engines[0]
    kokoro.failed.emit("Kokoro couldn't start: no model")
    system = reading.engines[-1]
    assert system.name == "speechd" and system.queue[0][1] == "One fish." and ctl.active
    assert "system voice" in ctl.bar.status.text()
    assert reading.db.get_setting("tts_engine", "kokoro") == "kokoro"  # tried again next time
    system.failed.emit("speech-dispatcher isn't available")
    assert not ctl.active and "No speech engine" in ctl.bar.status.text()


def test_clicking_a_sentence_while_reading_jumps_there(reading):
    ctl, view, doc = reading.ctl, reading.view, reading.doc
    ctl.start_from_view()
    (engine,) = reading.engines
    blue = next(w for w in doc.words(0) if w.text == "Blue")
    view.speechClicked.emit(0, (blue.x0 + blue.x1) / 2, (blue.y0 + blue.y1) / 2)
    assert [t for _, t in engine.queue] == ["Blue fish.", "Page 1."]


def test_pause_resume_and_stop(reading):
    ctl, view = reading.ctl, reading.view
    ctl.play_pause()  # idle: starts from the view
    (engine,) = reading.engines
    assert ctl.active
    ctl.play_pause()
    assert engine.paused and ctl.bar.play_btn.toolTip().startswith("Play")
    ctl.play_pause()
    assert not engine.paused
    ctl.stop()
    assert not ctl.active and view._speech is None and not view.speech_active


def test_follow_waits_after_the_user_scrolls(reading):
    view = reading.view
    far = {1: [(40, 300, 100, 312)]}
    view.verticalScrollBar().setValue(0)
    view._user_moved()
    view.follow_speech(far)
    assert view.verticalScrollBar().value() == 0
    view._last_nav -= 60
    view.follow_speech(far)
    assert view.current_page() == 1
    top = view.verticalScrollBar().value()
    view.follow_speech(far)  # already in view: stays put
    assert view.verticalScrollBar().value() == top


def test_reading_a_reply_highlights_its_blocks(reading):
    ctl = reading.ctl
    calls = []

    class Transcript:
        def speech_blocks(self, mid, cb):
            cb([{"n": 0, "text": "Hello there. Look: $x^2$"}, {"n": 2, "text": "A list item"}])

        def speech_highlight(self, mid, n):
            calls.append((mid, n))

    ctl.read_reply(Transcript(), 7)
    (engine,) = reading.engines
    assert [t for _, t in engine.queue] == ["Hello there.", "Look: x squared."]
    assert not reading.view.speech_active and reading.view._speech is None
    assert calls == [(7, 0)]
    spoken = []
    while engine.queue:
        spoken.append(engine.play_one())
    assert spoken[-1] == "A list item." and calls == [(7, 0), (7, 2), (7, -1)] and not ctl.active


# -- the reader and the window ------------------------------------------------------------------

def test_popup_reads_the_selection(story_pdf, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    from study_reader.document import Document
    from study_reader.reader_view import ReaderView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    view = ReaderView()
    view.resize(500, 300)
    view.show()
    view.set_document(Document(story_pdf))
    got = []
    view.readRequested.connect(lambda sel, onward: got.append((sel, onward)))
    words = view.doc.words(0)
    view._selection = {0: words[:2]}
    view._show_popup(QPoint(50, 50))
    assert view._read_btn.isVisible() and view._read_on_btn.isVisible()
    view._read_on_btn.click()
    assert got == [({0: words[:2]}, True)] and not view.has_selection()
    view._region = (0, (10, 10, 100, 100))
    view._show_popup(QPoint(50, 50))
    assert not view._read_btn.isVisible()  # screenshot regions have no words to read


def test_main_window_read_aloud_controls(story_pdf, tmp_path, monkeypatch, fake_engine):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = MainWindow(Database(tmp_path / "t.db"))
    win.show()
    assert not win.read_action.isEnabled() and not win.play_action.isEnabled()
    win.open_book(story_pdf)
    assert win.read_action.isEnabled() and not win.next_sentence_action.isEnabled()
    from study_reader.render_dialog import local_audio

    assert win.read_aloud.audio == local_audio(win.db.book(win.book_id)["hash"])  # Colab's audio, if any
    win.read_action.trigger()
    assert win.read_aloud.active and win.read_action.isChecked() and win.next_sentence_action.isEnabled()
    assert not win.read_aloud.bar.isHidden()
    win.next_sentence_action.trigger()
    assert win.read_aloud._cur == 1
    win.stop_reading_action.trigger()
    assert not win.read_action.isChecked()
    win.play_action.trigger()
    assert win.read_aloud.active
    win.show_library()  # the book stays open behind the Library, and reading pauses
    assert win.read_aloud.active and fake_engine[-1].paused
    win.show_page("book")
    win.play_action.trigger()
    assert not fake_engine[-1].paused
    win.close_book()
    assert not win.read_aloud.active and win.read_aloud.book is None
    win.close()


def test_rendered_audio_starts_at_a_whole_sentence_and_isnt_split(reading):
    from study_reader.speech_text import Utterance

    ctl = reading.ctl
    ctl.set_document(reading.doc, "/audio/here")
    (engine,) = reading.engines
    assert engine.cache_dir == "/audio/here"
    words = reading.doc.words(0)
    two = next(i for i, w in enumerate(words) if w.text == "Two")
    ctl.book.start_at = lambda page, y: (0, two + 1)  # the view opens mid-sentence, on "fish."
    ctl.start_from_view()
    assert engine.queue[0][1] == "fish."  # live speech starts right there
    engine.rendered = {"Two fish."}
    ctl.start_from_view()
    assert engine.queue[0][1] == "Two fish."  # rendered audio is of whole sentences

    long = "Once the zorblatt has been rinsed, it should be left to dry in a cool, dark place."
    engine.lookahead_chars = 600
    ctl._items, ctl._source = [Utterance(long)], None
    ctl._jump(0)
    assert [u.text for u in ctl._items] == ["Once the zorblatt has been rinsed,", long[35:]]  # split to start sooner
    engine.rendered = {long}
    ctl._items, ctl._source = [Utterance(long)], None
    ctl._jump(0)
    assert [u.text for u in ctl._items] == [long]  # already rendered: played whole


def test_a_new_audio_output_gets_the_sentence_again(reading):
    ctl = reading.ctl
    ctl.start_from_view()
    (engine,) = reading.engines
    engine.play_one()  # "One fish." heard; now on "Two fish."
    engine.outputChanged.emit()  # say, headphones connected: what was queued went nowhere
    assert [t for _, t in engine.queue][0] == "Two fish." and ctl._cur == 1


def test_kokoro_moves_to_a_new_default_output(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtMultimedia import QMediaDevices
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    engine = tts_engines.KokoroEngine()
    stopped = []
    engine._sink = SimpleNamespace(stop=lambda: stopped.append(1), deleteLater=lambda: None)
    engine._dev = SimpleNamespace(close=lambda: None, deleteLater=lambda: None)
    engine._device_id = b"speakers"
    moved = []
    engine.outputChanged.connect(lambda: moved.append(1))
    current = [b"speakers"]
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: SimpleNamespace(id=lambda: current[0])))
    engine._outputs_changed()  # another device came or went: still the same default
    assert not moved and engine._sink is not None
    current[0] = b"headphones"
    gen = engine._gen
    engine._outputs_changed()
    assert moved and stopped and engine._sink is None and engine._gen == gen + 1
