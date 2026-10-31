"""What an agent adapter is: it receives one stage of one package and changes files in that package's worktree.

An adapter never commits, never pushes and never lands anything; conductor does that after the stage, inside the
package's declared files. A review stage is read-only, and conductor checks that it left the tree as it found it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from .. import procs

STATUSES = ("done", "blocked", "failed", "cancelled", "timeout")
VERDICTS = ("approve", "request_changes")


@dataclass
class AgentTask:
    run_id: str
    package_id: str
    key: str
    stage: str  # "implement" or "review"
    title: str
    instruction: str
    worktree: str
    files: list[str]
    attempt: int
    continuation: bool
    timeout_seconds: int
    cancel: threading.Event
    feedback: str | None = None
    session_id: str | None = None
    progress: Callable[[str], None] = field(default=lambda message: None)

    @property
    def read_only(self) -> bool:
        return self.stage == "review"


@dataclass
class AgentResult:
    status: str
    summary: str = ""
    session_id: str | None = None
    verdict: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"Unknown agent status {self.status!r}.")
        if self.verdict is not None and self.verdict not in VERDICTS:
            raise ValueError(f"Unknown review verdict {self.verdict!r}.")


class Agent:
    adapter: ClassVar[str] = ""

    def __init__(self, spec: dict):
        self.spec = spec

    @classmethod
    def validate(cls, spec: dict) -> dict:
        """The normalized spec, or ValueError naming the problem."""
        raise NotImplementedError

    @classmethod
    def family_for(cls, spec: dict) -> str:
        """The model family, for the rule that a T2 or T3 reviewer comes from another family."""
        raise NotImplementedError

    @classmethod
    def describe(cls, spec: dict) -> str:
        return spec["adapter"]

    def run(self, task: AgentTask) -> AgentResult:
        raise NotImplementedError


def prompt(task: AgentTask) -> str:
    """The brief a command-line agent receives: the instruction, its scope, and the rules conductor enforces."""
    lines = [f"Package {task.package_id}: {task.title}", "", task.instruction.strip(), ""]
    if task.read_only:
        lines += [
            "You are reviewing this package's change. Do not modify any file.",
            "Inspect the change with `git diff` against the integration branch.",
            f"Files in scope: {', '.join(task.files)}.",
            "End your reply with exactly one line: VERDICT: approve, or VERDICT: request_changes followed by "
            "what must change.",
        ]
    else:
        lines += [
            f"Change only these paths (a path ending in / is a directory): {', '.join(task.files)}.",
            "Do not commit, push, switch branches or rebase; conductor commits and lands your work.",
            "Changes outside those paths fail the stage.",
        ]
    if task.continuation:
        lines += [
            "",
            "This continues an attempt that was stopped part-way. Files you already wrote are still "
            "in the worktree; finish the work rather than starting over.",
        ]
    if task.feedback:
        lines += ["", "The previous attempt was returned with this feedback:", task.feedback.strip()]
    return "\n".join(lines)


class SubprocessAgent(Agent):
    """An agent that is a command-line program, run in the worktree as a child of conductor.

    The child gets the allow-listed environment (plus the names its spec lists in ``env_allow``), its brief on stdin
    and then end of input, its own process group, and the package's timeout. Stop and timeout stop the whole process
    tree. A subclass supplies ``argv``, ``stdin``, ``interpret`` and ``session_from``.
    """

    executable: ClassVar[str] = ""
    COMMON_FIELDS: ClassVar[set[str]] = {"adapter", "command", "model", "extra_args", "env_allow"}

    @classmethod
    def validate(cls, spec: dict) -> dict:
        if not isinstance(spec, dict):
            raise ValueError("an agent is a table with an adapter")
        unknown = set(spec) - cls.COMMON_FIELDS
        if unknown:
            raise ValueError(f"{cls.adapter} does not take {', '.join(sorted(unknown))}")
        command = spec.get("command", [cls.executable])
        if isinstance(command, str):
            command = [command]
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 8
            or any(not isinstance(part, str) or not part or "\x00" in part for part in command)
        ):
            raise ValueError("command must be a program name or a list of up to 8 strings")
        model = spec.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 100):
            raise ValueError("model must be a model name")
        extra = spec.get("extra_args", [])
        if not isinstance(extra, list) or len(extra) > 40 or any(not isinstance(part, str) for part in extra):
            raise ValueError("extra_args must be a list of strings")
        allow = spec.get("env_allow", [])
        if not isinstance(allow, list) or any(not isinstance(name, str) or not name for name in allow):
            raise ValueError("env_allow must list environment variable names")
        return {
            "adapter": cls.adapter,
            "command": list(command),
            "model": model,
            "extra_args": list(extra),
            "env_allow": list(allow),
        }

    @classmethod
    def describe(cls, spec: dict) -> str:
        return f"{spec['adapter']}{' · ' + spec['model'] if spec.get('model') else ''}"

    def argv(self, task: AgentTask) -> list[str]:
        raise NotImplementedError

    def interpret(self, output: str, returncode: int | None, task: AgentTask) -> AgentResult:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        return procs.scrubbed_env(os.environ, allow=self.spec.get("env_allow") or ())

    def stdin(self, task: AgentTask) -> str | None:
        """Text written to the child's stdin and then closed (the brief), or None for an empty stdin."""
        return None

    def run(self, task: AgentTask) -> AgentResult:
        argv = self.argv(task)
        started = time.monotonic()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                process = procs.spawn(
                    argv,
                    cwd=task.worktree,
                    env=self.environment(),
                    stdout=output,
                    stderr=errors,
                    input_text=self.stdin(task),
                )
            except OSError as exc:
                return AgentResult("failed", f"{os.path.basename(argv[0])} could not start: {type(exc).__name__}")
            task.progress(
                f"started {os.path.basename(argv[0])}"
                + (" (resuming its session)" if task.continuation and task.session_id else "")
            )
            outcome = None
            while True:
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if task.cancel.is_set():
                    outcome = "cancelled"
                elif time.monotonic() - started > task.timeout_seconds:
                    outcome = "timeout"
                if outcome:
                    procs.stop_tree(process)
                    break
            output.seek(0)
            text = output.read().decode("utf-8", errors="replace")
            errors.seek(0)
            stderr = errors.read()[-2000:].decode("utf-8", errors="replace").strip()
        if outcome == "cancelled":
            return AgentResult(
                "cancelled", "stopped by the operator", session_id=self.session_from(text) or task.session_id
            )
        if outcome == "timeout":
            return AgentResult(
                "timeout",
                f"no result within {task.timeout_seconds}s",
                session_id=self.session_from(text) or task.session_id,
            )
        result = self.interpret(text, process.returncode, task)
        result.session_id = result.session_id or task.session_id
        if result.status == "failed" and stderr and stderr not in result.summary:
            result.summary = f"{result.summary} | stderr: {stderr[-500:]}"[:2000]
        return result

    def session_from(self, output: str) -> str | None:
        return None


def verdict_from(text: str) -> str:
    """The reviewer's verdict from its last ``VERDICT:`` line. No clear approval is a request for changes."""
    for line in reversed(text.strip().splitlines()):
        words = line.strip().lstrip("*_` ").split()
        if len(words) >= 2 and words[0].rstrip("*_`").upper() == "VERDICT:":
            return "approve" if words[1].strip("*_`.,").lower() == "approve" else "request_changes"
    return "request_changes"
