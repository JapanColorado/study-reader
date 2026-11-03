import ast
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from study_reader import colab_render as cr
from study_reader.audio_cache import AudioCache, book_key, sentence_key
from study_reader.db import Database
from test_audio_cache import _part

HASH = "eedd3e1d75924374" + "0" * 48
KEY = book_key(HASH)
TEXTS = ["One fish.", "Two fish.", "Red fish.", "Blue fish."]


class FakeColab:
    """The colab CLI over a pretend VM: a folder standing for /content/study-reader, where the
    status and prepare snippets really run; the test plays the render."""

    def __init__(self, vm: Path):
        self.vm, self.alive, self.calls, self.launches = vm, set(), [], 0
        vm.mkdir(parents=True, exist_ok=True)

    def _path(self, remote: str) -> Path:
        assert remote.startswith(cr.REMOTE + "/")
        return self.vm / remote[len(cr.REMOTE) + 1:]

    def sessions(self):
        self.calls.append(("sessions",))
        return set(self.alive)

    def new(self, name, gpu):
        assert name.startswith(cr.SESSION_PREFIX)
        self.calls.append(("new", gpu))
        self.alive.add(name)

    def stop(self, name):
        self.calls.append(("stop", name))
        self.alive.discard(name)

    def exec(self, name, snippet, **values):
        assert name in self.alive
        if snippet is cr.LAUNCH:
            assert "kokoro_render.py" in values["RUN_SH"] and "def render_book" in values["SCRIPT"]
            self.launches += 1
            (self.vm / "runner.pid").write_text(str(os.getpid()))  # a live process
            (self.vm / "exit").unlink(missing_ok=True)
            return {"started": True}
        scope = {"R": str(self.vm), **values}
        exec(snippet, scope)  # noqa: S102 - the real snippet, on the pretend VM
        return scope["_main"]()

    def upload(self, name, local, remote):
        self._path(remote).write_bytes(Path(local).read_bytes())

    def download(self, name, remote, local):
        shutil.copyfile(self._path(remote), local)

    # -- the VM's side --
    def request(self):
        return json.loads((self.vm / "books" / KEY / "request.json").read_text())

    def render(self, texts, done=False, number=0):
        req = self.request()
        (self.vm / "phase").write_text("rendering\n")
        _part(self.vm / "books" / KEY / "audio", f"part-{req['run']}-{number:04d}", {t: 0.3 for t in texts},
              speed=req["speed"])
        status = {"run": req["run"], "rendered": len(texts), "total": len(req["sentences"]), "done": done,
                  "speech": 60.0, "took": 2.0}
        (self.vm / "books" / KEY / "status.json").write_text(json.dumps(status))

    def finish(self, code):
        (self.vm / "exit").write_text(f"{code}\n")
        (self.vm / "runner.log").write_text("Traceback (most recent call last):\nRuntimeError: CUDA out of memory\n")


@pytest.fixture
def colab(tmp_path):
    base, audio = tmp_path / "colab", tmp_path / "audio"
    fake = FakeColab(tmp_path / "vm")
    return base, audio, fake, cr.Watcher(base, audio, fake, log=lambda m: None)


def test_a_queued_book_is_rendered_downloaded_and_the_vm_released(colab):
    base, audio, fake, w = colab
    _part(audio / KEY, "part-0000", {"One fish.": 0.3}, speed=1.3)  # already here, from Drive
    assert cr.queue(base, HASH, "Fish", "af_heart", 1.5, "L4", TEXTS + ["One fish."]) == 4
    assert cr.request(base, HASH)["speed"] == 1.3 and not cr.request(base, HASH)["done"]
    assert w.step()
    assert ("new", "L4") in fake.calls and fake.launches == 1
    assert [t for _, t in fake.request()["sentences"]] == TEXTS[1:]  # only what isn't here yet
    assert cr.read_state(base)["phase"] == "installing"
    fake.render(TEXTS[1:3])
    assert w.step()
    state = cr.read_state(base)
    assert state["books"][KEY]["copied"] == 1 and state["books"][KEY]["hash"] == HASH
    assert "2 of 3 sentences · 30× real time" in state["message"]
    assert AudioCache(audio / KEY).count(sentence_key("af_heart", t) for t in TEXTS) == 3
    assert not list((audio / KEY).glob("*.tmp"))
    fake.render(TEXTS[3:], done=True, number=1)
    assert w.step()
    assert cr.request(base, HASH)["done"] and cr.pending(base) == []
    assert AudioCache(audio / KEY).count(sentence_key("af_heart", t) for t in TEXTS) == 4
    assert not w.step()
    assert fake.alive == set() and cr.read_state(base)["phase"] == "done" and cr.read_state(base)["session"] is None


def test_a_book_already_rendered_needs_no_vm(colab):
    base, audio, fake, w = colab
    _part(audio / KEY, "part-0000", {t: 0.3 for t in TEXTS}, speed=1.0)
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    assert not w.step()
    assert not [c for c in fake.calls if c[0] == "new"] and cr.request(base, HASH)["done"]


