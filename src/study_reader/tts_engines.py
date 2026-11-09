"""Speech engines for reading aloud: Kokoro (a neural voice, run on a worker thread and
played through a QAudioSink, with word timings) and the system's speech-dispatcher
through QTextToSpeech (sentence by sentence, no word timings)."""

from __future__ import annotations

import importlib.util
import math
import os
import threading
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QIODevice, QObject, QTimer, Signal

from .audio_cache import AudioCache

RATE = 24000  # Kokoro's sample rate
REPO = "hexgrad/Kokoro-82M"
LATENCY = 0.08  # s between audio handed to the device and heard
BUFFER = 3.0  # s of speech ready before playing starts (or resumes after running dry)
TICK_MS = 30

ENGINES = [("kokoro", "Kokoro (natural voice)"), ("speechd", "System voice (speech-dispatcher)")]
KOKORO_VOICES = [
    ("af_heart", "Heart · US"), ("af_bella", "Bella · US"), ("af_nicole", "Nicole · US"),
    ("af_sarah", "Sarah · US"), ("am_michael", "Michael · US"), ("am_fenrir", "Fenrir · US"),
    ("am_puck", "Puck · US"), ("bf_emma", "Emma · UK"), ("bf_isabella", "Isabella · UK"),
    ("bm_george", "George · UK"), ("bm_fable", "Fable · UK"),
]
MIN_SPEED, MAX_SPEED = 0.5, 3.0


class Engine(QObject):
    """Speaks queued utterances (by uid) in order and reports what is audible."""

    started = Signal(int)  # uid now audible
    word = Signal(int, int)  # uid, char offset (in its text) of the word now audible
    finished = Signal(int)  # uid fully played
    failed = Signal(str)
    status = Signal(str)  # e.g. "Loading Kokoro…"; "" when ready
    outputChanged = Signal()  # sound now goes elsewhere (say, headphones connected): what was queued is gone
    lookahead = 0  # utterances to queue beyond the one playing
    lookahead_chars = 0  # ...or more, until this much text is queued
    word_timing = False
    skips_silence = False  # set_skip_silence does something
    name = ""

    def voices(self) -> list[tuple[str, str]]:
        return []

    def has(self, text: str) -> bool:
        """Whether the sentence was rendered ahead of time (and so plays at once)."""
        return False

    def cached(self) -> int:
        """How many sentences are rendered ahead of time for the open book."""
        return 0

    def set_cache(self, folder) -> None:
        """Play sentences rendered on Colab from this folder (None: none)."""

    def warm(self) -> None:
        """Get ready to speak (load models) before the first sentence is asked for."""

    def enqueue(self, uid: int, text: str, pause: float) -> None: ...
    def clear(self, keep_current: bool = False) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def is_paused(self) -> bool:
        return False
    def set_speed(self, speed: float) -> None: ...
    def set_skip_silence(self, on: bool) -> None: ...
    def set_voice(self, voice: str) -> None: ...
    def shutdown(self) -> None: ...


def kokoro_missing() -> str | None:
    """Why Kokoro can't run, without importing it (that takes seconds)."""
    for mod in ("kokoro", "misaki", "torch", "en_core_web_sm"):
        if importlib.util.find_spec(mod) is None:
            return f"the Python package {mod!r} isn't installed"
    return None


def make_engine(name: str, parent: QObject | None = None) -> Engine:
    return KokoroEngine(parent) if name == "kokoro" else SpeechdEngine(parent)


# -- PCM timing ---------------------------------------------------------------------------

@dataclass
class _Segment:
    uid: int
    data: bytes
    words: list[tuple[float, float, int, int]]  # (start s, end s, char start, char end)
    start: int | None = None  # byte offset in the stream where it began
    pos: int = 0  # bytes handed out
    announced: bool = False
    char: int | None = None


