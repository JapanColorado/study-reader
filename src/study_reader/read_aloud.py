"""Reading aloud: the controller that feeds sentences from the book (or a chat reply) to a
speech engine and moves the highlight along, and the playback bar under the reader."""

from __future__ import annotations

import time
from collections.abc import Iterator

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QSlider, QToolButton

from . import narration, tts_engines
from .db import Database
from .document import Document, Word, nearest_word
from .narration import WAIT, NarratedText
from .speech_pages import BookText
from .speech_text import Utterance, reply_utterances
from .theme import icon
from .tts_engines import MAX_SPEED, MIN_SPEED, Engine, kokoro_missing

REPLAY_AFTER = 2.0  # s into a sentence after which "previous" replays it instead
SPEED_DEBOUNCE_MS = 400
UID_SPAN = 1 << 20  # uids of one reading; a new reading starts past them


class Narrator(QObject):
    """Gets the open book's narration from Claude (narration.py): the pages reading asks for
    as it goes, and on request (``prepare``) the whole book, ``AT_ONCE`` pages at a time.
    Replies are kept in the ``narrations`` table."""

    ready = Signal(int)  # a page's narration is here, or failed (the page is read as extracted)
    progress = Signal(int, int)  # pages done and pages in the book, while preparing all of it
    prepared = Signal(bool)  # the whole book has been gone through (False: stopped before the end)
    failed = Signal(str)
    AT_ONCE = 3
    GIVE_UP = 3  # failures in a row that stop preparing the whole book

    def __init__(self, db: Database, worker, parent=None):
        super().__init__(parent)
        self.db, self.worker = db, worker
        self.text: NarratedText | None = None
        self.hash = ""
        self._flying: set[int] = set()
        self._todo: Iterator[int] | None = None
        self._seen = 0
        self._fails = 0
        self.gave_up = False  # preparing the whole book stopped because Claude kept failing
        self._timer = QTimer(self, singleShot=True, interval=0)
        self._timer.timeout.connect(self._pump)
        worker.narrated.connect(self._on_narrated)
        worker.narrationFailed.connect(self._on_failed)

    def set_document(self, book: BookText | None, file_hash: str) -> None:
        self.stop_preparing()
        if self._flying:
            self.worker.stop_narrating()
            self._flying.clear()
        self.hash = file_hash if book else ""
        self.text = NarratedText(book, self.db.narrations(file_hash), self.want) if book and file_hash else None

    def want(self, page: int) -> None:
        if page not in self._flying:
            self._flying.add(page)
            self.worker.submit_narration(self.hash, page, self.text.key(page), self.text.blocks(page))

    @property
    def preparing(self) -> bool:
        return self._todo is not None

    def prepare(self) -> None:
        """Narrate every page that isn't yet."""
        if self.text is None or self.preparing:
            return
        self._todo, self._seen, self._fails = iter(range(self.text.book.doc.page_count)), 0, 0
        self.gave_up = False
        self._timer.start()

    def stop_preparing(self) -> None:
        if self.preparing:
            self._todo = None
            self._timer.stop()
            self.prepared.emit(False)

    def _pump(self) -> None:
        """Look at the next page (one a turn of the event loop: reading a page takes a moment)."""
        if not self.preparing or len(self._flying) >= self.AT_ONCE:
            return
        page = next(self._todo, None)
        if page is None:
            if not self._flying:
                self._todo = None
                self.prepared.emit(True)
            return
        self.text.script(page)  # asks for it if it isn't here
        self._seen = page + 1
        self.progress.emit(self._seen - len(self._flying), self.text.book.doc.page_count)
        self._timer.start()

    def _landed(self, book: str, page: int) -> bool:
        if book != self.hash or self.text is None or page not in self._flying:
            return False
        self._flying.discard(page)
        return True

    def _on_narrated(self, book: str, page: int, key: str, text: str) -> None:
        if not self._landed(book, page):
            return
        if self.text.accept(page, key, text):
            self.db.set_narration(book, page, key, text)
            self._fails = 0
        self._after(page)

    def _on_failed(self, book: str, page: int, message: str) -> None:
        if not self._landed(book, page):
            return
        self.text.fail(page)
        self._fails += 1
        if self.preparing and self._fails >= self.GIVE_UP:
            self.gave_up = True
            self.stop_preparing()
        self._after(page)
        self.failed.emit(message)

    def _after(self, page: int) -> None:
        self.ready.emit(page)
        if self.preparing:
            self.progress.emit(self._seen - len(self._flying), self.text.book.doc.page_count)
            self._timer.start()


