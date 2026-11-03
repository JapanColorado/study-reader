"""Rendering read-aloud audio on a Colab GPU through the ``colab`` CLI (Qt-free).

The app queues a book by writing ``<data>/colab/requests/<key>.json`` (``queue``) and starts
the watcher, ``python -m study_reader.colab_render`` (``start_watcher``), detached so it
outlives the app; a lock keeps it to one. The watcher rents a VM (``colab new``), uploads
each queued book's sentences that have no audio here yet at its speed, and starts
assets/kokoro_render.py in the background there (``run.sh`` installs Kokoro first). Every
``POLL`` seconds it downloads finished parts straight into the book's audio folder
(audio_cache's format, named ``part-<run>-NNNN`` so they never collide with parts copied
from Drive) and, once every book is done, releases the VM (``colab stop``). ``state.json``
says how it is going, for the Render dialog; a ``stop`` file asks it to release the VM and
drop the queue, and it leaves a ``failed`` file (the reason) when it gives up.

The CLI keeps its sessions in our own ``sessions.json`` (``--config``), so the user's
other Colab sessions are never touched. ``colab exec`` exits 0 even when the code raises,
so every snippet prints its result after ``MARK``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from .audio_cache import AudioCache, book_key, render_speed, sentence_key, sync

POLL = 60  # seconds between looks at the VM
MAX_FAILURES = 15  # Colab unreachable this many polls in a row: give up
GPUS = ("T4", "L4", "G4", "A100")
DEFAULT_GPU = "T4"
GPU_SETTING = "tts_colab_gpu"
REMOTE = "/content/study-reader"
SESSION_PREFIX = "study-reader-tts-"
PART_SENTENCES = 300  # sentences per audio file
OPUS_QUALITY = 0.9  # libsndfile's Opus compression level: about 30 kbit/s, clear for speech
MARK = "@@study-reader@@"
RENDER_SCRIPT = Path(__file__).parent / "assets" / "kokoro_render.py"
SIGN_IN = "Sign in to Colab: run `colab sessions` in a terminal once, then try again."

RUN_SH = f"""#!/bin/bash
cd "$(dirname "$0")"
(
  set -e
  echo installing > phase
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
  command -v uv >/dev/null || pip -q install uv
  # Kokoro needs Python 3.12 or older, so it gets its own environment.
  [ -x kokoro-env/bin/python ] || uv venv -q --python 3.12 kokoro-env
  uv pip install -q -p kokoro-env/bin/python "kokoro>=0.9.4,<0.10" "misaki[en]" "soundfile>=0.13" \\
    "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
  echo rendering > phase
  kokoro-env/bin/python -W ignore -u kokoro_render.py "$PWD" {PART_SENTENCES} {OPUS_QUALITY}
)
echo $? > exit
"""

# Snippets run on the VM by ``Colab.exec``: each defines _main(), whose result comes back.
_ALIVE = """
def _alive(R):
    import os
    try:
        pid = int(open(R + "/runner.pid").read())
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False
"""

PREPARE = _ALIVE + """
def _main():
    import os
    for key in KEYS:
        os.makedirs(f"{R}/books/{key}", exist_ok=True)
    return {"ok": True}
"""

LAUNCH = _ALIVE + """
def _main():
    import os, subprocess
    if _alive(R):
        return {"started": False}
    with open(R + "/kokoro_render.py", "w") as f:
        f.write(SCRIPT)
    with open(R + "/run.sh", "w") as f:
        f.write(RUN_SH)
    try:
        os.remove(R + "/exit")
    except OSError:
        pass
    log = open(R + "/runner.log", "ab")
    p = subprocess.Popen(["bash", R + "/run.sh"], cwd=R, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with open(R + "/runner.pid", "w") as f:
        f.write(str(p.pid))
    return {"started": True}
"""

STATUS = _ALIVE + """
def _main():
    import glob, json, os
    def read(p):
        try:
            return open(p).read()
        except OSError:
            return None
    books = {}
    for d in glob.glob(R + "/books/*"):
        status = read(d + "/status.json")
        parts = sorted(os.path.basename(p)[:-5] for p in glob.glob(d + "/audio/part-*.json")
                       if os.path.exists(p[:-5] + ".opus"))
        books[os.path.basename(d)] = {"status": json.loads(status) if status else None, "parts": parts}
    code = (read(R + "/exit") or "").strip()
    return {"alive": _alive(R), "phase": (read(R + "/phase") or "").strip(),
            "exit": int(code) if code else None, "log": (read(R + "/runner.log") or "")[-3000:], "books": books}
"""

_WRAP = """
import json as _json, traceback as _tb
try:
    _result = _main()
except Exception:
    _result = {"error": _tb.format_exc()}
print(MARK + _json.dumps(_result), flush=True)
"""


class CliError(Exception):
    """A colab command failed (perhaps only for now: the network, Colab itself)."""


class AuthError(CliError):
    """The CLI isn't signed in (and can't ask, with no terminal)."""


class StartError(CliError):
    """Colab wouldn't give us a VM."""


