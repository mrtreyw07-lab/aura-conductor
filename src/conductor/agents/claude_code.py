"""Claude Code as an agent: ``claude -p --output-format json``, the brief on stdin, the permission mode by stage.

    [packages.agent]
    adapter = "claude_code"
    model = "claude-sonnet-5-5"    # optional; the CLI's own default otherwise
    extra_args = []                # e.g. ["--allowedTools", "Bash(pytest:*)"] to let it run the tests
    env_allow = []                 # e.g. ["ANTHROPIC_API_KEY"] when you use a key rather than `claude login`

An implement stage runs with ``--permission-mode acceptEdits``: file edits are allowed and any other tool needs
``extra_args`` to allow it, since nobody is there to approve it. A review runs with ``--permission-mode plan``, which
cannot edit. A stage that was stopped part-way resumes its own session (``--resume <id>``). If the stop came before
Claude saved that session, there is nothing to resume, so the stage starts under the same id instead.
"""

from __future__ import annotations

import json
import shutil
import uuid

from .base import AgentResult, AgentTask, SubprocessAgent, prompt, verdict_from


class ClaudeCodeAgent(SubprocessAgent):
    adapter = "claude_code"
    executable = "claude"

    @classmethod
    def family_for(cls, spec: dict) -> str:
        return "anthropic"

    def argv(self, task: AgentTask) -> list[str]:
        command = list(self.spec["command"])
        command[0] = shutil.which(command[0]) or command[0]
        mode = "plan" if task.read_only else "acceptEdits"
        options = ["-p", "--output-format", "json", "--permission-mode", mode]
        if self.spec.get("model"):
            options += ["--model", self.spec["model"]]
        if task.continuation and task.session_id:
            options += ["--resume", task.session_id]
        else:
            # A new id is known before spawn, even when Stop arrives before Claude prints its final result.
            try:
                uuid.UUID(task.session_id or "")
            except ValueError:
                task.session_id = str(uuid.uuid4())
            options += ["--session-id", task.session_id]
        return [*command, *options, *(self.spec.get("extra_args") or [])]

    def run(self, task: AgentTask) -> AgentResult:
        result = super().run(task)
        if task.continuation and task.session_id and _never_saved(result, task.session_id):
            # Stopped before Claude saved the session: nothing ran, so start it now under the same id.
            if task.cancel.is_set():
                return AgentResult("cancelled", "stopped by the operator", session_id=task.session_id)
            task.continuation = False
            return super().run(task)
        return result

    def stdin(self, task: AgentTask) -> str:
        return prompt(task)

    def session_from(self, output: str) -> str | None:
        result = _result(output)
        session = (result or {}).get("session_id")
        return session if isinstance(session, str) else None

    def interpret(self, output: str, returncode: int | None, task: AgentTask) -> AgentResult:
        result = _result(output)
        session = self.session_from(output)
        if result is None:
            tail = output.strip()[-500:]
            return AgentResult(
                "failed", f"claude exited {returncode} without a result: {tail}"[:2000], session_id=session
            )
        text = str(result.get("result") or "").strip()
        if result.get("is_error") or result.get("subtype") != "success" or returncode != 0:
            return AgentResult(
                "failed", (text or f"claude ended with {result.get('subtype')}")[:2000], session_id=session
            )
        verdict = verdict_from(text) if task.read_only else None
        return AgentResult("done", text[:2000], session_id=session, verdict=verdict)


def _never_saved(result: AgentResult, session_id: str) -> bool:
    """Claude's refusal to resume an id it has no record of. The failed summary carries its stderr."""
    return result.status == "failed" and f"No conversation found with session ID: {session_id}" in result.summary


def _result(output: str) -> dict | None:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return value
    return None