def test_audio_the_drive_notebook_finished_is_copied_in_first(colab, tmp_path):
    base, audio, fake, w = colab
    drive = tmp_path / "drive"
    _part(drive, "part-0000", {t: 0.3 for t in TEXTS[:3]}, speed=1.0)
    _part(drive, "part-0001", {TEXTS[3]: 0.3}, speed=1.0)  # finished after the book was opened
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS, drive)
    assert not w.step()
    assert not [c for c in fake.calls if c[0] == "new"] and cr.request(base, HASH)["done"]
    assert (audio / KEY / "part-0001.json").exists()


def test_a_vm_whose_start_was_cut_short_is_used_or_stopped(colab):
    base, audio, fake, w = colab
    fake.alive.add(cr.SESSION_PREFIX + "abc123")  # started, but the watcher was killed meanwhile
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()
    assert cr.read_state(base)["session"] == cr.SESSION_PREFIX + "abc123" and fake.launches == 1
    assert not [c for c in fake.calls if c[0] == "new"]
    fake.alive.add(cr.SESSION_PREFIX + "def456")
    cr.request_stop(base)
    w.step()
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", [])
    assert not w.step()
    assert fake.alive == set()


def test_a_lost_vm_is_replaced_and_only_whats_missing_sent_again(colab):
    base, audio, fake, w = colab
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()
    fake.render(TEXTS[:2])
    w.step()
    fake.alive.clear()  # Colab reclaimed it
    shutil.rmtree(fake.vm / "books")
    assert w.step()
    assert [c[0] for c in fake.calls].count("new") == 2 and fake.launches == 2
    assert [t for _, t in fake.request()["sentences"]] == TEXTS[2:]


def test_a_render_that_crashes_releases_the_vm_and_says_why(colab):
    base, audio, fake, w = colab
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()
    fake.finish(1)
    (fake.vm / "runner.pid").write_text("999999999")
    w.run(sleep=lambda s: None)
    assert fake.alive == set()
    assert "CUDA out of memory" in cr.failure(base) and cr.read_state(base)["phase"] == "failed"
    assert cr.pending(base)  # still queued: rendering again carries on
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    assert cr.failure(base) is None


def test_a_runner_that_finished_before_a_book_was_queued_is_started_again(colab):
    base, audio, fake, w = colab
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()
    fake.finish(0)
    (fake.vm / "runner.pid").write_text("999999999")
    assert w.step() and fake.launches == 2


def test_stop_releases_the_vm_and_drops_the_queue(colab):
    base, audio, fake, w = colab
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()
    cr.request_stop(base)
    assert not w.step()
    assert fake.alive == set() and cr.pending(base) == [] and cr.read_state(base)["phase"] == "stopped"
    assert not (base / "stop").exists()


def test_colab_unreachable_is_retried_then_given_up(colab, monkeypatch):
    base, audio, fake, w = colab
    cr.queue(base, HASH, "Fish", "af_heart", 1.0, "T4", TEXTS)
    w.step()

    def down():
        raise cr.CliError("connection refused")

    monkeypatch.setattr(fake, "sessions", down)
    sleeps = []
    w.run(sleep=sleeps.append)
    assert len(sleeps) == cr.MAX_FAILURES - 1
    assert "didn't answer" in cr.failure(base)


def test_prune_drops_parts_made_at_an_old_speed_once_redone(tmp_path):
    _part(tmp_path, "part-aaaaaa-0000", {"One fish.": 0.3, "Two fish.": 0.3}, speed=1.0)
    _part(tmp_path, "part-0007", {"One fish.": 0.3}, speed=1.0)  # from Drive: left alone
    _part(tmp_path, "part-bbbbbb-0000", {"One fish.": 0.3}, speed=1.3)
    assert cr.prune(tmp_path, 1.3) == 0  # "Two fish." isn't redone yet
    _part(tmp_path, "part-bbbbbb-0001", {"Two fish.": 0.3}, speed=1.3)
    assert cr.prune(tmp_path, 1.3) == 1
    assert sorted(p.stem for p in tmp_path.glob("*.json")) == ["part-0007", "part-bbbbbb-0000", "part-bbbbbb-0001"]


def test_vm_snippets_and_script_parse():
    for snippet in (cr.PREPARE, cr.LAUNCH, cr.STATUS, cr._WRAP):
        ast.parse(snippet)
    ast.parse(cr.RENDER_SCRIPT.read_text())
    assert f"kokoro_render.py \"$PWD\" {cr.PART_SENTENCES} {cr.OPUS_QUALITY}" in cr.RUN_SH


def _fake_cli(tmp_path, script: str) -> cr.Colab:
    exe = tmp_path / "colab"
    exe.write_text("#!/bin/bash\n" + script)
    exe.chmod(0o755)
    return cr.Colab(tmp_path / "sessions.json", exe=str(exe))


