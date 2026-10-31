"""The leak check that CI runs over the files and the history, tested on throwaway repositories."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from conductor import worktrees as wt

from .conftest import git, make_repo

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "leak_check.py"
spec = importlib.util.spec_from_file_location("leak_check", SCRIPT)
leak_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(leak_check)


def commit_as(repo: str, name: str, email: str, message: str = "change") -> None:
    env = {
        **wt.git_environment(),
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", message], env=env, check=True)


@pytest.mark.parametrize(
    "text,label",
    [
        ("see C:\\Users\\someone\\x", "a drive-letter path"),
        ("cd /home/someone/code", "a home directory"),
        ("session local_12345678-abcd-", "a desktop session id"),
        ("as WP-85 found", "a private package number"),
    ],
)
def test_patterns_fire(text, label):
    assert leak_check.findings(text, set()) == [label]
    assert leak_check.findings("https://github.com/someone/aura-conductor and /home/runner/work", set()) == []


def test_identities_pass_only_as_github_noreply_addresses():
    extra = {"examplehandle"}
    assert leak_check.identity_findings("examplehandle", "1+examplehandle@users.noreply.github.com", extra) == []
    assert leak_check.identity_findings("", "examplehandle@users.noreply.github.com", extra) == []
    assert leak_check.identity_findings("examplehandle", "examplehandle@mail.example", extra) == [
        "an email that is not a GitHub noreply address",
        "an extra term in a commit identity",
    ]
    assert leak_check.identity_findings("Real Name examplehandle", "1+other@users.noreply.github.com", extra) == [
        "a name that is not the noreply address's login",
        "an extra term in a commit identity",
    ]
    # Strict without any denylist: an unknown personal address is a finding by itself.
    assert leak_check.identity_findings("Someone", "someone@mail.example", set()) == [
        "an email that is not a GitHub noreply address"
    ]


def test_history_scans_messages_and_identities(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    git(tmp_path, "init", "-q", "-b", "main", repo)
    monkeypatch.setenv("LEAK_TERMS", "examplehandle")
    commit_as(repo, "examplehandle", "1+examplehandle@users.noreply.github.com")
    assert leak_check.main(["--history"], root=repo) == 0
    commit_as(repo, "examplehandle", "examplehandle@mail.example")
    assert leak_check.main(["--history"], root=repo) == 1
    assert leak_check.main([], root=repo) == 0  # the files alone are clean
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    commit_as(repo, "operator", "1+operator@users.noreply.github.com", "fix for examplehandle")
    assert leak_check.main(["--history"], root=repo) == 1  # the message names an extra term
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    commit_as(repo, "Operator Name", "1+operator@users.noreply.github.com", "clean message")
    assert leak_check.main(["--history"], root=repo) == 1  # a real name on a noreply address


def test_files_are_scanned_and_binaries_skipped(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "repo", {"README.md": "clean\n"})
    monkeypatch.delenv("LEAK_TERMS", raising=False)
    (tmp_path / "repo" / "notes.txt").write_text("cd /Users/someone\n", encoding="utf-8")
    (tmp_path / "repo" / "image.bin").write_bytes(b"\0/Users/someone")
    assert leak_check.main([], root=repo) == 1
    assert capsys.readouterr().out.splitlines()[0] == "notes.txt: a home directory"
    assert leak_check.main([], root=str(tmp_path)) == 2  # not a repository