class ReadAloud(QObject):
    activeChanged = Signal(bool)
    renderRequested = Signal()  # the bar's "Render on Colab" button
    narrationChanged = Signal(bool)

    def __init__(self, db: Database, reader, parent=None, worker=None):
        super().__init__(parent)
        self.db, self.reader = db, reader
        self.book: BookText | None = None
        # Claude's narration of the pages (None: no worker to ask through).
        self.narrator = Narrator(db, worker, self) if worker is not None else None
        self.narration = db.get_setting(narration.ENABLED_SETTING, narration.ENABLED_DEFAULT) == "1"
        self._waiting = False  # for a page's narration
        self.engine: Engine | None = None
        self.engine_name = db.get_setting("tts_engine", "kokoro")
        self._fallback = False  # Kokoro failed this session: use speech-dispatcher
        self.speed = _clamp(float(db.get_setting("tts_speed", "1.0") or 1.0))
        self.skip_silence = db.get_setting("tts_skip_silence", "0") == "1"
        self.audio = None  # folder of the open book's audio rendered on Colab
        self.active = False
        self._items: list[Utterance] = []
        self._source: Iterator[Utterance] | None = None
        self._first_uid = 0
        self._next_uid = 0
        self._cur = 0
        self._fed = -1
        self._heard = False
        self._started_at = 0.0
        self._shown_block = None
        self._reply = None  # (transcript, message id) while reading a reply
        self._speed_timer = QTimer(self, singleShot=True, interval=SPEED_DEBOUNCE_MS)
        self._speed_timer.timeout.connect(self._apply_speed)
        self.bar = ReadAloudBar(self)
        reader.speechClicked.connect(self.jump_to)
        if self.narrator is not None:
            self.narrator.ready.connect(self._on_narration)
            self.narrator.failed.connect(
                lambda why: self.active and self.bar.set_status(f"No narration for a page ({why}); reading it as it is."))

    # -- the engine ---------------------------------------------------------------------
    @property
    def current_engine(self) -> str:
        return "speechd" if self._fallback else self.engine_name

    def _engine(self) -> Engine:
        if self.engine is None:
            name = self.current_engine
            if name == "kokoro" and (why := kokoro_missing()):
                self._fallback, name = True, "speechd"
                self.bar.set_status(f"Kokoro isn't available ({why}); using the system voice.")
            self.engine = tts_engines.make_engine(name, self)
            self.engine.set_cache(self.audio)
            self.engine.started.connect(self._on_started)
            self.engine.word.connect(self._on_word)
            self.engine.finished.connect(self._on_finished)
            self.engine.failed.connect(self._on_failed)
            self.engine.status.connect(self.bar.set_status)
            self.engine.outputChanged.connect(self._on_output_changed)
            voices = self.engine.voices()
            default = voices[0][0] if voices and self.engine.name == "kokoro" else ""
            voice = self.db.get_setting(f"tts_voice:{self.engine.name}", default)
            if voice:
                self.engine.set_voice(voice)
            self.engine.set_speed(self.speed)
            self.engine.set_skip_silence(self.skip_silence)
            self.bar.set_voices(voices, voice)
            self.bar.skip.setVisible(self.engine.skips_silence)
        return self.engine

    def _drop_engine(self) -> None:
        if self.engine is not None:
            self.engine.shutdown()
            self.engine.deleteLater()
            self.engine = None

    def _on_failed(self, message: str) -> None:
        if self.engine is not None and self.engine.name == "kokoro":
            self._fallback = True
            self._drop_engine()
            self.bar.set_status(f"{message} Reading with the system voice instead.")
            if self.active:
                self._jump(self._cur)
            return
        self._drop_engine()
        self.stop()
        self.bar.set_status(f"No speech engine is available: {message}")
        self.bar.show()

    def _on_output_changed(self) -> None:
        if self.active:  # say the sentence again where the sound goes now
            self._jump(self._cur)

    def set_engine(self, name: str) -> None:
        self.engine_name, self._fallback = name, False
        self.db.set_setting("tts_engine", name)
        self._drop_engine()
        if self.active:
            self._jump(self._cur)

    def set_voice(self, voice: str) -> None:
        engine = self._engine()
        engine.set_voice(voice)
        self.db.set_setting(f"tts_voice:{engine.name}", voice)
        if self.active:
            self._jump(self._cur)

    def set_speed(self, speed: float) -> None:
        self.speed = _clamp(speed)
        self.bar.show_speed(self.speed)
        self._speed_timer.start()

    def _apply_speed(self) -> None:
        self.db.set_setting("tts_speed", f"{self.speed:.1f}")
        if self.engine is None:
            return
        self.engine.set_speed(self.speed)
        self._requeue()

    def set_skip_silence(self, on: bool) -> None:
        """Shorten the silences in Kokoro's audio (rendered or made now), from the next sentence."""
        self.skip_silence = on
        self.db.set_setting("tts_skip_silence", "1" if on else "0")
        self.bar.skip.setChecked(on)
        if self.engine is not None:
            self.engine.set_skip_silence(on)
            self._requeue()

    def _requeue(self) -> None:
        """Remake what is queued after a change to how it sounds."""
        if not self.active:
            return
        if self._heard:  # let the sentence being read finish; redo the ones queued after it
            kept = self.engine.clear(keep_current=True)
            # In a sentence's last moments the next one may be on its way to the device
            # already, and is kept too: carry on after it, or it would be heard twice.
            i = self._index(kept) if kept is not None else None
            self._fed = max(self._cur, i) if i is not None else self._cur
            self._feed()
        else:
            self._jump(self._cur)

    # -- starting -------------------------------------------------------------------------
    def set_document(self, doc: Document | None, audio=None, file_hash: str = "") -> None:
        """Read ``doc`` (None: nothing), playing sentences rendered on Colab from ``audio``;
        ``file_hash`` is what the book's narration is kept under."""
        self.stop()
        self.book = BookText(doc) if doc else None
        if self.narrator is not None:
            self.narrator.set_document(self.book, file_hash)
        self.audio = audio if doc else None
        if self.engine is not None:
            self.engine.set_cache(self.audio)
        if doc and self.current_engine == "kokoro":
            self._engine().warm()  # loading takes a while; do it before F5

    def set_audio(self, audio) -> None:
        """New or more rendered audio for the open book."""
        self.audio = audio
        if self.engine is not None:
            self.engine.set_cache(audio)

    @property
    def narrated(self) -> NarratedText | None:
        """The book's narration, when it is to be read from."""
        return self.narrator.text if self.narration and self.narrator is not None else None

    def set_narration(self, on: bool) -> None:
        """Read Claude's narration of each page, or the page as extracted; while reading, carry on
        from the sentence being read."""
        self.narration = on
        self.db.set_setting(narration.ENABLED_SETTING, "1" if on else "0")
        self.bar.narrate.setChecked(on)
        self.narrationChanged.emit(on)
        if not self.active or self._reply or not self.book:
            return
        u = self._items[self._cur] if self._cur < len(self._items) else None
        if u is None or not u.rects:
            self.start_from_view()
            return
        page = min(u.rects)
        self._start_at(page, min(r[1] for r in u.rects[page]) + 1)

    def start_from_view(self) -> None:
        if not self.book:
            return
        self._start_at(*self.reader._position())

    def _start_at(self, page: int, y: float) -> None:
        if self.narrated:
            self._read_book(self.narrated.from_view(page, y))
            return
        start = self.book.start_at(page, y)
        engine = self._engine()
        if engine.cached() and (u := self.book.utterance_at(*start)) and engine.has(u.text):
            # Rendered sentences are whole ones: start at the one the page opens in.
            self._read_book(self._after(u), first=u)
            return
        self._read_book(self.book.utterances(start))

    def read_selection(self, sel: dict[int, list[Word]], onward: bool) -> None:
        """Read the selected words, or (onward) from them to the end of the book."""
        if not self.book or not sel:
            return
        doc = self.book.doc
        p0, p1 = min(sel), max(sel)
        start = (p0, doc.words(p0).index(sel[p0][0]))
        if onward and self.narrated:
            self._read_book(self.narrated.from_word(*start))
            return
        end = None if onward else (p1, doc.words(p1).index(sel[p1][-1]))
        self._read_book(self.book.utterances(start, end))

    def jump_to(self, page: int, x: float, y: float) -> None:
        """Read from the sentence under a click on the page."""
        if not self.book or not self.active or self._reply:
            return
        idx = nearest_word(self.book.doc.words(page), x, y)
        if idx is not None and self.narrated:
            self._read_book(self.narrated.from_word(page, idx))
        elif idx is not None and (u := self.book.utterance_at(page, idx)):
            self._read_book(self._after(u), first=u)

    def _after(self, u: Utterance, end: tuple[int, int] | None = None) -> Iterator[Utterance]:
        return self.book.utterances((u.last[0], u.last[1] + 1), end)

    def _read_book(self, source: Iterator[Utterance], first: Utterance | None = None) -> None:
        first = first or next(source, None)
        if first is None:
            self.stop()
            return
        self._end_reply()
        if first is WAIT:  # the page's narration is on its way: _feed starts once it lands
            self._begin([], source)
            return
        self._begin([first], source)
        self.reader.follow_speech(first.rects, force=True)

    def read_reply(self, transcript, message_id: int) -> None:
        """Read a chat reply aloud, highlighting its paragraphs in the transcript."""
        def got(blocks):
            items = reply_utterances([(b["n"], b["text"]) for b in blocks or []])
            if not items:
                return
            self._end_reply()
            self.reader.clear_speech()
            self._reply = (transcript, message_id)
            self.bar.set_status("Reading the reply")
            self._begin(items, None)

        transcript.speech_blocks(message_id, got)

    def _begin(self, items: list[Utterance], source: Iterator[Utterance] | None) -> None:
        engine = self._engine()
        engine.clear()
        if engine.is_paused():
            engine.resume()
        self._first_uid = self._next_uid
        self._next_uid += UID_SPAN
        self._items, self._source = items, source
        self._set_active(True)
        self._jump(0)

    def _jump(self, index: int) -> None:
        engine = self._engine()
        engine.clear()
        if engine.lookahead_chars and index < len(self._items) and not engine.has(self._items[index].text):
            # Starting cold: the first half of a long sentence is ready sooner.
            self._items[index:index + 1] = self._items[index].split()
        self._cur, self._fed, self._heard = index, index - 1, False
        self._shown_block = None
        if index < len(self._items):
            self._show(self._items[index], None)
        self._feed()
        self.bar.set_playing(not engine.is_paused())

    def _on_narration(self, _page: int) -> None:
        if self.active and self._waiting:
            self._feed()

    def _wait(self, on: bool) -> None:
        if on != self._waiting:
            self._waiting = on
            self.bar.set_status("Preparing the narration…" if on else "")

    def _feed(self) -> None:
        engine = self._engine()
        self._wait(False)
        started = not self._items
        while self._fed < self._cur + engine.lookahead or (
            sum(len(u.text) for u in self._items[self._cur:self._fed + 1]) < engine.lookahead_chars
        ):
            n = self._fed + 1
            if n >= len(self._items):
                nxt = next(self._source, None) if self._source else None
                if nxt is WAIT:
                    self._wait(True)
                    break
                if nxt is None:
                    self._source = None
                    break
                self._items.append(nxt)
            u = self._items[n]
            engine.enqueue(self._first_uid + n, u.text, u.pause)
            self._fed = n
        if self._cur >= len(self._items) and not self._waiting:
            self.stop()
        elif started and self._items:  # the first sentence, after waiting for its page
            self._show(self._items[0], None)
            self.reader.follow_speech(self._items[0].rects, force=True)

    # -- engine events -------------------------------------------------------------------
    def _index(self, uid: int) -> int | None:
        i = uid - self._first_uid
        return i if self.active and 0 <= i < len(self._items) else None

    def _on_started(self, uid: int) -> None:
        if (i := self._index(uid)) is None:
            return
        self._cur, self._heard, self._started_at = i, True, time.monotonic()
        self._show(self._items[i], None)
        self._feed()

    def _on_word(self, uid: int, char: int) -> None:
        if (i := self._index(uid)) is not None and i == self._cur:
            u = self._items[i]
            self._show(u, u.word_at(char))

    def _on_finished(self, uid: int) -> None:
        if (i := self._index(uid)) is None or i < self._cur:
            return
        self._cur, self._heard = i + 1, False
        self._feed()

    def _show(self, u: Utterance, word) -> None:
        if self._reply:
            transcript, mid = self._reply
            if u.block != self._shown_block:
                self._shown_block = u.block
                transcript.speech_highlight(mid, u.block)
            return
        self.reader.set_speech(u.rects, word)
        self.reader.follow_speech(word or u.rects)

    # -- controls -------------------------------------------------------------------------
    def play_pause(self) -> None:
        if not self.active:
            self.start_from_view()
            return
        engine = self._engine()
        if engine.is_paused():
            engine.resume()
        else:
            engine.pause()
        self.bar.set_playing(not engine.is_paused())

    def pause(self) -> None:
        if self.active and self.engine is not None and not self.engine.is_paused():
            self.engine.pause()
            self.bar.set_playing(False)

    def next_sentence(self) -> None:
        if not self.active:
            return
        n = self._cur + 1
        if n >= len(self._items):
            nxt = next(self._source, None) if self._source else None
            if nxt is WAIT:  # its page isn't narrated yet: reading gets there by itself
                return
            if nxt is None:
                self.stop()
                return
            self._items.append(nxt)
        self._jump(n)
        self._follow_now()

    def prev_sentence(self) -> None:
        if not self.active:
            return
        if self._heard and time.monotonic() - self._started_at > REPLAY_AFTER:
            self._jump(self._cur)
        elif self._cur > 0:
            self._jump(self._cur - 1)
        elif self.book and not self._reply and self._items and (u := self._before(self._items[0])):
            self._items.insert(0, u)
            self._first_uid -= 1  # the queued sentences keep their uids
            self._jump(0)
        else:
            self._jump(self._cur)
        self._follow_now()

    def _before(self, u: Utterance) -> Utterance | None:
        return self.narrated.before(u) if self.narrated else self.book.utterance_before(u)

    def _follow_now(self) -> None:
        if not self._reply and self._cur < len(self._items):
            self.reader.follow_speech(self._items[self._cur].rects, force=True)

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.clear()
            if self.engine.is_paused():
                self.engine.resume()
        self._end_reply()
        self.reader.clear_speech()
        self._items, self._source = [], None
        self._wait(False)
        self._set_active(False)

    def _end_reply(self) -> None:
        if self._reply:
            transcript, mid = self._reply
            self._reply = None
            transcript.speech_highlight(mid, -1)
            self.bar.set_status("")

    def _set_active(self, on: bool) -> None:
        self.reader.speech_active = on and not self._reply
        if on != self.active:
            self.active = on
            self.bar.setVisible(on)
            self.activeChanged.emit(on)

    def shutdown(self) -> None:
        self.stop()
        self._drop_engine()


