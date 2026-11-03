"""Read-aloud audio rendered ahead of time on Colab (Qt-free).

colab_render.py has a Colab GPU render a book's sentences with Kokoro (assets/kokoro_render.py)
into ``audio/part-*.opus`` (Ogg Opus) plus ``part-*.json``, which maps each sentence's key to
``[speed, start sample, samples, tokens]``, and downloads the parts into the book's folder in
the app's data folder, where ``AudioCache`` plays them, stretching the audio when the reading
speed differs from the one it was rendered at. Parts the old Drive notebook made
(``part-NNNN``) are still copied from the shared Drive folder (``sync``)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import OrderedDict
from pathlib import Path

import numpy as np

RATE = 24000
# Kokoro's own speed setting drops sounds and whole words above this (Whisper's word error
# rate on textbook sentences: 3% here, 6% at 1.5×, 16% at 2×), so faster reading is made
# at this speed and then sped up (stretch), which keeps every word.
NATIVE_SPEED = 1.3
OPEN_PARTS = 4  # decoders kept open


def book_key(file_hash: str) -> str:
    return file_hash[:16]


def sentence_key(voice: str, text: str) -> str:
    """What a sentence's audio is filed under: its voice and exact spoken text."""
    return hashlib.sha1(f"{voice}\n{text}".encode()).hexdigest()[:20]


def render_speed(speed: float) -> float:
    """The speed to make audio at for reading at ``speed`` (faster is stretched)."""
    return round(min(speed, NATIVE_SPEED), 2)


def _drive_parts(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("part-*.json") if re.fullmatch(r"part-\d+\.json", p.name))


def sync(src: Path, dst: Path) -> int:
    """Copy parts the Drive notebook rendered that are new or changed from the shared folder,
    and drop the ones removed there; how many changed. Only its parts (part-NNNN) are touched,
    not the ones colab_render downloads. Each part's audio is copied before its index (and its
    index removed first), so a half-copied part is never used."""
    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for index in _drive_parts(dst):
        if not (src / index.name).exists():
            index.unlink()
            index.with_suffix(".opus").unlink(missing_ok=True)
            copied += 1
    for index in _drive_parts(src):
        audio = index.with_suffix(".opus")
        if not audio.exists():
            continue
        for f in (audio, index):
            target = dst / f.name
            st = f.stat()
            if target.exists() and target.stat().st_size == st.st_size and target.stat().st_mtime >= st.st_mtime:
                continue
            tmp = target.with_name(target.name + ".tmp")
            shutil.copyfile(f, tmp)
            os.replace(tmp, target)
            copied += f is index
    return copied


class AudioCache:
    """Rendered sentences in a folder of parts, looked up by voice and text."""

    def __init__(self, folder: Path | str | None):
        self.folder = Path(folder) if folder else None
        self.index: dict[str, list[tuple[float, str, int, int, list]]] = {}
        self._open: OrderedDict[str, object] = OrderedDict()
        if self.folder and self.folder.is_dir():
            for path in sorted(self.folder.glob("part-*.json")):
                try:
                    items = json.loads(path.read_text())["items"]
                except (OSError, ValueError, KeyError):
                    continue
                audio = path.with_suffix(".opus").name
                for key, (speed, start, n, tokens) in items.items():
                    self.index.setdefault(key, []).append((speed, audio, start, n, tokens))

    def __len__(self) -> int:
        return len(self.index)

    def has(self, voice: str, text: str) -> bool:
        return sentence_key(voice, text) in self.index

    def count(self, keys) -> int:
        return sum(k in self.index for k in keys)

    def get(self, voice: str, text: str, speed: float) -> tuple[np.ndarray, list] | None:
        """The sentence's samples at ``speed`` and its word timings (as tts_engines makes
        them), or None if it wasn't rendered."""
        from .speech_text import align_tokens

        entries = self.index.get(sentence_key(voice, text))
        if not entries:
            return None
        # Audio made above NATIVE_SPEED (before that limit) only when there is nothing better.
        rendered, audio, start, n, tokens = min(
            entries, key=lambda e: (e[0] > NATIVE_SPEED + 0.01, abs(e[0] - speed)))
        samples = self._read(audio, start, n)
        if samples is None:
            return None
        factor = speed / rendered
        if abs(factor - 1) > 0.02:
            samples = stretch(samples, factor)
        else:
            factor = 1.0
        toks = [(t, a / factor if a is not None else None, b / factor if b is not None else None) for t, a, b in tokens]
        return samples, align_tokens(text, toks)

    def _read(self, audio: str, start: int, n: int) -> np.ndarray | None:
        import soundfile as sf

        try:
            f = self._open.get(audio)
            if f is None:
                f = self._open[audio] = sf.SoundFile(self.folder / audio)
                while len(self._open) > OPEN_PARTS:
                    self._open.popitem(last=False)[1].close()
            self._open.move_to_end(audio)
            f.seek(start)
            return f.read(n, dtype="float32")
        except (OSError, RuntimeError, ValueError):
            return None


def stretch(x: np.ndarray, rate: float, frame: int = 960, tol: int = 240) -> np.ndarray:
    """Speech played ``rate`` times as fast at the same pitch (WSOLA: overlapping frames,
    each shifted by up to ``tol`` samples to line up with how the last one carried on)."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) < frame or abs(rate - 1) < 1e-3:
        return x
    hop_out = frame // 2
    hop_in = hop_out * rate
    window = np.hanning(frame).astype(np.float32)
    n_frames = int(np.ceil(len(x) / hop_in))
    out = np.zeros(n_frames * hop_out + frame, np.float32)
    weight = np.zeros_like(out)
    pad = np.concatenate([np.zeros(tol, np.float32), x, np.zeros(frame + 2 * tol + int(hop_in) + 1, np.float32)])
    prev = None  # where the last frame was taken from, in pad
    for k in range(n_frames):
        nominal = int(k * hop_in) + tol
        if prev is None:
            pos = nominal
        else:
            # The frame that would have followed the last one naturally, matched on a
            # coarse grid of the search region (every 4th sample) for speed.
            follow = pad[prev + hop_out: prev + hop_out + frame:4]
            lo = max(nominal - tol, 0)
            region = pad[lo: nominal + tol + frame:4]
            if len(region) >= len(follow) and np.any(follow):
                corr = np.correlate(region, follow, "valid")
                pos = lo + 4 * int(np.argmax(corr))
            else:
                pos = nominal
        seg = pad[pos: pos + frame]
        if len(seg) < frame:
            break
        out[k * hop_out: k * hop_out + frame] += seg * window
        weight[k * hop_out: k * hop_out + frame] += window
        prev = pos
    n = int(round(len(x) / rate))
    weight = weight[:n]
    return out[:n] / np.where(weight > 1e-3, weight, 1)
