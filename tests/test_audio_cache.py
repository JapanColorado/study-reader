import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pymupdf
import pytest
import soundfile as sf

from study_reader.audio_cache import RATE, AudioCache, render_speed, sentence_key, stretch, sync
from study_reader.db import Database

ASSETS = Path(__file__).parent.parent / "src" / "study_reader" / "assets"


def _tone(seconds, freq=200.0):
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _part(folder: Path, name: str, sentences: dict, speed: float = 1.0):
    """A rendered part as the notebook writes it: {text: seconds}."""
    folder.mkdir(parents=True, exist_ok=True)
    clips, items, pos = [], {}, 0
    for text, seconds in sentences.items():
        clip = _tone(seconds)
        words = text.split()
        step = seconds / len(words)
        tokens = [[w, i * step, (i + 1) * step] for i, w in enumerate(words)] + [[".", None, None]]
        items[sentence_key("af_heart", text)] = [speed, pos, len(clip), tokens]
        clips.append(clip)
        pos += len(clip)
    sf.write(folder / f"{name}.opus", np.concatenate(clips), RATE, format="OGG", subtype="OPUS")
    (folder / f"{name}.json").write_text(json.dumps({"version": 1, "rate": RATE, "items": items}))


def test_cached_sentences_play_at_any_speed(tmp_path):
    _part(tmp_path, "part-0000", {"The zorblatt hums.": 1.5, "It wobbles.": 1.0}, speed=1.2)
    cache = AudioCache(tmp_path)
    assert len(cache) == 2 and cache.has("af_heart", "It wobbles.") and not cache.has("am_michael", "It wobbles.")
    samples, words = cache.get("af_heart", "The zorblatt hums.", 1.2)
    assert len(samples) == int(1.5 * RATE)
    assert np.corrcoef(samples[2000:3000], _tone(1.5)[2000:3000])[0, 1] > 0.99  # the right stretch of audio
    assert words == [(0.0, 0.5, 0, 3), (0.5, 1.0, 4, 12), (1.0, 1.5, 13, 18)]  # aligned onto the text
    fast, fast_words = cache.get("af_heart", "The zorblatt hums.", 1.8)
    assert len(fast) == round(1.5 * RATE * 1.2 / 1.8)  # stretched from the speed it was made at
    assert fast_words[1][0] == pytest.approx(0.5 * 1.2 / 1.8)
    second, _ = cache.get("af_heart", "It wobbles.", 1.2)
    assert len(second) == RATE
    assert cache.get("af_heart", "Never rendered.", 1.2) is None
    assert AudioCache(tmp_path / "nothing").get("af_heart", "It wobbles.", 1.0) is None


def test_sync_copies_new_parts_and_skips_unfinished_ones(tmp_path):
    src, dst = tmp_path / "drive", tmp_path / "local"
    _part(src, "part-0000", {"One fish.": 0.5})
    (src / "part-0001.json").write_text("{}")  # its audio hasn't arrived yet
    assert sync(src, dst) == 1 and sorted(p.name for p in dst.iterdir()) == ["part-0000.json", "part-0000.opus"]
    assert sync(src, dst) == 0
    _part(src, "part-0001", {"Two fish.": 0.5})
    assert sync(src, dst) == 1 and len(AudioCache(dst)) == 2
    assert sync(tmp_path / "absent", dst) == 0
    (src / "part-0000.json").unlink()  # the notebook removed a part made at an old speed
    (src / "part-0000.opus").unlink()
    assert sync(src, dst) == 1 and sorted(p.name for p in dst.iterdir()) == ["part-0001.json", "part-0001.opus"]
    _part(dst, "part-a1b2c3-0000", {"Red fish.": 0.5})  # downloaded from a Colab VM, not on Drive
    assert sync(src, dst) == 0 and (dst / "part-a1b2c3-0000.json").exists()


def test_stretch_keeps_pitch_and_scales_length():
    x = _tone(2.0, 180)
    for rate in (0.75, 1.5, 2.0):
        y = stretch(x, rate)
        assert len(y) == round(len(x) / rate)
        spectrum = np.abs(np.fft.rfft(y[RATE // 4: RATE // 4 + RATE // 2]))
        assert np.argmax(spectrum) * 2 == pytest.approx(180, abs=4)  # bins of 2 Hz
    assert stretch(x, 1.0) is not None and len(stretch(x[:100], 1.5)) == 100


def test_job_audio_uses_the_cache_before_kokoro(tmp_path):
    from study_reader.tts_engines import job_audio

    _part(tmp_path, "part-0000", {"It wobbles.": 1.0})
    cache = AudioCache(tmp_path)
    loads = []

    class Kokoro:
        def load(self, voice):
            loads.append(voice)

        def pipeline(self, lang):
            tok = SimpleNamespace(text="Hums", start_ts=0.0, end_ts=0.2)
            return lambda text, **kw: iter([SimpleNamespace(audio=np.ones(2400, np.float32), tokens=[tok])])

        def pack(self, voice):
            return voice

    audio, words = job_audio((0, 1, "It wobbles.", "af_heart", 1.0, 0.5), cache, Kokoro())
    assert len(audio) == RATE + RATE // 2 and loads == []
    audio, words = job_audio((0, 2, "Hums.", "af_heart", 1.0, 0.0), cache, Kokoro())
    assert len(audio) == 2400 and words == [(0.0, 0.2, 0, 4)] and loads == ["af_heart"]
    audio, _ = job_audio((0, 3, "  ", "af_heart", 1.0, 0.1), None, Kokoro())
    assert len(audio) == RATE // 10


def test_book_sentences_match_what_is_read(pdf_path):
    from study_reader.document import Document
    from study_reader.render_dialog import book_sentences
    from study_reader.speech_pages import BookText

    pages = []
    texts = book_sentences(pdf_path, lambda p, n: pages.append((p, n)))
    assert texts == [u.text for u in BookText(Document(pdf_path)).utterances((0, 0))]
    assert pages == [(0, 3), (1, 3), (2, 3)]


def test_fast_reading_is_made_at_kokoros_safe_speed_and_sped_up(tmp_path):
    from study_reader.audio_cache import NATIVE_SPEED
    from study_reader.tts_engines import synthesize

    assert render_speed(2.0) == NATIVE_SPEED and render_speed(1.1) == 1.1
    calls = []
    tok = SimpleNamespace(text="One", start_ts=0.2, end_ts=0.6)

    def pipeline(text, **kw):
        calls.append(kw["speed"])
        return iter([SimpleNamespace(audio=_tone(1.3), tokens=[tok])])

    audio, words = synthesize(pipeline, "One.", "af_heart", 2.0, 0.0)
    assert calls == [NATIVE_SPEED]
    assert len(audio) == round(1.3 * RATE * NATIVE_SPEED / 2.0)
    assert words[0][0] == pytest.approx(0.2 * NATIVE_SPEED / 2.0)
    synthesize(pipeline, "One.", "af_heart", 1.1, 0.0)
    assert calls[-1] == 1.1  # slower than the limit: Kokoro's own speed


def test_audio_made_too_fast_is_used_only_without_better(tmp_path):
    _part(tmp_path, "part-0000", {"It wobbles.": 0.5}, speed=2.0)
    assert len(AudioCache(tmp_path).get("af_heart", "It wobbles.", 2.0)[0]) == RATE // 2
    _part(tmp_path, "part-0001", {"It wobbles.": 1.0}, speed=1.3)
    samples, _ = AudioCache(tmp_path).get("af_heart", "It wobbles.", 2.0)
    assert len(samples) == round(RATE * 1.3 / 2.0)  # the 1.3× render, sped up
