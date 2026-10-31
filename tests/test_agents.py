"""The command-line adapters, against stand-in executables that speak the real CLIs' output formats."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from conductor import agents, contract
from conductor.agents import AgentTask, prompt, verdict_from
from conductor.agents.base import AgentResult, SubprocessAgent

from .conftest import ALLOW_ALL, Harness, events_of, git, workflow

FAKES = Path(__file__).resolve().parent / "fakes"
CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]


def task(
    worktree,
    instruction: str,
    *,
    stage: str = "implement",
    continuation: bool = False,
    session_id: str | None = None,
    timeout: int = 60,
    cancel: threading.Event | None = None,
) -> AgentTask:
    return AgentTask(
        run_id="r",
        package_id="A",
        key="r:A:implement:1",
        stage=stage,
        title="Package A",
        instruction=instruction,
        worktree=str(worktree),
        files=["a.txt"],
        attempt=1,
        continuation=continuation,
        timeout_seconds=timeout,
        cancel=cancel or threading.Event(),
        session_id=session_id,
    )


def agent(adapter: str, **spec):
    command = CODEX if adapter == "codex_cli" else CLAUDE
    return agents.create(
        agents.validate_spec({"adapter": adapter, "command": command, "env_allow": ["FAKE_LOG"], **spec})
    )


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(path))

    def calls() -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    return calls


ADAPTERS = ["codex_cli", "claude_code"]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_an_implement_stage_edits_the_worktree_from_a_brief_on_stdin(adapter, tmp_path, log):
    result = agent(adapter, model="some-model").run(task(tmp_path, "write a.txt hello"))
    assert result.status == "done" and result.session_id
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
    call = log()[0]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tmp_path)
    assert "Change only these paths (a path ending in / is a directory): a.txt." in call["brief"]
    assert "some-model" in call["argv"]
    if adapter == "codex_cli":
        assert call["argv"][:1] == ["exec"] and 'sandbox_mode="workspace-write"' in call["argv"]
        assert call["argv"][-1] == "-"
    else:
        assert call["argv"][call["argv"].index("--permission-mode") + 1] == "acceptEdits"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_continued_stage_resumes_its_own_session(adapter, tmp_path, log):
    result = agent(adapter).run(task(tmp_path, "write a.txt again", continuation=True, session_id="session-42"))
    assert result.status == "done" and result.session_id == "session-42"
    call = log()[0]
    if adapter == "codex_cli":
        assert call["argv"][:3] == ["exec", "resume", "session-42"]
    else:
        assert call["argv"][call["argv"].index("--resume") + 1] == "session-42"
    assert "This continues an attempt that was stopped part-way" in call["brief"]
    # A new attempt never resumes an old session, even when one is known.
    agent(adapter).run(task(tmp_path, "write a.txt fresh", session_id="session-42"))
    assert "session-42" not in log()[1]["argv"]


def test_claude_starts_a_session_it_never_saved_under_the_same_id(tmp_path, log, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("FAKE_SESSIONS", str(sessions))
    session = "0b0c1d2e-3f40-4152-8637-48495a6b7c8d"
    claude = agent("claude_code", env_allow=["FAKE_LOG", "FAKE_SESSIONS"])
    result = claude.run(task(tmp_path, "write a.txt hello", continuation=True, session_id=session))
    assert result.status == "done" and result.session_id == session
    first, second = log()
    assert first["argv"][first["argv"].index("--resume") + 1] == session
    assert second["argv"][second["argv"].index("--session-id") + 1] == session
    assert "This continues an attempt" not in second["brief"]
    # Once saved, the session resumes, and any other failure is reported rather than retried.
    failed = claude.run(task(tmp_path, "fail still broken", continuation=True, session_id=session))
    assert failed.status == "failed" and len(log()) == 3 and "--resume" in log()[2]["argv"]


def test_claude_does_not_start_an_unsaved_session_after_a_stop(tmp_path, monkeypatch):
    session = "0b0c1d2e-3f40-4152-8637-48495a6b7c8d"
    cancel = threading.Event()
    calls = []

    def refused(self, task):
        calls.append(task.continuation)
        cancel.set()  # Stop arrives just as Claude reports it has no such session
        summary = f"claude exited 1 without a result:  | stderr: No conversation found with session ID: {session}"
        return AgentResult("failed", summary)

    monkeypatch.setattr(SubprocessAgent, "run", refused)
    result = agent("claude_code").run(task(tmp_path, "x", continuation=True, session_id=session, cancel=cancel))
    assert result.status == "cancelled" and result.session_id == session and calls == [True]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_a_review_is_read_only_and_returns_its_verdict(adapter, verdict, tmp_path, log):
    result = agent(adapter).run(task(tmp_path, f"verdict {verdict}", stage="review"))
    assert result.status == "done" and result.verdict == verdict
    call = log()[0]
    if adapter == "codex_cli":
        assert 'sandbox_mode="read-only"' in call["argv"]
    else:
        assert call["argv"][call["argv"].index("--permission-mode") + 1] == "plan"
    assert "VERDICT: approve" in call["brief"] and "Do not modify any file." in call["brief"]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_failed_run_is_a_failed_stage(adapter, tmp_path, log):
    result = agent(adapter).run(task(tmp_path, "fail the tests do not pass"))
    assert result.status == "failed" and "the tests do not pass" in result.summary


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_credentials_are_absent_unless_allowed_by_name(adapter, tmp_path, log, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    key = "OPENAI_API_KEY" if adapter == "codex_cli" else "ANTHROPIC_API_KEY"
    agent(adapter).run(task(tmp_path, f"env {key}"))
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "absent"
    agent(adapter, env_allow=["FAKE_LOG", key]).run(task(tmp_path, f"env {key}"))
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "fake-key"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_stop_and_timeout_end_the_process(adapter, tmp_path, log):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    result = agent(adapter).run(task(tmp_path, "sleep 30", cancel=cancel))
    assert result.status == "cancelled" and time.monotonic() - started < 15
    result = agent(adapter).run(task(tmp_path, "sleep 30", timeout=1))
    assert result.status == "timeout"


def test_output_that_is_not_a_result_fails_closed(tmp_path):
    codex, claude = agent("codex_cli"), agent("claude_code")
    work = task(tmp_path, "x")
    assert codex.interpret('{"type": "thread.started", "thread_id": "t"}\n', 0, work).status == "failed"
    assert codex.interpret("not json\n", 1, work).summary == "codex exited 1"
    assert claude.interpret("not json", 0, work).status == "failed"
    error = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False, "result": ""})
    assert "error_max_turns" in claude.interpret(error, 0, work).summary
    missing = agents.create(agents.validate_spec({"adapter": "codex_cli", "command": ["no-such-codex-binary"]}))
    assert missing.run(task(tmp_path, "x")).summary.startswith("no-such-codex-binary could not start")


def test_verdicts_fail_closed():
    assert verdict_from("Looks good.\nVERDICT: approve") == "approve"
    assert verdict_from("**VERDICT:** approve.") == "approve"
    assert verdict_from("VERDICT: approve\nVERDICT: request_changes the test is missing") == "request_changes"
    assert verdict_from("I think it is fine") == "request_changes"
    assert verdict_from("VERDICT: approved-ish") == "request_changes"


def test_specs_are_validated_and_families_differ():
    with pytest.raises(ValueError, match="does not take"):
        agents.validate_spec({"adapter": "codex_cli", "sandbox": "off"})
    with pytest.raises(ValueError, match="command"):
        agents.validate_spec({"adapter": "claude_code", "command": []})
    with pytest.raises(ValueError, match="env_allow"):
        agents.validate_spec({"adapter": "claude_code", "env_allow": "ANTHROPIC_API_KEY"})
    codex = agents.validate_spec({"adapter": "codex_cli"})
    claude = agents.validate_spec({"adapter": "claude_code", "model": "m"})
    assert codex["command"] == ["codex"] and agents.family_of(codex) == "openai"
    assert agents.family_of(claude) == "anthropic" and agents.describe(claude) == "claude_code · m"


def test_the_brief_carries_scope_feedback_and_continuation():
    work = task(".", "Do the thing.", continuation=True)
    work.feedback = "checks failed: tests/test_a.py exited 1"
    text = prompt(work)
    assert text.startswith("Package A: Package A\n\nDo the thing.")
    assert "Do not commit, push, switch branches or rebase" in text
    assert text.endswith(
        "The previous attempt was returned with this feedback:\nchecks failed: tests/test_a.py exited 1"
    )


@pytest.mark.integration
def test_a_tier_two_run_with_codex_implementing_and_claude_reviewing(repo, conductor_home, log):
    harness = Harness(repo, conductor_home, {**ALLOW_ALL, "adapters": ["codex_cli", "claude_code"], "max_tier": "T2"})
    data = workflow(
        {
            "id": "A",
            "title": "Write a",
            "instruction": "write a.txt from codex\nverdict approve",
            "files": ["a.txt"],
            "verification_tier": "T2",
            "agent": {"adapter": "codex_cli", "command": CODEX, "env_allow": ["FAKE_LOG"]},
            "reviewer": {"adapter": "claude_code", "command": CLAUDE, "env_allow": ["FAKE_LOG"]},
            "acceptance": {
                "checks": [
                    {"argv": ["{python}", "-c", "assert open('a.txt').read() == 'from codex'"], "timeout_seconds": 60}
                ]
            },
        }
    )
    assert contract.validate(data)["packages"][0]["reviewer"]["adapter"] == "claude_code"
    run, status = harness.run(data)
    assert status == "completed"
    assert git(repo, "show", "main:a.txt") == "from codex"
    assert [e["verdict"] for e in events_of(run, "review.verdict")] == ["approve"]
    calls = log()
    assert calls[0]["argv"][0] == "exec" and "--permission-mode" in calls[1]["argv"]
    assert run.load()["packages"]["A"]["session_id"] == "11111111-2222-3333-4444-555555555555"
