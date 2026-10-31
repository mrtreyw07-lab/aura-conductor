"""The Codex CLI as an agent: ``codex exec --json``, the brief on stdin, the sandbox chosen by stage.

    [packages.agent]
    adapter = "codex_cli"
    model = "gpt-5-codex"          # optional; the CLI's own default otherwise
    extra_args = []                # appended to every invocation
    env_allow = []                 # e.g. ["OPENAI_API_KEY"] when you log in with a key rather than `codex login`

An implement stage runs with ``sandbox_mode="workspace-write"`` and a review with ``"read-only"``. A stage that was
stopped part-way resumes its own session (``codex exec resume <id>``), so the agent keeps its context; a new attempt
starts a new session with the previous attempt's feedback in the brief.
"""

from __future__ import annotations

import json
import shutil

from .base import AgentResult, AgentTask, SubprocessAgent, prompt, verdict_from


class CodexCLIAgent(SubprocessAgent):
    adapter = "codex_cli"
    executable = "codex"

    @classmethod
    def family_for(cls, spec: dict) -> str:
        return "openai"

    def argv(self, task: AgentTask) -> list[str]:
        command = list(self.spec["command"])
        command[0] = shutil.which(command[0]) or command[0]
        sandbox = "read-only" if task.read_only else "workspace-write"
        options = ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"']
        if self.spec.get("model"):
            options += ["-m", self.spec["model"]]
        options += self.spec.get("extra_args") or []
        if task.continuation and task.session_id:
            return [*command, "exec", "resume", task.session_id, *options, "-"]
        return [*command, "exec", *options, "-"]

    def stdin(self, task: AgentTask) -> str:
        return prompt(task)

    def session_from(self, output: str) -> str | None:
        for event in _events(output):
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
        return None

    def interpret(self, output: str, returncode: int | None, task: AgentTask) -> AgentResult:
        events = list(_events(output))
        session = self.session_from(output)
        failure = next((e for e in events if e.get("type") in {"turn.failed", "error"}), None)
        messages = [
            e["item"].get("text", "")
            for e in events
            if e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message"
        ]
        summary = (messages[-1] if messages else "").strip()
        if failure is not None or returncode != 0:
            error = (failure or {}).get("error") or failure or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            return AgentResult("failed", (detail or summary or f"codex exited {returncode}")[:2000], session_id=session)
        if not any(e.get("type") == "turn.completed" for e in events):
            return AgentResult("failed", "codex ended without completing its turn", session_id=session)
        verdict = verdict_from(summary) if task.read_only else None
        return AgentResult("done", summary[:2000], session_id=session, verdict=verdict)


def _events(output: str):
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event
