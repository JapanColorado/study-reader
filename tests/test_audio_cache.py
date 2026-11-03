import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pymupdf
import pytest
import soundfile as sf

from study_reader.audio_cache import (
    RATE, AudioCache, read_request, sentence_key, stretch, sync, write_request,
)
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


def test_requests_list_each_sentence_once(tmp_path):
    n = write_request(tmp_path / "b", "Zorblatts", "af_heart", 1.25, ["One.", "Two.", "One."])
    req = read_request(tmp_path / "b")
    assert n == 2 and req["speed"] == 1.25 and req["voice"] == "af_heart"
    assert req["sentences"] == [[sentence_key("af_heart", "One."), "One."], [sentence_key("af_heart", "Two."), "Two."]]
    assert sentence_key("af_heart", "One.") != sentence_key("am_michael", "One.")
    assert read_request(tmp_path / "missing") is None


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


def test_render_notebook_is_valid_and_points_at_the_default_folder():
    from study_reader.render_dialog import DEFAULT_FOLDER

    nb = json.loads((ASSETS / "colab_render.ipynb").read_text())
    code = [c["source"] for c in nb["cells"] if c["cell_type"] == "code"]
    for src in code:
        lines = [line for line in src.splitlines() if not line.lstrip().startswith(("!", "%%"))]
        ast.parse("\n".join(lines))
    assert any(src.startswith("%%writefile /content/render_books.py") for src in code)
    assert "/content/kokoro-env/bin/python" in code[-1]  # Kokoro runs in its own Python 3.12
    assert f'FOLDER = "/content/drive/MyDrive/{DEFAULT_FOLDER.name}"' in code[0]


def test_book_sentences_match_what_is_read(pdf_path):
    from study_reader.document import Document
    from study_reader.render_dialog import book_sentences
    from study_reader.speech_pages import BookText

    pages = []
    texts = book_sentences(pdf_path, lambda p, n: pages.append((p, n)))
    assert texts == [u.text for u in BookText(Document(pdf_path)).utterances((0, 0))]
    assert pages == [(0, 3), (1, 3), (2, 3)]


def _wait(app, cond, timeout=10, msg=lambda: ""):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    assert cond(), msg()


def test_render_dialog_sends_and_copies(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.document import file_hash
    from study_reader.render_dialog import (
        FOLDER_SETTING, NOTEBOOK, AudioSync, RenderDialog, local_audio, remote_audio,
    )

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    db.set_setting(FOLDER_SETTING, str(tmp_path / "drive" / "study-reader-tts"))
    (tmp_path / "drive").mkdir()
    book = db.upsert_book(file_hash(pdf_path), pdf_path, "Zorblatts", 3)
    sync_ = AudioSync(db)
    dlg = RenderDialog(db, book, "af_heart", 1.3, sync_)
    _wait(app, lambda: dlg.send_btn.isEnabled())
    assert "Not sent yet" in dlg.status.text()
    dlg.send()
    _wait(app, lambda: "Sent 6 sentences" in dlg.status.text(), msg=dlg.status.text)
    remote = remote_audio(db, book["hash"])
    assert read_request(remote)["speed"] == 1.3
    assert (remote.parent.parent / NOTEBOOK).exists()
    texts = [t for _, t in read_request(remote)["sentences"]]
    _part(remote / "audio", "part-0000", {t: 0.5 for t in texts[:2]})
    (remote / "status.json").write_text(json.dumps({"rendered": 2, "total": 6}))
    dlg.copy()
    _wait(app, lambda: "Copied here: 2 of 6" in dlg.status.text(), msg=dlg.status.text)
    assert "Rendered on Colab: 2 of 6" in dlg.status.text()
    assert len(AudioCache(local_audio(book["hash"]))) == 2
    dlg.voice = "am_michael"
    dlg.refresh()
    _wait(app, lambda: "send again" in dlg.status.text())


def test_fast_reading_is_made_at_kokoros_safe_speed_and_sped_up(tmp_path):
    from study_reader.audio_cache import NATIVE_SPEED
    from study_reader.tts_engines import synthesize

    write_request(tmp_path, "Z", "af_heart", 2.0, ["One."])
    assert read_request(tmp_path)["speed"] == NATIVE_SPEED
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
