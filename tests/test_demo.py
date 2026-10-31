"""The scripted demo, end to end: what the README promises is what this test checks."""

from __future__ import annotations

import ctypes
import io
import os
import tempfile

import pytest

from conductor import demo, store
from conductor.events import read_events

from .conftest import git, symlinks_available

pytestmark = pytest.mark.integration


def another_name_for(directory) -> str | None:
    """The directory by a name ``realpath`` rewrites: its 8.3 short form on Windows, else a symlink to it."""
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(directory), buffer, len(buffer)) and os.path.normcase(
            buffer.value
        ) != os.path.normcase(str(directory)):
            return buffer.value
    if symlinks_available(directory.parent):
        link = directory.parent / "temp-link"
        os.symlink(directory, link, target_is_directory=True)
        return str(link)
    return None


def test_the_demo_runs_when_its_temporary_directory_has_another_name(tmp_path, monkeypatch):
    # macOS's temporary directory sits behind the /var link, and Windows runners set TEMP to an 8.3 short name.
    real = tmp_path / "a-long-temporary-directory"
    real.mkdir()
    alias = another_name_for(real)
    if alias is None:
        pytest.skip("this machine offers neither 8.3 short names nor symlinks")
    monkeypatch.setattr(tempfile, "tempdir", alias)
    out = io.StringIO()
    summary = demo.run_demo(stream=out, stop_on=None)
    assert summary["ok"], out.getvalue()


def test_the_demo_tells_the_whole_story(tmp_path):
    out = io.StringIO()
    summary = demo.run_demo(str(tmp_path / "demo"), stream=out)
    assert summary["ok"], out.getvalue()
    repo = summary["repo"]
    state = store.RunDir(repo, summary["run_id"]).load()
    packages = state["packages"]

    # Stop held the run; Resume finished it.
    assert (summary["first"], summary["final"]) == ("held", "completed")
    # B continued its first attempt after the Stop rather than starting a second.
    implements = [(r["attempt"], r["continuation"]) for r in packages["B"]["runs"] if r["stage"] == "implement"]
    assert packages["B"]["attempts"] == 1 and implements == [(1, False), (1, True)]
    # Of the two parallel packages that both append to the README, the second to land was reworked from the new tip.
    reworked = [pid for pid in ("A", "C") if packages[pid]["attempts"] == 2]
    assert len(reworked) == 1 and packages[reworked[0]]["failure"] == "merge_conflict"

    events = list(read_events(store.RunDir(repo, summary["run_id"]).events_path))
    landed = {e["package_id"]: e for e in events if e["kind"] == "merge.landed"}
    started_b = min(e["seq"] for e in events if e["kind"] == "stage.started" and e["package_id"] == "B")
    assert started_b > landed["A"]["seq"]  # B waited until A landed
    rechecked = {e["package_id"]: e["landing_sha"] for e in events if e["kind"] == "merge.rechecked" and e["passed"]}
    assert {pid: e["sha"] for pid, e in landed.items()} == rechecked  # what landed is what was re-checked

    # main: the seed commit and three landings, each one commit with one parent, moved only by fast-forward.
    history = git(repo, "rev-list", "--parents", "main").splitlines()
    assert len(history) == 4 and all(len(line.split()) <= 2 for line in history)
    moves = git(repo, "reflog", "--format=%gs", "refs/heads/main").splitlines()
    assert len(moves) == 4 and all("Fast-forward" in move or move.startswith("conductor: land") for move in moves[:-1])
    assert git(repo, "status", "--porcelain") == ""
    text = out.getvalue()
    for line in (
        "$ conductor stop",
        "$ conductor resume",
        "conflict on README.md",
        "(continues)",
        "main fast-forwarded to",
    ):
        assert line in text