@dataclass
class PcmQueue:
    """Audio segments played back to back, padded with silence, and which one (and which
    word in it) is audible once a given number of bytes has been played."""

    bytes_per_second: int = RATE * 2
    frame: int = 2  # bytes per frame, so silence never splits one
    segs: list[_Segment] = field(default_factory=list)
    total: int = 0  # bytes handed out since the last reset
    hold: bool = True  # play silence until enough is ready (see settle)

    def append(self, uid: int, data: bytes, words: list[tuple[float, float, int, int]]) -> None:
        self.segs.append(_Segment(uid, data, words))

    def read(self, n: int) -> bytes:
        n -= n % self.frame
        out = bytearray()
        for s in [] if self.hold else self.segs:
            if len(out) >= n:
                break
            if s.pos >= len(s.data):
                if s.start is None:
                    s.start = self.total + len(out)
                continue
            if s.start is None:
                s.start = self.total + len(out)
            chunk = s.data[s.pos:s.pos + n - len(out)]
            s.pos += len(chunk)
            out += chunk
        out += bytes(n - len(out))
        self.total += n
        return bytes(out)

    def pending(self) -> bool:
        return any(s.pos < len(s.data) for s in self.segs)

    def buffered(self) -> float:
        """Seconds of audio queued and not yet handed out."""
        return sum(len(s.data) - s.pos for s in self.segs) / self.bytes_per_second

    def settle(self, waiting: int, buffer: float = BUFFER) -> None:
        """Hold playback while less than ``buffer`` s is ready and ``waiting`` more
        utterances are still being made, so a slow stretch is one pause, not many."""
        ready = self.buffered()
        if self.hold:
            if ready > 0 and (ready >= buffer or not waiting):
                self.hold = False
        elif ready == 0 and waiting:
            self.hold = True

    def events(self, played: int) -> list[tuple]:
        """("started", uid), ("word", uid, char) and ("finished", uid), in order, up to
        ``played`` bytes into the stream."""
        out: list[tuple] = []
        while self.segs and self.segs[0].start is not None and self.segs[0].start <= played:
            s = self.segs[0]
            if not s.announced:
                s.announced = True
                out.append(("started", s.uid))
            if s.pos >= len(s.data) and played >= s.start + len(s.data):
                out.append(("finished", s.uid))
                self.segs.pop(0)
                continue
            t = (played - s.start) / self.bytes_per_second
            char = None
            for t0, _, c0, _ in s.words:
                if t0 > t:
                    break
                char = c0
            if char is not None and char != s.char:
                s.char = char
                out.append(("word", s.uid, char))
            break
        return out

    def current(self) -> _Segment | None:
        return next((s for s in self.segs if s.start is not None), None)

    def drop_unstarted(self) -> None:
        self.segs = [s for s in self.segs if s.start is not None]

    def reset(self) -> None:
        self.segs, self.total, self.hold = [], 0, True


def to_format(samples: np.ndarray, rate: int, channels: int, kind: str) -> bytes:
    """Mono float samples at RATE -> bytes for a device (kind "int16" or "float")."""
    if rate != RATE and len(samples):
        n = int(round(len(samples) * rate / RATE))
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    samples = np.clip(samples, -1.0, 1.0)
    if channels > 1:
        samples = np.repeat(samples[:, None], channels, axis=1).reshape(-1)
    if kind == "float":
        return samples.astype("<f4").tobytes()
    return (samples * 32767).astype("<i2").tobytes()


def synthesize(pipeline, text: str, voice: str, speed: float, pause: float,
               skip: bool = False) -> tuple[np.ndarray, list]:
    """Kokoro's audio for one utterance (float samples at RATE, with ``pause`` s of
    silence after; ``skip``: its own silences shortened) and its word timings against
    ``text``. Above NATIVE_SPEED it is made at that speed and sped up, as Kokoro's own speed
    setting drops words when pushed."""
    from .audio_cache import render_speed, skip_silence, stretch
    from .speech_text import align_tokens

    made = render_speed(speed)
    chunks, toks, t0 = [], [], 0.0
    for result in pipeline(text, voice=voice, speed=made, split_pattern=None):
        audio = result.audio
        if audio is None:
            continue
        audio = np.asarray(audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio, dtype=np.float32)
        for t in result.tokens or []:
            ok = t.start_ts is not None and t.end_ts is not None
            toks.append((t.text, t0 + t.start_ts if ok else None, t0 + t.end_ts if ok else None))
        chunks.append(audio)
        t0 += len(audio) / RATE
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if skip:
        audio, toks = skip_silence(audio, toks)
    if speed > made + 1e-3:
        audio = stretch(audio, speed / made)
        k = made / speed
        toks = [(t, a * k if a is not None else None, b * k if b is not None else None) for t, a, b in toks]
    return np.concatenate([audio, np.zeros(int(pause * RATE), dtype=np.float32)]), align_tokens(text, toks)


