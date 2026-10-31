"""The command line, driven in-process."""

from __future__ import annotations

import json
import os

import pytest

from conductor import cli, store

from .conftest import package, workflow, write_workflow

pytestmark = pytest.mark.integration


def policy_file(tmp_path, **extra) -> str:
    body = {
        "kinds": ["run", "land", "grant"],
        "adapters": ["scripted"],
        "commands": ["{python}"],
        "max_tier": "T3",
        **extra,
    }
    lines = ["[policy]"] + [f"{key} = {json.dumps(value)}" for key, value in body.items()]
    path = tmp_path / "policy.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def roots(monkeypatch, repo):
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ROOTS", os.path.dirname(repo))
    return repo


def simple(tmp_path) -> str:
    return write_workflow(tmp_path, workflow(package("A", [{"write": "a.txt", "content": "a"}])))


def test_validate_prints_the_plan_and_its_fingerprint(tmp_path, repo, capsys):
    assert cli.main(["--repo", repo, "validate", simple(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "fixture: 1 packages onto main" in out and "fingerprint " in out


def test_run_status_log_and_resume_of_a_finished_run(tmp_path, roots, capsys):
    workflow_path, policy = simple(tmp_path), policy_file(tmp_path)
    assert cli.main(["--repo", roots, "run", workflow_path, "--policy", policy]) == 0
    out = capsys.readouterr().out
    assert "finished: completed" in out and "main fast-forwarded to" in out
    assert cli.main(["--repo", roots, "status"]) == 0
    assert "accepted" in capsys.readouterr().out
    assert cli.main(["--repo", roots, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert cli.main(["--repo", roots, "log"]) == 0
    assert "merging -> accepted" in capsys.readouterr().out
    assert cli.main(["--repo", roots, "log", "--json"]) == 0
    kinds = [json.loads(line)["kind"] for line in capsys.readouterr().out.splitlines()]
    assert kinds[0] == "approval.requested" and kinds[-1] == "run.finished"
    assert cli.main(["--repo", roots, "stop"]) == 0
    assert "is not running (completed)" in capsys.readouterr().out
    assert cli.main(["--repo", roots, "resume", "--policy", policy]) == 1
    assert "nothing to resume" in capsys.readouterr().err


def test_a_run_outside_every_root_is_refused(tmp_path, repo, capsys):
    assert cli.main(["--repo", repo, "run", simple(tmp_path), "--policy", policy_file(tmp_path)]) == 1
    assert "outside conductor's dispatch roots" in capsys.readouterr().err


def test_without_a_terminal_or_a_policy_nothing_is_approved(tmp_path, roots, capsys):
    assert cli.main(["--repo", roots, "run", simple(tmp_path)]) == 1
    assert "not approved: no interactive terminal" in capsys.readouterr().err
    assert store.runs(roots) == []


def test_grant_records_a_repository_until_it_is_archived(tmp_path, repo, capsys):
    policy = policy_file(tmp_path, roots=[str(tmp_path)])
    assert cli.main(["grant", repo]) == 1  # no terminal, no policy
    assert "Not granted" in capsys.readouterr().err
    assert cli.main(["grant", repo, "--policy", policy]) == 0
    assert cli.main(["grants"]) == 0
    listing = capsys.readouterr().out
    assert f"granted  {repo}" in listing and f"project  {repo}" in listing
    assert cli.main(["--repo", repo, "run", simple(tmp_path), "--policy", policy]) == 0
    assert cli.main(["archive", repo]) == 0
    capsys.readouterr()
    assert cli.main(["grants"]) == 0  # this start restores the record without the archived project
    assert f"granted  {repo}" not in capsys.readouterr().out
    assert cli.main(["revoke", repo]) == 0


def test_a_policy_inside_the_repository_is_refused(tmp_path, roots, capsys):
    inside = os.path.join(roots, "policy.toml")
    with open(policy_file(tmp_path), encoding="utf-8") as source, open(inside, "w", encoding="utf-8") as target:
        target.write(source.read())
    assert cli.main(["--repo", roots, "run", simple(tmp_path), "--policy", inside]) == 1
    assert "where agents work" in capsys.readouterr().err


def test_hold_resume_and_cancel_from_the_command_line(tmp_path, roots, capsys):
    path = write_workflow(tmp_path, workflow(package("A", [{"write": "a.txt", "content": "a"}])))
    with open(os.path.join(roots, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("untracked, in the way")
    policy = policy_file(tmp_path)
    assert cli.main(["--repo", roots, "run", path, "--policy", policy]) == cli.EXIT_HELD
    assert "is held" in capsys.readouterr().out
    assert cli.main(["--repo", roots, "cancel"]) == 0
    assert "cancelled; 1 packages stopped" in capsys.readouterr().out


def test_errors_are_one_line(tmp_path, repo, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("version = 2\n", encoding="utf-8")
    assert cli.main(["--repo", repo, "validate", str(bad)]) == 1
    assert capsys.readouterr().err.startswith("conductor: A version 1 workflow is required")
    assert cli.main(["--repo", repo, "status"]) == 1
    assert "No conductor run" in capsys.readouterr().err


def test_the_printer_renders_every_kind_it_knows():
    lines = []

    class Sink:
        def write(self, text):
            lines.append(text)

        def flush(self):
            pass

    printer = cli.EventPrinter(Sink(), clock=lambda: 0.0)
    for event in [
        {"kind": "package.state_changed", "package_id": "A", "from": "a", "to": "accepted", "cause": "c"},
        {
            "kind": "stage.started",
            "package_id": "A",
            "key": "r:A:implement:2",
            "stage": "implement",
            "attempt": 1,
            "continuation": True,
        },
        {
            "kind": "stage.started",
            "package_id": "A",
            "key": "r:A:verify:3",
            "stage": "recheck",
            "attempt": 1,
            "continuation": False,
        },
        {"kind": "agent.progress", "package_id": "A", "message": "wrote a.txt"},
        {
            "kind": "worktree.provisioned",
            "package_id": "A",
            "path": "p",
            "branch": "b",
            "base_sha": "abcdef123",
            "created": True,
        },
        {"kind": "worktree.refreshed", "package_id": "A", "new_base": "abcdef123"},
        {
            "kind": "check.result",
            "package_id": "A",
            "argv": ["/usr/bin/python3", "-V"],
            "exit_code": 1,
            "duration_ms": 1500,
        },
        {"kind": "review.verdict", "package_id": "A", "verdict": "approve"},
        {"kind": "merge.built", "package_id": "A", "landing_sha": "1234567", "parent_sha": "7654321", "replayed": True},
        {"kind": "merge.conflict", "package_id": "A", "paths": []},
        {"kind": "merge.rechecked", "package_id": "A", "landing_sha": "1234567", "passed": False, "reason": "x"},
        {"kind": "merge.landed", "package_id": "A", "sha": "1234567"},
        {"kind": "merge.held", "package_id": "A", "reason": "staged"},
        {
            "kind": "approval.decided",
            "approval": "land",
            "subject": "A",
            "approved": False,
            "approver": "p",
            "reason": "no",
        },
        {"kind": "run.stopping"},
        {"kind": "run.held", "packages": [], "reason": "r"},
        {"kind": "run.resumed", "packages": ["A"]},
        {"kind": "run.finished", "status": "failed"},
        {"kind": "receipt.recorded", "package_id": "A"},
    ]:
        printer(event)
    text = "".join(lines)
    for fragment in (
        "a -> accepted",
        "implement 2, attempt 1 (continues)",
        "re-check on the landing commit",
        "agent: wrote a.txt",
        "check FAIL python3 -V (1.5s)",
        "(replayed)",
        "nothing to land",
        "re-check failed: x",
        "main fast-forwarded to 1234567",
        "held: staged",
        "land A refused by p",
        "resumed: A",
        "finished: failed",
    ):
        assert fragment in text
    assert len(lines) == 18
