"""Reading aloud: the controller that feeds sentences from the book (or a chat reply) to a
speech engine and moves the highlight along, and the playback bar under the reader."""

from __future__ import annotations

import time
from collections.abc import Iterator

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QSlider, QToolButton

from . import tts_engines
from .db import Database
from .document import Document, Word, nearest_word
from .speech_pages import BookText
from .speech_text import Utterance, reply_utterances
from .theme import icon
from .tts_engines import MAX_SPEED, MIN_SPEED, Engine, kokoro_missing

REPLAY_AFTER = 2.0  # s into a sentence after which "previous" replays it instead
SPEED_DEBOUNCE_MS = 400
UID_SPAN = 1 << 20  # uids of one reading; a new reading starts past them


class ReadAloud(QObject):
    activeChanged = Signal(bool)

    def __init__(self, db: Database, reader, parent=None):
        super().__init__(parent)
        self.db, self.reader = db, reader
        self.book: BookText | None = None
        self.engine: Engine | None = None
        self.engine_name = db.get_setting("tts_engine", "kokoro")
        self._fallback = False  # Kokoro failed this session: use speech-dispatcher
        self.speed = _clamp(float(db.get_setting("tts_speed", "1.0") or 1.0))
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
            self.engine.started.connect(self._on_started)
            self.engine.word.connect(self._on_word)
            self.engine.finished.connect(self._on_finished)
            self.engine.failed.connect(self._on_failed)
            self.engine.status.connect(self.bar.set_status)
            voices = self.engine.voices()
            default = voices[0][0] if voices and self.engine.name == "kokoro" else ""
            voice = self.db.get_setting(f"tts_voice:{self.engine.name}", default)
            if voice:
                self.engine.set_voice(voice)
            self.engine.set_speed(self.speed)
            self.bar.set_voices(voices, voice)
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
        if not self.active:
            return
        if self._heard:  # let the sentence being read finish; redo the ones queued after it
            self.engine.clear(keep_current=True)
            self._fed = self._cur
            self._feed()
        else:
            self._jump(self._cur)

    # -- starting -------------------------------------------------------------------------
    def set_document(self, doc: Document | None) -> None:
        self.stop()
        self.book = BookText(doc) if doc else None
        if doc and self.current_engine == "kokoro":
            self._engine().warm()  # loading takes a while; do it before F5

    def start_from_view(self) -> None:
        if not self.book:
            return
        page, y = self.reader._position()
        self._read_book(self.book.utterances(self.book.start_at(page, y)))

    def read_selection(self, sel: dict[int, list[Word]], onward: bool) -> None:
        """Read the selected words, or (onward) from them to the end of the book."""
        if not self.book or not sel:
            return
        doc = self.book.doc
        p0, p1 = min(sel), max(sel)
        start = (p0, doc.words(p0).index(sel[p0][0]))
        end = None if onward else (p1, doc.words(p1).index(sel[p1][-1]))
        self._read_book(self.book.utterances(start, end))

    def jump_to(self, page: int, x: float, y: float) -> None:
        """Read from the sentence under a click on the page."""
        if not self.book or not self.active or self._reply:
            return
        idx = nearest_word(self.book.doc.words(page), x, y)
        if idx is not None and (u := self.book.utterance_at(page, idx)):
            self._read_book(self._after(u), first=u)

    def _after(self, u: Utterance, end: tuple[int, int] | None = None) -> Iterator[Utterance]:
        return self.book.utterances((u.last[0], u.last[1] + 1), end)

    def _read_book(self, source: Iterator[Utterance], first: Utterance | None = None) -> None:
        first = first or next(source, None)
        if first is None:
            self.stop()
            return
        self._end_reply()
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
        if engine.lookahead_chars and index < len(self._items):
            # Starting cold: the first half of a long sentence is ready sooner.
            self._items[index:index + 1] = self._items[index].split()
        self._cur, self._fed, self._heard = index, index - 1, False
        self._shown_block = None
        if index < len(self._items):
            self._show(self._items[index], None)
        self._feed()
        self.bar.set_playing(not engine.is_paused())

    def _feed(self) -> None:
        engine = self._engine()
        while self._fed < self._cur + engine.lookahead or (
            sum(len(u.text) for u in self._items[self._cur:self._fed + 1]) < engine.lookahead_chars
        ):
            n = self._fed + 1
            if n >= len(self._items):
                nxt = next(self._source, None) if self._source else None
                if nxt is None:
                    self._source = None
                    break
                self._items.append(nxt)
            u = self._items[n]
            engine.enqueue(self._first_uid + n, u.text, u.pause)
            self._fed = n
        if self._cur >= len(self._items):
            self.stop()

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
        elif self.book and not self._reply and (u := self.book.utterance_before(self._items[0])):
            self._items.insert(0, u)
            self._first_uid -= 1  # the queued sentences keep their uids
            self._jump(0)
        else:
            self._jump(self._cur)
        self._follow_now()

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
        row.addSpacing(10)
        self.voice = QComboBox(self, toolTip="Voice")
        self.voice.setMinimumContentsLength(12)
        self.voice.activated.connect(lambda i: ctl.set_voice(self.voice.itemData(i)))
        row.addWidget(self.voice)
        self.status = QLabel(self)
        self.status.setProperty("role", "muted")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.status, 1)
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