# -- Kokoro ---------------------------------------------------------------------------------

class _PcmDevice(QIODevice):
    """A never-ending sequential device the sink pulls audio from."""

    def __init__(self, pcm: PcmQueue, parent=None):
        super().__init__(parent)
        self.pcm = pcm

    def readData(self, maxlen: int) -> bytes:
        return self.pcm.read(min(maxlen, 1 << 16))

    def writeData(self, data) -> int:
        return 0

    def bytesAvailable(self) -> int:
        return (1 << 16) + super().bytesAvailable()

    def isSequential(self) -> bool:
        return True


def _fetch(name: str, note: str, put) -> str:
    from huggingface_hub import hf_hub_download

    try:
        return hf_hub_download(REPO, name, local_files_only=True)
    except Exception:  # noqa: BLE001 - not cached yet
        put(("status", note))
        return hf_hub_download(REPO, name)


class _Kokoro:
    """The model and its pipelines and voices, loaded the first time a sentence needs it."""

    def __init__(self, put):
        self.put, self.model = put, None
        self.pipelines: dict[str, object] = {}
        self.packs: dict[str, str] = {}

    def load(self, voice: str) -> None:
        if self.model is not None:
            return
        self.put(("status", "Loading Kokoro…"))
        from kokoro import KModel

        config = _fetch("config.json", "Downloading Kokoro…", self.put)
        weights = _fetch("kokoro-v1_0.pth", "Downloading Kokoro…", self.put)
        self.model = KModel(repo_id=REPO, config=config, model=weights).eval()
        synthesize(self.pipeline(voice[0]), "Ready.", self.pack(voice), 1.0, 0.0)  # the first run is slow
        self.put(("status", ""))

    def pipeline(self, lang: str):
        if lang not in self.pipelines:
            from kokoro import KPipeline

            self.pipelines[lang] = KPipeline(lang_code=lang, repo_id=REPO, model=self.model)
        return self.pipelines[lang]

    def pack(self, voice: str) -> str:
        if voice not in self.packs:
            self.packs[voice] = _fetch(f"voices/{voice}.pt", f"Downloading the {voice} voice…", self.put)
            if self.model is not None:
                self.put(("status", ""))
        return self.packs[voice]


def job_audio(job, cache, kokoro) -> tuple[np.ndarray, list]:
    """A sentence's samples (with its pause after) and word timings: rendered on Colab
    if the cache has it, else made by Kokoro now (whose loading errors propagate)."""
    _, _, text, voice, speed, pause, skip = job
    silence = np.zeros(int(pause * RATE), dtype=np.float32)
    if not text.strip():
        return silence, []
    if cache is not None and (hit := cache.get(voice, text, speed, skip)) is not None:
        return np.concatenate([hit[0], silence]), hit[1]
    kokoro.load(voice)
    try:
        return synthesize(kokoro.pipeline(voice[0]), text, kokoro.pack(voice), speed, pause, skip)
    except Exception as e:  # noqa: BLE001 - skip the sentence, keep reading
        kokoro.put(("status", f"Couldn't read a sentence: {e}"))
        return silence, []


