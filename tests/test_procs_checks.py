"""Bounded child processes and acceptance checks: scrubbed, timed, stoppable, and bound to the tree they ran on."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from conductor import checks, procs

from .conftest import FAIL, PASS, git


def test_the_environment_is_an_allowlist_a_workflow_can_extend():
    inherited = {
        "PATH": "p",
        "LC_ALL": "C",
        "XDG_CACHE_HOME": "c",
        "MY_SETTING": "1",
        "DATABASE_URL": "u",
        "SENTRY_DSN": "d",
        "SESSION_JWT": "j",
        "DEPLOY_KEY": "k",
        "GIT_ASKPASS": "a",
        "SSH_ASKPASS": "a",
    }
    assert procs.scrubbed_env(inherited) == {"PATH": "p", "LC_ALL": "C", "XDG_CACHE_HOME": "c"}
    extended = procs.scrubbed_env(
        inherited, extend=["my_setting", "DATABASE_URL", "SENTRY_DSN", "DEPLOY_KEY", "GIT_ASKPASS"]
    )
    assert extended == {"PATH": "p", "LC_ALL": "C", "XDG_CACHE_HOME": "c", "MY_SETTING": "1", "DATABASE_URL": "u"}
    assert procs.scrubbed_env(inherited, {"INJECTED": "i", "SECRET_TOKEN": "t"})["INJECTED"] == "i"
    assert "SECRET_TOKEN" not in procs.scrubbed_env(inherited, {"SECRET_TOKEN": "t"})


def test_stop_tree_stops_a_child_and_what_it_started(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process = procs.spawn([sys.executable, "-c", script], cwd=str(tmp_path), env=procs.scrubbed_env(os.environ))
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    started = time.monotonic()
    procs.stop_tree(process, grace=2.0)
    assert process.poll() is not None and time.monotonic() - started < 10
    if os.name != "nt":
        grandchild = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the grandchild outlived the stop")


def test_checks_pass_in_order_and_stop_at_the_first_failure(repo):
    seen = []
    receipt = checks.run_checks("k", repo, [PASS, FAIL, PASS], on_result=seen.append)
    assert not receipt["passed"] and [r["exit_code"] for r in receipt["checks"]] == [0, 3]
    assert [line["index"] for line in seen] == [0, 1]
    assert receipt["head_sha_before"] == receipt["head_sha_after"] == git(repo, "rev-parse", "HEAD")
    assert receipt["untouched"] and receipt["contract_sha256"] == checks.contract_fingerprint(
        "k", repo, [PASS, FAIL, PASS]
    )
    assert checks.bound(receipt, "k", repo, [PASS, FAIL, PASS], receipt["head_sha_before"]).endswith("exited 3")


def test_a_passing_receipt_is_bound_to_its_commit_and_contract(repo):
    head = git(repo, "rev-parse", "HEAD")
    receipt = checks.run_checks("k", repo, [PASS])
    assert receipt["passed"] and checks.bound(receipt, "k", repo, [PASS], head) is None
    assert checks.bound(receipt, "other", repo, [PASS], head) == "the receipt is for another contract"
    assert checks.bound(receipt, "k", repo, [PASS], "0" * 40).startswith("the checks did not run on")
    assert checks.bound(receipt, "k", repo, [FAIL], head) == "the receipt is for another contract"


def test_a_check_that_changes_the_tree_is_not_a_pass(repo):
    write = {"argv": ["{python}", "-c", "open('new.txt', 'w').write('x')"], "timeout_seconds": 60}
    receipt = checks.run_checks("k", repo, [write, PASS])
    assert not receipt["passed"] and not receipt["untouched"] and len(receipt["checks"]) == 1
    assert (
        checks.bound(receipt, "k", repo, [write, PASS], git(repo, "rev-parse", "HEAD"))
        == "check 1 changed the tree it was checking"
    )


def test_checks_that_change_and_restore_a_file_between_them_fail(repo):
    # Write "good", assert it, restore "bad": before, the tree looked untouched at the end and "bad" would land.
    with open(f"{repo}/a.txt", "w", encoding="utf-8") as handle:
        handle.write("bad")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "bad")
    steps = [
        {"argv": ["{python}", "-c", "open('a.txt', 'w').write('good')"], "timeout_seconds": 30},
        {"argv": ["{python}", "-c", "assert open('a.txt').read() == 'good'"], "timeout_seconds": 30},
        {"argv": ["{python}", "-c", "open('a.txt', 'w').write('bad')"], "timeout_seconds": 30},
    ]
    receipt = checks.run_checks("k", repo, steps)
    assert not receipt["passed"] and len(receipt["checks"]) == 1
    assert checks.bound(receipt, "k", repo, steps, git(repo, "rev-parse", "HEAD")) == (
        "check 1 changed the tree it was checking"
    )


def test_checks_refuse_a_tree_that_is_not_clean(repo):
    with open(f"{repo}/README.md", "a", encoding="utf-8") as handle:
        handle.write("uncommitted\n")
    receipt = checks.run_checks("k", repo, [PASS])
    assert not receipt["passed"] and receipt["checks"] == [] and not receipt["clean_before"]
    assert checks.bound(receipt, "k", repo, [PASS], git(repo, "rev-parse", "HEAD")) == (
        "the tree had uncommitted changes before the checks ran"
    )


def test_checks_never_see_a_secret(repo, monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "fake-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-secret")
    probe = {
        "argv": ["{python}", "-c", "import os, sys; sys.exit(1 if 'fake-secret' in os.environ.values() else 0)"],
        "timeout_seconds": 60,
    }
    assert checks.run_checks("k", repo, [probe])["passed"]
    assert "SERVICE_API_KEY" not in checks.check_environment()
    monkeypatch.setenv("MY_SETTING", "1")
    assert "MY_SETTING" not in checks.check_environment()
    assert checks.check_environment(["MY_SETTING"])["MY_SETTING"] == "1"
    assert "SERVICE_API_KEY" not in checks.check_environment(["SERVICE_API_KEY"])


def test_a_check_times_out_and_its_process_is_stopped(repo):
    slow = {"argv": ["{python}", "-c", "import time; time.sleep(30)"], "timeout_seconds": 1}
    started = time.monotonic()
    receipt = checks.run_checks("k", repo, [slow])
    assert not receipt["passed"] and "timed out" in receipt["checks"][0]["error"]
    assert time.monotonic() - started < 15


def test_a_check_is_cancelled(repo):
    cancel = threading.Event()
    slow = {"argv": ["{python}", "-c", "import time; time.sleep(30)"], "timeout_seconds": 60}
    threading.Timer(0.5, cancel.set).start()
    receipt = checks.run_checks("k", repo, [slow, PASS], cancel=cancel)
    assert receipt["cancelled"] and not receipt["passed"] and len(receipt["checks"]) == 1


def test_a_command_that_cannot_start_fails_cleanly(repo):
    missing = {"argv": ["definitely-not-a-command-conductor"], "timeout_seconds": 5}
    receipt = checks.run_checks("k", repo, [missing])
    assert not receipt["passed"] and receipt["checks"][0]["error"].startswith("The check could not start")


def test_python_placeholder_expands():
    assert checks.expand(["{python}", "-V"]) == [sys.executable, "-V"]
    assert (
        subprocess.run(
            checks.expand(["{python}", "-c", "pass"]), env=procs.scrubbed_env(os.environ), check=False
        ).returncode
        == 0
    )