def test_cli_wrapper_reads_results_sessions_and_sign_in_trouble(tmp_path):
    cli = _fake_cli(tmp_path, 'cat > /dev/null; echo "[colab] chatter"; echo \'@@study-reader@@{"ok": 1}\'\n')
    assert cli.exec("s", "def _main(): pass") == {"ok": 1}
    cli = _fake_cli(tmp_path, 'cat > /dev/null; echo \'@@study-reader@@{"error": "Traceback\\nKeyError: 1"}\'\n')
    with pytest.raises(cr.CliError, match="KeyError"):
        cli.exec("s", "def _main(): pass")
    cli = _fake_cli(tmp_path, 'echo "[study-reader-tts-abc123] gpu-t4 | T4"; echo "[?] gpu-t4-other | T4"\n')
    assert cli.sessions() == {"study-reader-tts-abc123"}
    cli = _fake_cli(tmp_path, 'echo "Please visit this URL to authorize"; echo "EOFError" >&2; exit 1\n')
    with pytest.raises(cr.AuthError):
        cli.sessions()
    cli = _fake_cli(tmp_path, 'echo "[colab] 400 no quota" >&2; exit 1\n')
    with pytest.raises(cr.StartError, match="wouldn't start a A100.*no quota"):
        cli.new("study-reader-tts-x", "A100")
    args = tmp_path / "args"
    cli = _fake_cli(tmp_path, f'echo "$@" > {args}\n')
    cli.stop("study-reader-tts-x")
    assert args.read_text().split() == ["--config", str(tmp_path / "sessions.json"), "stop", "-s", "study-reader-tts-x"]


def test_only_one_watcher_runs(tmp_path):
    import fcntl

    assert not cr.watcher_running(tmp_path)
    with open(tmp_path / "watch.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cr.watcher_running(tmp_path)
    assert not cr.watcher_running(tmp_path)


def _wait(app, cond, timeout=10, msg=lambda: ""):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    assert cond(), msg()


def test_render_dialog_queues_the_book_and_reports_progress(pdf_path, tmp_path, monkeypatch, _no_colab):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cr, "colab_exe", lambda: "/usr/bin/colab")
    from PySide6.QtWidgets import QApplication
    from study_reader.document import file_hash
    from study_reader.render_dialog import ColabRender, RenderDialog, local_audio

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    book = db.upsert_book(file_hash(pdf_path), pdf_path, "Zorblatts", 3)
    colab = ColabRender()
    colab.resume()
    assert _no_colab == []  # nothing queued: no watcher
    dlg = RenderDialog(db, book, "af_heart", 1.3, colab)
    _wait(app, lambda: "Not rendered yet" in dlg.status.text(), msg=dlg.status.text)
    assert not dlg.stop_btn.isEnabled()
    dlg.gpu.setCurrentText("L4")
    assert db.get_setting(cr.GPU_SETTING) == "L4"
    dlg.send()
    _wait(app, lambda: "Getting ready to render 6 sentences" in dlg.status.text(), msg=dlg.status.text)
    req = cr.request(colab.base, book["hash"])
    assert req["gpu"] == "L4" and req["speed"] == 1.3 and _no_colab == [colab.base]
    texts = [t for _, t in req["sentences"]]
    _part(local_audio(book["hash"]), "part-abcdef-0000", {t: 0.3 for t in texts[:2]}, speed=1.3)
    synced = []
    colab.synced.connect(lambda h, n: synced.append((h, n)))
    state = {"session": "study-reader-tts-1", "phase": "rendering", "runs": {req["key"]: {"id": req["id"], "total": 4}}, "message": "Rendering Zorblatts on a Colab L4: 2 of 6 sentences.",
             "books": {req["key"]: {"hash": book["hash"], "copied": 1}}}
    (colab.base / "state.json").write_text(json.dumps(state))
    colab._check()
    assert synced == [(book["hash"], 1)]
    _part(local_audio(book["hash"]), "part-abcdef-0001", {texts[2]: 0.3}, speed=1.0)  # another speed: still to do
    _wait(app, lambda: "Here: 2 of 6" in dlg.status.text(), msg=dlg.status.text)
    assert "Colab: Rendering Zorblatts" in dlg.status.text() and dlg.stop_btn.isEnabled()
    assert "Rendering the 4 of its 6 sentences" in dlg.status.text()
    dlg.voice = "am_michael"
    dlg.refresh()
    _wait(app, lambda: "render again" in dlg.status.text())
    dlg.stop()
    assert (colab.base / "stop").exists()
    (colab.base / "failed").write_text("Colab wouldn't start a L4.")
    dlg.refresh()
    _wait(app, lambda: "Rendering stopped: Colab wouldn't start" in dlg.status.text(), msg=dlg.status.text)
    colab2 = ColabRender()
    colab2.resume()
    assert len(_no_colab) == 2  # the stop request started one; a failure doesn't resume


def test_a_hung_cli_call_is_killed(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "sleep 30 &\nwait\n")  # a child holding the pipes open too
    start = time.time()
    with pytest.raises(cr.CliError, match="took over"):
        cli._run("sessions", timeout=0.5)
    assert time.time() - start < 5