def _clamp(speed: float) -> float:
    return round(min(max(speed, MIN_SPEED), MAX_SPEED), 1)


class ReadAloudBar(QFrame):
    """⏮ ⏯ ⏭, speed, voice and ✕, under the reader while reading aloud."""

    def __init__(self, ctl: ReadAloud):
        super().__init__(objectName="readBar")
        self.ctl = ctl
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(4)

        def button(name: str, tip: str, slot) -> QToolButton:
            b = QToolButton(self, autoRaise=True, toolTip=tip)
            b.setIcon(icon(name))
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        self.prev_btn = button("skip-back", "Previous sentence (F4)", ctl.prev_sentence)
        self.play_btn = button("pause", "Pause (F5)", ctl.play_pause)
        self.next_btn = button("skip-forward", "Next sentence (F6)", ctl.next_sentence)
        row.addSpacing(10)
        row.addWidget(QLabel("Speed", self))
        self.speed = QSlider(Qt.Orientation.Horizontal, self, minimum=int(MIN_SPEED * 10), maximum=int(MAX_SPEED * 10))
        self.speed.setFixedWidth(130)
        self.speed.setPageStep(1)
        self.speed.setValue(int(round(ctl.speed * 10)))
        self.speed.valueChanged.connect(lambda v: ctl.set_speed(v / 10))
        row.addWidget(self.speed)
        self.speed_label = QLabel(self)
        self.speed_label.setMinimumWidth(34)
        row.addWidget(self.speed_label)
        self.show_speed(ctl.speed)
        self.skip = QToolButton(self, autoRaise=True, checkable=True, text="Skip silence")
        self.skip.setToolTip("Skip silence: shorten the pauses before, inside and after each sentence")
        self.skip.setIcon(icon("audio-lines"))
        self.skip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.skip.setChecked(ctl.skip_silence)
        self.skip.toggled.connect(lambda on: on != ctl.skip_silence and ctl.set_skip_silence(on))
        row.addWidget(self.skip)
        self.narrate = QToolButton(self, autoRaise=True, checkable=True, text="Narrate")
        self.narrate.setToolTip("Narrate with Claude: Haiku puts each page in reading order, leaves out the labels "
                                "inside figures, and says equations in words (each page is prepared once)")
        self.narrate.setIcon(icon("sparkles"))
        self.narrate.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.narrate.setChecked(ctl.narration)
        self.narrate.setVisible(ctl.narrator is not None)
        self.narrate.toggled.connect(lambda on: on != ctl.narration and ctl.set_narration(on))
        row.addWidget(self.narrate)
        row.addSpacing(10)
        self.voice = QComboBox(self, toolTip="Voice")
        self.voice.setMinimumContentsLength(12)
        self.voice.activated.connect(lambda i: ctl.set_voice(self.voice.itemData(i)))
        row.addWidget(self.voice)
        self.status = QLabel(self)
        self.status.setProperty("role", "muted")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.status, 1)
        button("cloud", "Render this book's audio on Colab, so reading never waits", ctl.renderRequested.emit)
        button("x", "Stop reading aloud (Shift+F5)", ctl.stop)
        self.hide()

    def show_speed(self, speed: float) -> None:
        self.speed_label.setText(f"{speed:.1f}×")
        if self.speed.value() != round(speed * 10):
            self.speed.blockSignals(True)
            self.speed.setValue(int(round(speed * 10)))
            self.speed.blockSignals(False)

    def set_voices(self, voices: list[tuple[str, str]], current: str) -> None:
        self.voice.clear()
        for vid, label in voices:
            self.voice.addItem(label, vid)
        self.voice.setCurrentIndex(max(self.voice.findData(current), 0))
        self.voice.setVisible(bool(voices))

    def set_playing(self, playing: bool) -> None:
        self.play_btn.setIcon(icon("pause" if playing else "play"))
        self.play_btn.setToolTip("Pause (F5)" if playing else "Play (F5)")

    def set_status(self, text: str) -> None:
        self.status.setText(text)
