"""Fixtures: throwaway git repositories, an isolated conductor home, and small workflow builders."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from conductor import permissions, scheduler
from conductor import worktrees as wt
from conductor.events import read_events

IDENTITY = ("Test Operator", "operator@example.invalid")
ALLOW_ALL = {
    "kinds": ["run", "land", "grant"],
    "adapters": ["scripted"],
    "commands": ["{python}"],
    "max_tier": "T3",
    "max_parallel": 8,
}
PASS = {"argv": ["{python}", "-c", "pass"], "timeout_seconds": 60}
FAIL = {"argv": ["{python}", "-c", "raise SystemExit(3)"], "timeout_seconds": 60}


def git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=wt.git_environment(), check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", IDENTITY[0]),
        ("user.email", IDENTITY[1]),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(path, "config", key, value)
    for name, content in (files or {"README.md": "# fixture\n", ".gitignore": "__pycache__/\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "Initial commit")
    return os.path.realpath(path)


@pytest.fixture(autouse=True)
def conductor_home(tmp_path_factory, monkeypatch) -> str:
    home = str(tmp_path_factory.mktemp("conductor-home"))
    monkeypatch.setenv("CONDUCTOR_HOME", home)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_ROOTS", raising=False)
    return home


@pytest.fixture
def repo(tmp_path) -> str:
    return make_repo(tmp_path / "repo")


def package(
    package_id: str,
    steps: list[dict],
    *,
    files: list[str] | None = None,
    checks: list[dict] | None = None,
    depends_on: tuple[str, ...] = (),
    tier: str = "T0",
    reviewer: dict | None = None,
    max_attempts: int = 2,
    timeout: int = 60,
) -> dict:
    entry = {
        "id": package_id,
        "title": f"Package {package_id}",
        "files": files or [f"{package_id.lower()}.txt"],
        "depends_on": list(depends_on),
        "verification_tier": tier,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout,
        "agent": {"adapter": "scripted", "steps": steps},
        "acceptance": {"checks": checks or [PASS]},
    }
    if reviewer is not None:
        entry["reviewer"] = reviewer
    return copy.deepcopy(entry)


def workflow(*packages: dict, **top) -> dict:
    return {
        "version": 1,
        "name": "fixture",
        "integration_branch": "main",
        "max_parallel": 2,
        "packages": list(packages),
        **top,
    }


def write_workflow(directory, data: dict, name: str = "workflow.json") -> str:
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    return path


class Harness:
    """A repository, its grants and an approver, and helpers to run workflows in it."""

    def __init__(self, repo: str, home: str, policy: dict | None = None):
        self.repo = repo
        self.home = home
        self.grants = permissions.GrantStore(home, static_roots=[os.path.dirname(repo)])
        self.registry = permissions.ProjectRegistry(home)
        self.approver = permissions.PolicyApprover(policy or ALLOW_ALL)
        self.events: list[dict] = []

    def create(self, data: dict, name: str = "workflow.json"):
        path = write_workflow(os.path.dirname(self.repo), data, name)
        return scheduler.create_run(self.repo, path, approver=self.approver, grants=self.grants, registry=self.registry)

    def drive(self, run, *, on_event=None, hold_wait: float = 0.0) -> str:
        def listener(event: dict) -> None:
            self.events.append(event)
            if on_event is not None:
                on_event(event)

        engine = scheduler.Conductor(
            run, approver=self.approver, grants=self.grants, on_event=listener, poll=0.02, hold_wait=hold_wait
        )
        return engine.run_until_settled()

    def run(self, data: dict, **kwargs):
        run = self.create(data)
        return run, self.drive(run, **kwargs)


@pytest.fixture
def harness(repo, conductor_home) -> Harness:
    return Harness(repo, conductor_home)


def events_of(run, kind: str | None = None) -> list[dict]:
    return [event for event in read_events(run.events_path) if kind is None or event["kind"] == kind]


def symlinks_available(tmp_path) -> bool:
    target, link = tmp_path / "symlink-target", tmp_path / "symlink-probe"
    target.mkdir(exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    os.rmdir(link) if os.name == "nt" else os.unlink(link)
    return True