def kokoro_worker(jobs, results, gen, cache_dir: str | None) -> None:
    """Kokoro's own process: makes each job's audio (from the Colab cache when it can),
    skipping jobs from before the app's latest clear() (``gen`` is shared with it).
    Other messages: ("warm", voice) loads the model now, ("cache", folder) switches cache."""
    import multiprocessing

    from .audio_cache import AudioCache

    put = results.put
    parent = multiprocessing.parent_process()
    if parent is not None:  # don't outlive the app if it dies without shutting this down
        threading.Thread(target=lambda: (parent.join(), os._exit(0)), daemon=True).start()
    try:
        os.nice(5)  # the reader stays responsive while this works flat out
    except OSError:
        pass
    cache = AudioCache(cache_dir) if cache_dir else None
    kokoro = _Kokoro(put)
    while (job := jobs.get()) is not None:
        try:
            if job[0] == "cache":
                cache = AudioCache(job[1]) if job[1] else None
                continue
            if job[0] == "warm":
                kokoro.load(job[1])
                continue
            if job[0] != gen.value:
                continue
            samples, words = job_audio(job, cache, kokoro)
        except Exception as e:  # noqa: BLE001 - Kokoro can't load: the app falls back
            put(("failed", f"Kokoro couldn't start: {e}"))
            return
        put(("ready", job[0], job[1], samples, words))


class _Bridge(QObject):
    """Carries the Kokoro process's messages to the GUI thread (queued connections)."""

    ready = Signal(int, int, object, object)  # generation, uid, samples, words
    status = Signal(str)
    failed = Signal(str)