class RenderFailed(Exception):
    """The render on the VM stopped with an error."""


def colab_exe() -> str | None:
    found = shutil.which("colab")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "colab"
    return str(local) if local.exists() else None


class Colab:
    """The ``colab`` CLI, with its sessions kept in ``config``."""

    def __init__(self, config: Path, exe: str | None = None):
        self.config, self.exe = Path(config), exe or colab_exe()

    def _run(self, *args: str, input: str | None = None, timeout: float = 300) -> str:
        if not self.exe:
            raise StartError("The colab CLI isn't installed: uv tool install google-colab-cli")
        try:
            # In its own process group, so a hung call (the CLI sometimes waits forever on a
            # websocket) is killed whole, then retried on the next look.
            p = subprocess.Popen([self.exe, "--config", str(self.config), *args], text=True,
                                 stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            raise CliError(f"couldn't run colab: {e}") from e
        try:
            stdout, stderr = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            raise CliError(f"colab {args[0]} took over {timeout:.0f} s") from e
        if p.returncode:
            out = stdout + stderr
            # With no terminal, signing in (a URL to visit, a code to paste back) can't happen.
            if re.search(r"EOFError|invalid_grant|RefreshError|Please visit this URL|authorization code", out):
                raise AuthError(SIGN_IN)
            raise CliError(_tail(out) or f"colab {args[0]} failed ({p.returncode})")
        return stdout

    def sessions(self) -> set[str]:
        """Our sessions Colab still has (others' show as [?])."""
        return set(re.findall(r"^\[([^\]?][^\]]*)\]", self._run("sessions", timeout=90), re.M))

    def new(self, name: str, gpu: str) -> None:
        try:
            self._run("new", "-s", name, "--gpu", gpu, timeout=600)
        except AuthError:
            raise
        except CliError as e:
            raise StartError(f"Colab wouldn't start a {gpu}: {e}") from e
        if name not in self.sessions():
            raise StartError(f"Colab wouldn't start a {gpu}.")

    def stop(self, name: str) -> None:
        self._run("stop", "-s", name, timeout=120)

    def exec(self, name: str, snippet: str, **values) -> dict:
        """Run a snippet defining _main() with ``values`` as globals; what it returned."""
        head = "".join(f"{k} = {v!r}\n" for k, v in {"MARK": MARK, "R": REMOTE, **values}.items())
        out = self._run("exec", "-s", name, "--timeout", "60", input=head + snippet + _WRAP, timeout=150)
        for line in reversed(out.splitlines()):
            if line.startswith(MARK):
                result = json.loads(line[len(MARK):])
                if "error" in result:
                    raise CliError(_tail(result["error"]))
                return result
        if re.search(r"appears to be lost|not found", out):
            raise CliError("the Colab VM is gone")
        raise CliError(_tail(out) or "no answer from the Colab VM")

    def upload(self, name: str, local: Path, remote: str) -> None:
        self._run("upload", "-s", name, str(local), remote, timeout=600)

    def download(self, name: str, remote: str, local: Path) -> None:
        self._run("download", "-s", name, remote, str(local), timeout=600)


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(line for line in text.strip().splitlines()[-lines:])


# -- the queue, kept in <data>/colab ------------------------------------------------------

def colab_dir() -> Path:
    from .db import data_dir

    return data_dir() / "colab"


def audio_root() -> Path:
    from .db import data_dir

    return data_dir() / "audio"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def queue(base: Path, file_hash: str, title: str, voice: str, speed: float, gpu: str, texts,
          drive: Path | None = None) -> int:
    """Ask for a book's sentences (in reading order) to be rendered; how many there are.
    ``drive`` is where the old Drive notebook put the book's audio: what it finished is copied
    in before working out what is left to render."""
    seen, sentences = set(), []
    for text in texts:
        key = sentence_key(voice, text)
        if key not in seen:
            seen.add(key)
            sentences.append([key, text])
    key = book_key(file_hash)
    _write_json(base / "requests" / f"{key}.json", {
        "version": 1, "id": secrets.token_hex(4), "key": key, "hash": file_hash, "title": title,
        "voice": voice, "speed": render_speed(speed), "gpu": gpu, "sentences": sentences, "queued": time.time(),
        "drive": str(drive) if drive else None})
    (base / "failed").unlink(missing_ok=True)
    (base / "stop").unlink(missing_ok=True)
    return len(sentences)


def request(base: Path, file_hash: str) -> dict | None:
    """The book's latest request, with "done" saying whether it has all been rendered."""
    key = book_key(file_hash)
    req = _read_json(base / "requests" / f"{key}.json")
    if req:
        req["done"] = _done_id(base, key) == req["id"]
    return req


def _done_id(base: Path, key: str) -> str | None:
    try:
        return (base / "requests" / f"{key}.done").read_text().strip()
    except OSError:
        return None


def pending(base: Path) -> list[dict]:
    """Requests not finished yet, in the order they were made."""
    reqs = []
    for path in (base / "requests").glob("*.json"):
        req = _read_json(path)
        if req and _done_id(base, req["key"]) != req["id"]:
            reqs.append(req)
    return sorted(reqs, key=lambda r: r["queued"])


def request_stop(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "stop").write_text(str(time.time()))


def read_state(base: Path) -> dict:
    return _read_json(base / "state.json") or {}


def failure(base: Path) -> str | None:
    try:
        return (base / "failed").read_text()
    except OSError:
        return None


def watcher_running(base: Path) -> bool:
    import fcntl

    base.mkdir(parents=True, exist_ok=True)
    with open(base / "watch.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def start_watcher(base: Path) -> None:
    """Start the watcher in its own session, so it keeps going when the app closes."""
    base.mkdir(parents=True, exist_ok=True)
    src = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
    with open(base / "watch.log", "ab") as log:
        subprocess.Popen([sys.executable, "-m", "study_reader.colab_render"], cwd=str(base), env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def rendered_keys(folder: Path, speed: float) -> set[str]:
    """Sentences with audio in ``folder`` made at ``speed``."""
    return {k for k, entries in AudioCache(folder).index.items() if any(abs(e[0] - speed) < 0.01 for e in entries)}


def prune(folder: Path, speed: float) -> int:
    """Remove parts this renderer made at another speed whose sentences all have audio at
    ``speed`` now; how many. (Parts copied from Drive are named part-NNNN and left alone.)"""
    have, removed = rendered_keys(folder, speed), 0
    for index in sorted(folder.glob("part-*-*.json")):
        items = (_read_json(index) or {}).get("items")
        if items and all(abs(v[0] - speed) >= 0.01 and k in have for k, v in items.items()):
            index.unlink()
            index.with_suffix(".opus").unlink(missing_ok=True)
            removed += 1
    return removed


# -- the watcher -----------------------------------------------------------------------------

class Watcher:
    """Drives one Colab VM through the queue; ``step`` is one look at it."""

    def __init__(self, base: Path, audio: Path, cli: Colab, log=print):
        self.base, self.audio, self.cli, self.log = Path(base), Path(audio), cli, log
        self.state = read_state(self.base)
        self.state.setdefault("runs", {})  # book key → {"id", "run", "total"}, uploaded to this VM
        self.state.setdefault("books", {})  # book key → progress, for the dialog
        self.failures = 0

    def save(self, phase: str | None = None, message: str | None = None) -> None:
        if phase is not None:
            self.state["phase"] = phase
        if message is not None:
            self.state["message"] = message
            self.log(message)
        self.state["updated"] = time.time()
        _write_json(self.base / "state.json", self.state)

    def run(self, sleep=time.sleep, poll: float = POLL) -> None:
        while True:
            try:
                more = self.step()
                self.failures = 0
            except AuthError as e:
                return self._give_up(str(e), release=False)
            except (StartError, RenderFailed) as e:
                return self._give_up(str(e))
            except CliError as e:
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    return self._give_up(f"Colab didn't answer for {self.failures} tries: {e}")
                self.save(message=f"Can't reach Colab ({e}); trying again.")
                more = True
            if not more:
                return
            sleep(poll)

    def _give_up(self, reason: str, release: bool = True) -> None:
        if release:
            self._release()
        (self.base / "failed").write_text(reason)
        self.save("failed", reason)

    def _release(self, everything: bool = False) -> None:
        """Stop our VM (``everything``: and any other in our sessions, say one whose start
        was cut short before it was written down)."""
        names = {self.state.get("session")} - {None}
        if everything:
            names |= self.cli.sessions()
        for name in sorted(names):
            try:
                self.cli.stop(name)
            except CliError as e:
                self.log(f"couldn't stop {name}: {e}")
        self.state.update(session=None, runs={})

    def step(self) -> bool:
        """One look: start, upload, download, finish; whether there is more to do."""
        if (self.base / "stop").exists():
            self._release()
            for req in pending(self.base):
                (self.base / "requests" / f"{req['key']}.json").unlink(missing_ok=True)
            (self.base / "stop").unlink(missing_ok=True)
            self.save("stopped", "Stopped, and the Colab GPU was released.")
            return False
        reqs = pending(self.base)
        if not reqs:
            had = self.state.get("session")
            self._release(everything=True)
            if had:
                self.save("done", "All rendered, and the Colab GPU was released.")
            return False
        for req in reqs:  # sent again with nothing new to render: no VM needed
            if self.state["runs"].get(req["key"], {}).get("id") != req["id"] and not self._todo(req):
                self._finish(req)
        reqs = pending(self.base)
        if not reqs:
            return self.step()
        name = self.state.get("session")
        if name and name not in self.cli.sessions():
            self.state.update(session=None, runs={})
            self.save(message="Colab ended the VM; starting another to carry on.")
            name = None
        if not name:
            ours = sorted(self.cli.sessions())
            if ours:  # one whose start was cut short: use it
                name = ours[0]
                self.state.update(session=name, runs={})
                self.save(message="Carrying on with the Colab VM already running.")
            else:
                gpu = reqs[0].get("gpu") or DEFAULT_GPU
                name = SESSION_PREFIX + secrets.token_hex(3)
                self.state.update(session=name, gpu=gpu, runs={})  # written down first, so it's stopped even if cut short
                self.save("starting", f"Starting a {gpu} on Colab…")
                self.cli.new(name, gpu)
                self.save()
        self._upload([r for r in reqs if self.state["runs"].get(r["key"], {}).get("id") != r["id"]])
        if not self.state["runs"]:
            return bool(pending(self.base))
        status = self.cli.exec(name, STATUS)
        for key in list(self.state["runs"]):
            if key in status["books"]:
                self._collect(name, key, status["books"][key])
            else:  # the VM lost it (restarted?): send it again
                del self.state["runs"][key]
        if self.state["runs"] and not status["alive"]:
            if status["exit"]:
                raise RenderFailed("Rendering on Colab stopped with an error:\n" + _tail(status["log"], 12))
            self.cli.exec(name, LAUNCH, SCRIPT=RENDER_SCRIPT.read_text(), RUN_SH=RUN_SH)  # queued after it finished
        self.save(*self._describe(status))
        return True

    def _upload(self, reqs: list[dict]) -> None:
        name, sends = self.state["session"], []
        for req in reqs:
            todo = self._todo(req)
            if not todo:
                self._finish(req)
                continue
            sends.append((req, todo))
        if not sends:
            return
        self.cli.exec(name, PREPARE, KEYS=[req["key"] for req, _ in sends])
        with tempfile.TemporaryDirectory() as tmp:
            for req, todo in sends:
                run = secrets.token_hex(3)
                path = Path(tmp) / f"{req['key']}.json"
                path.write_text(json.dumps({"run": run, "title": req["title"], "voice": req["voice"],
                                            "speed": req["speed"], "sentences": todo}, ensure_ascii=False))
                self.save(message=f"Sending {req['title']} to Colab…")
                self.cli.upload(name, path, f"{REMOTE}/books/{req['key']}/request.json")
                self.state["runs"][req["key"]] = {"id": req["id"], "run": run, "total": len(todo)}
                self.state["books"][req["key"]] = {
                    "title": req["title"], "hash": req["hash"], "rendered": 0, "total": len(todo),
                    "copied": self.state["books"].get(req["key"], {}).get("copied", 0)}
                self.save()
        self.cli.exec(name, LAUNCH, SCRIPT=RENDER_SCRIPT.read_text(), RUN_SH=RUN_SH)

    def _todo(self, req: dict) -> list:
        """The request's sentences with no audio here yet at its speed (after copying in what
        the Drive notebook finished)."""
        folder = self.audio / req["key"]
        drive = Path(req["drive"]) if req.get("drive") else None
        if drive and drive.is_dir():
            try:
                sync(drive, folder)
            except OSError as e:
                self.log(f"couldn't copy from {drive}: {e}")
        have = rendered_keys(folder, req["speed"])
        return [s for s in req["sentences"] if s[0] not in have]

    def _collect(self, name: str, key: str, remote: dict) -> None:
        """Download the book's finished parts; finish it when they're all here."""
        run = self.state["runs"][key]["run"]
        book, folder = self.state["books"][key], self.audio / key
        folder.mkdir(parents=True, exist_ok=True)
        status = remote.get("status") or {}
        mine = status.get("run") == run
        parts = [p for p in remote.get("parts", []) if p.startswith(f"part-{run}-")]
        for part in parts:
            if (folder / f"{part}.json").exists():
                continue
            for ext in (".opus", ".json"):  # the audio first: an index is only there with its audio
                tmp = folder / f"{part}{ext}.tmp"
                self.cli.download(name, f"{REMOTE}/books/{key}/audio/{part}{ext}", tmp)
                os.replace(tmp, folder / f"{part}{ext}")
            book["copied"] = book.get("copied", 0) + 1
            self.save()
        if mine:
            book.update(rendered=status.get("rendered", 0), total=status.get("total", book["total"]),
                        speech=status.get("speech", 0), took=status.get("took", 0))
        if mine and status.get("done") and all((folder / f"{p}.json").exists() for p in parts):
            req = _read_json(self.base / "requests" / f"{key}.json")
            if req and req["id"] == self.state["runs"][key]["id"]:
                self._finish(req)
            del self.state["runs"][key]

    def _finish(self, req: dict) -> None:
        folder = self.audio / req["key"]
        if folder.is_dir():
            prune(folder, req["speed"])
        (self.base / "requests" / f"{req['key']}.done").write_text(req["id"])
        self.state["books"].setdefault(req["key"], {"title": req["title"], "hash": req["hash"], "copied": 0})
        self.state["books"][req["key"]].update(done=True)
        self.log(f"{req['title']}: done")

    def _describe(self, status: dict) -> tuple[str, str]:
        gpu = self.state.get("gpu", DEFAULT_GPU)
        if status.get("phase") != "rendering":
            return "installing", f"Setting up Kokoro on a Colab {gpu}…"
        for key in self.state["runs"]:
            book = self.state["books"][key]
            if book.get("rendered", 0) < book.get("total", 0):
                rate = book.get("speech", 0) / book["took"] if book.get("took") else 0
                speed = f" · {rate:.0f}× real time" if rate >= 1 else ""
                return "rendering", (f"Rendering {book['title']} on a Colab {gpu}: "
                                     f"{book['rendered']:,} of {book['total']:,} sentences{speed}.")
        return "rendering", "Copying the last audio from Colab…"


def main() -> None:
    import fcntl

    base = colab_dir()
    base.mkdir(parents=True, exist_ok=True)
    lock = open(base / "watch.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another watcher has it
    log = lambda msg: print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)  # noqa: E731
    watcher = Watcher(base, audio_root(), Colab(base / "sessions.json"), log=log)
    try:
        watcher.run()
    except Exception:  # noqa: BLE001 - written down for the dialog
        watcher._give_up("The Colab watcher crashed:\n" + _tail(traceback.format_exc(), 8))


if __name__ == "__main__":
    main()
