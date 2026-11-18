"""The phone app's parity fixtures (android/core/src/test/resources) are made by the scripts in
android/scripts from this desktop's own code; the Kotlin tests check the port against them. Each is
made again here and compared with the committed one, so a desktop change that the fixtures (and so
the Kotlin port) haven't caught up with, or a script that no longer runs, fails here.

When one fails: run that script as its docstring says (it rewrites the fixture), then the phone's
tests (`pixi run test` in android/), and port the change to the Kotlin if they fail."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android"
RESOURCES = ANDROID / "core" / "src" / "test" / "resources"


def run(script: str, out: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONWARNINGS": "ignore"}
    env.pop("STUDY_READER_DATA", None)
    done = subprocess.run([sys.executable, str(ANDROID / "scripts" / script), str(out)], cwd=ANDROID, env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"{script} failed:\n{done.stderr[-3000:]}"


def differences(made: list, committed: list, limit: int = 5) -> list[str]:
    out = [f"{json.dumps(c)[:300]}\n  now: {json.dumps(m)[:300]}" for m, c in zip(made, committed) if m != c]
    if len(made) != len(committed):
        out.append(f"{len(committed)} cases committed, {len(made)} made now")
    return out[:limit]


def compare_json(made: Path, committed: Path) -> None:
    now, before = json.loads(made.read_text()), json.loads(committed.read_text())
    if now == before:
        return
    lines = []
    for key in sorted(set(now) | set(before)):
        if now.get(key) != before.get(key):
            a, b = now.get(key), before.get(key)
            detail = differences(a, b) if isinstance(a, list) and isinstance(b, list) else []
            lines.append(f"{key} differs" + "".join(f"\n  {d}" for d in detail))
    pytest.fail(f"{committed.name} is out of date with the desktop:\n" + "\n".join(lines))


@pytest.mark.parametrize("script, fixture", [
    ("grading_fixtures.py", "grading_cases.json"),
    ("fsrs_fixtures.py", "fsrs_cases.json"),
])
def test_json_fixture_is_current(tmp_path, script, fixture):
    run(script, tmp_path / fixture)
    compare_json(tmp_path / fixture, RESOURCES / fixture)


def snapshot_contents(path: Path) -> dict:
    """Every table's SQL and rows, leaving out what changes from run to run (the snapshot's id, and
    py-fsrs's card_id, which it takes from the clock)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {}
    try:
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = []
            for r in conn.execute(f'SELECT * FROM "{name}"'):
                row = dict(r)
                if name == "questions" and row.get("fsrs"):
                    row["fsrs"] = {k: v for k, v in json.loads(row["fsrs"]).items() if k != "card_id"}
                if name == "meta" and row.get("key") == "id":
                    row["value"] = None
                rows.append(json.dumps(row, sort_keys=True))
            out[name] = (sql, sorted(rows))
    finally:
        conn.close()
    return out


def test_queue_fixtures_are_current(tmp_path):
    run("queue_fixtures.py", tmp_path)
    compare_json(tmp_path / "queue_cases.json", RESOURCES / "queue_cases.json")
    now, before = snapshot_contents(tmp_path / "queue_snapshot.db"), snapshot_contents(RESOURCES / "queue_snapshot.db")
    stale = [t for t in sorted(set(now) | set(before)) if now.get(t) != before.get(t)]
    assert not stale, f"queue_snapshot.db is out of date with the desktop's export (tables {', '.join(stale)})"