class KokoroEngine(Engine):
    lookahead = 1
    lookahead_chars = 600  # about 40 s of speech
    word_timing = True
    skips_silence = True
    name = "kokoro"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.voice, self.speed, self.skip = "af_heart", 1.0, False
        self._cache_dir: str | None = None
        self.cache = AudioCache(None)
        self._gen = 0
        self._proc = None  # the Kokoro process, with its job and result queues and shared generation
        self._jobs = self._results = self._shared_gen = None
        self._bridge = _Bridge(self)
        self._bridge.ready.connect(self._on_ready)
        self._bridge.status.connect(self.status)
        self._bridge.failed.connect(self._fail)
        self._sink = None
        self._dev: _PcmDevice | None = None
        self._pcm = PcmQueue()
        self._fmt = (RATE, 1, "int16")
        self._paused = False
        self._dead = False
        self._closing = False
        self._waiting = 0  # utterances enqueued and not yet made
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._watch = QTimer(self, interval=1000)  # notices the process dying
        self._watch.timeout.connect(self._check_alive)
        self._devices = None  # QMediaDevices, made with the sink
        self._device_id = b""

    def voices(self) -> list[tuple[str, str]]:
        return KOKORO_VOICES

    # -- the process ------------------------------------------------------------------
    def _start(self) -> None:
        if self._proc is not None:
            return
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")  # never fork a process running Qt
        self._jobs, self._results, self._shared_gen = ctx.Queue(), ctx.Queue(), ctx.Value("i", self._gen)
        self._proc = ctx.Process(
            target=kokoro_worker, args=(self._jobs, self._results, self._shared_gen, self._cache_dir),
            daemon=True, name="kokoro",
        )
        self._proc.start()
        threading.Thread(target=self._listen, args=(self._results,), daemon=True, name="kokoro-results").start()
        self._watch.start()

    def _listen(self, results) -> None:
        while True:
            try:
                msg = results.get()
            except (EOFError, OSError, ValueError, TypeError):  # the queue closed under it at exit
                return
            if msg is None:
                return
            if msg[0] == "ready":
                self._bridge.ready.emit(*msg[1:])
            elif msg[0] == "status":
                self._bridge.status.emit(msg[1])
            else:
                self._bridge.failed.emit(msg[1])

    def _check_alive(self) -> None:
        if self._proc is not None and not self._proc.is_alive() and not self._closing and not self._dead:
            self._fail(f"Kokoro couldn't start: its process stopped (exit code {self._proc.exitcode})")

    def _bump(self) -> None:
        self._gen += 1
        if self._shared_gen is not None:
            self._shared_gen.value = self._gen

    # -- the GUI side ---------------------------------------------------------------
    def _fail(self, message: str) -> None:
        if "couldn't start" in message:
            if self._dead:
                return
            self._dead = True
            self._watch.stop()
        self.failed.emit(message)

    def _ensure_sink(self) -> bool:
        if self._sink is not None:
            return True
        from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

        device = QMediaDevices.defaultAudioOutput()
        if self._devices is None:
            # A sink plays on the device it was made for, even after the default changes.
            self._devices = QMediaDevices(self)
            self._devices.audioOutputsChanged.connect(self._outputs_changed)
        if device.isNull():
            self._fail("Kokoro couldn't start: there is no audio output device")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        kinds = {QAudioFormat.SampleFormat.Int16: "int16", QAudioFormat.SampleFormat.Float: "float"}
        if fmt.sampleFormat() not in kinds:
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._fmt = (fmt.sampleRate(), fmt.channelCount(), kinds.get(fmt.sampleFormat(), "int16"))
        width = 4 if self._fmt[2] == "float" else 2
        self._pcm = PcmQueue(fmt.sampleRate() * fmt.channelCount() * width, fmt.channelCount() * width)
        self._device_id = bytes(device.id())
        self._sink = QAudioSink(device, fmt, self)
        self._sink.setBufferSize(int(self._pcm.bytes_per_second * 0.1))
        self._sink.stateChanged.connect(self._sink_state)
        self._audio_error = QAudio.Error.NoError
        self._dev = _PcmDevice(self._pcm, self)
        self._dev.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.start(self._dev)
        if self._paused:
            self._sink.suspend()
        self._timer.start()
        return True

    def _outputs_changed(self) -> None:
        """Move to the new default output: the next sentence made gets a sink there."""
        from PySide6.QtMultimedia import QMediaDevices

        if self._sink is None or self._closing or bytes(QMediaDevices.defaultAudioOutput().id()) == self._device_id:
            return
        self._bump()
        self._waiting = 0
        self._drop_sink()
        self.outputChanged.emit()

    def _drop_sink(self) -> None:
        """Throw the sink away (the next sentence made gets a new one). Starting a stopped
        QAudioSink again leaves its old PulseAudio stream behind, so it is never reused."""
        self._timer.stop()
        self._sink.stop()
        self._sink.deleteLater()
        self._sink = None
        self._dev.close()
        self._dev.deleteLater()
        self._dev = None
        self._pcm.reset()

    def _sink_state(self, _state) -> None:
        from PySide6.QtMultimedia import QAudio

        err = self._sink.error()
        if err in (QAudio.Error.OpenError, QAudio.Error.FatalError):
            self._fail(f"Kokoro couldn't start: the audio device failed ({err.name})")

    def _played(self) -> int:
        b = self._pcm.bytes_per_second
        played = self._sink.processedUSecs() * b // 1_000_000 - int(LATENCY * b)
        return max(played, -1)

    def _tick(self) -> None:
        if self._sink is None or self._paused:
            return
        self._pcm.settle(self._waiting)
        for ev in self._pcm.events(self._played()):
            getattr(self, ev[0]).emit(*ev[1:])

    def warm(self) -> None:
        if not self._dead:
            self._start()
            if not len(self.cache):  # a book read from Colab's audio may never need the model
                self._jobs.put(("warm", self.voice))

    def has(self, text: str) -> bool:
        return self.cache.has(self.voice, text)

    def cached(self) -> int:
        return len(self.cache)

    def set_cache(self, folder) -> None:
        self._cache_dir = str(folder) if folder else None
        self.cache = AudioCache(folder)
        if self._proc is not None:
            self._jobs.put(("cache", self._cache_dir))

    def enqueue(self, uid: int, text: str, pause: float) -> None:
        if self._dead:
            return
        self._start()
        self._waiting += 1
        self._jobs.put((self._gen, uid, text, self.voice, self.speed, pause, self.skip))

    def _on_ready(self, gen: int, uid: int, samples, words) -> None:
        if gen != self._gen or not self._ensure_sink():
            return
        self._waiting = max(self._waiting - 1, 0)
        rate, channels, kind = self._fmt
        self._pcm.append(uid, to_format(samples, rate, channels, kind), words)
        self._pcm.settle(self._waiting)

    def clear(self, keep_current: bool = False) -> None:
        self._bump()
        self._waiting = 0
        if self._sink is None:
            return
        if keep_current:
            self._pcm.drop_unstarted()
            return
        self._drop_sink()

    def pause(self) -> None:
        self._paused = True
        if self._sink is not None:
            self._sink.suspend()

    def resume(self) -> None:
        self._paused = False
        if self._sink is not None:
            self._sink.resume()

    def is_paused(self) -> bool:
        return self._paused

    def set_speed(self, speed: float) -> None:
        self.speed = speed

    def set_skip_silence(self, on: bool) -> None:
        self.skip = on

    def set_voice(self, voice: str) -> None:
        self.voice = voice

    def shutdown(self) -> None:
        self._closing = True
        self._bump()
        self._timer.stop()
        self._watch.stop()
        if self._sink is not None:
            self._sink.stop()
        if self._proc is not None:
            try:
                self._jobs.put(None)
                self._proc.join(0.3)
            finally:
                if self._proc.is_alive():
                    self._proc.kill()  # mid-sentence: no need to wait for it
                    self._proc.join(2)
                self._results.put(None)  # ends _listen
                for q in (self._jobs, self._results):
                    q.cancel_join_thread()


