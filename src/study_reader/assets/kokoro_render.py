"""Renders the books Study Reader queued on this Colab VM with Kokoro (run on the VM by
colab_render.py, in a Python 3.12 environment, since Kokoro needs one).

Usage: python kokoro_render.py ROOT PART_SENTENCES OPUS_QUALITY

Each ``ROOT/books/<key>/request.json`` ({"run", "title", "voice", "speed", "sentences":
[[key, text], …]}) is rendered, oldest first, into ``ROOT/books/<key>/audio/part-<run>-NNNN.opus``
(Ogg Opus) plus ``.json`` ({"version", "rate", "items": {key: [speed, start, samples, tokens]}}),
each part's audio written before its index. ``status.json`` beside the request says how far it
has got. Books queued while it runs are picked up; it exits when none are left."""

import glob
import json
import os
import re
import sys
import tempfile
import time

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

ROOT, PART_SENTENCES, OPUS_QUALITY = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
RATE = 24000
TMP = tempfile.mkdtemp(dir=ROOT)  # on the same disk, so finished files are renamed into place
assert torch.cuda.is_available(), "this Colab VM has no GPU"
pipelines = {}


def pipeline(lang):
    if lang not in pipelines:
        pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M", device="cuda")
    return pipelines[lang]


def speak(text, voice, speed):
    """Samples and Kokoro's timed tokens [text, start s, end s] for one sentence."""
    parts, tokens, t0 = [], [], 0.0
    for r in pipeline(voice[0])(text, voice=voice, speed=speed, split_pattern=None):
        if r.audio is None:
            continue
        audio = r.audio.detach().cpu().numpy().astype(np.float32)
        for t in r.tokens or []:
            timed = t.start_ts is not None and t.end_ts is not None
            tokens.append([t.text, t0 + t.start_ts if timed else None, t0 + t.end_ts if timed else None])
        parts.append(audio)
        t0 += len(audio) / RATE
    return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), tokens


def write_json(path, data):
    tmp = os.path.join(TMP, os.path.basename(path))
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pending():
    """Queued books not finished yet, oldest request first."""
    books = []
    for path in glob.glob(f"{ROOT}/books/*/request.json"):
        book_dir = os.path.dirname(path)
        request = read_json(path)
        status = read_json(f"{book_dir}/status.json") or {}
        if request and not (status.get("done") and status.get("run") == request["run"]):
            books.append((os.path.getmtime(path), book_dir, request))
    return [(d, r) for _, d, r in sorted(books, key=lambda b: b[0])]


def render_book(book_dir, request):
    run, voice, speed, sentences = request["run"], request["voice"], request["speed"], request["sentences"]
    audio_dir = f"{book_dir}/audio"
    os.makedirs(audio_dir, exist_ok=True)
    mine = sorted(glob.glob(f"{audio_dir}/part-{run}-*.json"))
    done = set()
    for path in mine:  # a restarted run carries on
        done |= set((read_json(path) or {}).get("items", {}))
    todo = [(k, t) for k, t in sentences if k not in done]
    number = max((int(re.search(r"-(\d+)\.json$", p).group(1)) for p in mine), default=-1) + 1
    print(f"{request['title']}: {len(sentences) - len(todo):,} of {len(sentences):,} sentences already rendered", flush=True)
    start, seconds = time.time(), 0.0

    def status(finished):
        took = time.time() - start
        write_json(f"{book_dir}/status.json", {
            "run": run, "rendered": len(done), "total": len(sentences), "done": finished,
            "speech": seconds, "took": took, "updated": time.time()})

    status(False)
    for i in range(0, len(todo), PART_SENTENCES):
        clips, items, pos = [], {}, 0
        for key, text in todo[i:i + PART_SENTENCES]:
            audio, tokens = speak(text, voice, speed)
            items[key] = [speed, pos, len(audio), tokens]
            clips.append(audio)
            pos += len(audio)
        audio = np.concatenate(clips) if clips else np.zeros(0, np.float32)
        name = f"part-{run}-{number:04d}"
        sf.write(f"{TMP}/{name}.opus", audio, RATE, format="OGG", subtype="OPUS", compression_level=OPUS_QUALITY)
        os.replace(f"{TMP}/{name}.opus", f"{audio_dir}/{name}.opus")
        write_json(f"{audio_dir}/{name}.json", {"version": 1, "rate": RATE, "items": items})
        number += 1
        done |= set(items)
        seconds += len(audio) / RATE
        status(False)
        took = time.time() - start
        print(f"  {len(done):,} of {len(sentences):,} · {seconds / 3600:.1f} h of speech in {took / 60:.1f} min "
              f"({seconds / max(took, 1e-9):.0f}× real time)", flush=True)
    status(True)


while True:
    books = pending()
    if not books:
        break
    render_book(*books[0])
print("Done.", flush=True)