# -- speech-dispatcher -------------------------------------------------------------------------

def speechd_rate(speed: float) -> float:
    """A speed factor -> QTextToSpeech's rate (-1 to 1, 0 is normal)."""
    return max(-1.0, min(1.0, math.log(speed, MAX_SPEED)))


class SpeechdEngine(Engine):
    name = "speechd"

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtTextToSpeech import QTextToSpeech

        self._queue: list[tuple[int, str]] = []
        self._uid: int | None = None
        self._stopping = False
        self._paused = False
        self._voice = ""
        self.tts = None
        if "speechd" not in QTextToSpeech.availableEngines():
            QTimer.singleShot(0, lambda: self.failed.emit("speech-dispatcher isn't available"))
            return
        self.tts = QTextToSpeech("speechd", self)
        self.tts.stateChanged.connect(self._state)

    def voices(self) -> list[tuple[str, str]]:
        if self.tts is None:
            return []
        return [(v.name(), v.name()) for v in self.tts.availableVoices()]

    def _state(self, state) -> None:
        from PySide6.QtTextToSpeech import QTextToSpeech

        if state == QTextToSpeech.State.Error:
            self.failed.emit(f"speech-dispatcher failed: {self.tts.errorString()}")
        elif state == QTextToSpeech.State.Speaking and self._uid is not None:
            self.started.emit(self._uid)
        elif state == QTextToSpeech.State.Ready:
            if self._stopping:
                self._stopping = False
            elif self._uid is not None:
                uid, self._uid = self._uid, None
                self.finished.emit(uid)
            self._next()

    def _next(self) -> None:
        from PySide6.QtTextToSpeech import QTextToSpeech

        if self.tts is None or self._uid is not None or not self._queue or self._paused:
            return
        if self.tts.state() not in (QTextToSpeech.State.Ready,):
            return
        self._uid, text = self._queue.pop(0)
        if text.strip():
            self.tts.say(text)
        else:
            uid, self._uid = self._uid, None
            self.started.emit(uid)
            self.finished.emit(uid)
            self._next()

    def enqueue(self, uid: int, text: str, pause: float) -> None:
        self._queue.append((uid, text))
        self._next()

    def clear(self, keep_current: bool = False) -> None:
        self._queue = []
        if keep_current or self.tts is None:
            return
        if self._uid is not None:
            self._uid = None
            self._stopping = True
            from PySide6.QtTextToSpeech import QTextToSpeech

            self.tts.stop(QTextToSpeech.BoundaryHint.Immediate)

    def pause(self) -> None:
        self._paused = True
        if self.tts is not None and self._uid is not None:
            from PySide6.QtTextToSpeech import QTextToSpeech

            self.tts.pause(QTextToSpeech.BoundaryHint.Immediate)

    def resume(self) -> None:
        self._paused = False
        if self.tts is None:
            return
        if self._uid is not None:
            self.tts.resume()
        else:
            self._next()

    def is_paused(self) -> bool:
        return self._paused

    def set_speed(self, speed: float) -> None:
        if self.tts is not None:
            self.tts.setRate(speechd_rate(speed))

    def set_voice(self, voice: str) -> None:
        if self.tts is None:
            return
        for v in self.tts.availableVoices():
            if v.name() == voice:
                self.tts.setVoice(v)
                return

    def shutdown(self) -> None:
        self.clear()
