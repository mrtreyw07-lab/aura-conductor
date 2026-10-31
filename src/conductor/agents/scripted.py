"""Deterministic fake agents: a script of file edits. No model, no network, no API key.

They exist so the engine can be demonstrated and tested exactly: the same script produces the same commits on
every machine. A script is a list of steps, each optionally limited to some attempts::

    [packages.agent]
    adapter = "scripted"
    steps = [
      { write = "src/greeting.py", content = "..." },
      { append = "README.md", text = "- greeting\\n" },
      { sleep = 0.5 },
      { fail = "the tests do not pass yet", attempts = [1] },
    ]

A continued attempt (after Stop and Resume) runs the script again over the partial tree: a write whose content is
already there, or an append whose text already ends the file, is kept rather than repeated. That is what a real
agent does when told to finish the work rather than start over.

A reviewer's script gives a verdict per attempt (``verdicts = ["request_changes", "approve"]``); the last one
repeats.
"""

from __future__ import annotations

import os

from .. import contract as _contract_paths
from .base import Agent, AgentResult, AgentTask

STEP_KINDS = ("write", "append", "delete", "sleep", "fail", "block")
FIELDS = {"adapter", "name", "family", "steps", "verdicts", "review_steps"}


def _validate_steps(raw: object, label: str) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 200:
        raise ValueError(f"{label} must be a list of at most 200 steps")
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            raise ValueError(f"each of {label} is a table")
        kinds = [kind for kind in STEP_KINDS if kind in step]
        if len(kinds) != 1:
            raise ValueError(f"each of {label} has exactly one of {', '.join(STEP_KINDS)}")
        kind = kinds[0]
        allowed = {kind, "attempts", "content", "text"}
        if set(step) - allowed:
            raise ValueError(f"{label}: unknown fields {', '.join(sorted(set(step) - allowed))}")
        clean: dict = {"kind": kind}
        if kind in {"write", "append", "delete"}:
            clean["path"] = _contract_paths.relative_path(step[kind], f"{label} {kind}")
            if kind == "write":
                if not isinstance(step.get("content"), str):
                    raise ValueError(f"{label}: write needs content")
                clean["content"] = step["content"]
            if kind == "append":
                if not isinstance(step.get("text"), str) or not step["text"]:
                    raise ValueError(f"{label}: append needs text")
                clean["text"] = step["text"]
        elif kind == "sleep":
            seconds = step["sleep"]
            if not isinstance(seconds, int | float) or isinstance(seconds, bool) or not 0 <= seconds <= 60:
                raise ValueError(f"{label}: sleep is 0 to 60 seconds")
            clean["seconds"] = float(seconds)
        else:
            if not isinstance(step[kind], str) or not step[kind]:
                raise ValueError(f"{label}: {kind} needs a message")
            clean["message"] = step[kind]
        attempts = step.get("attempts")
        if attempts is not None:
            if not isinstance(attempts, list) or any(type(a) is not int or a < 1 for a in attempts):
                raise ValueError(f"{label}: attempts lists attempt numbers from 1")
            clean["attempts"] = sorted(set(attempts))
        steps.append(clean)
    return steps


class ScriptedAgent(Agent):
    adapter = "scripted"

    @classmethod
    def validate(cls, spec: dict) -> dict:
        unknown = set(spec) - FIELDS
        if unknown:
            raise ValueError(f"scripted does not take {', '.join(sorted(unknown))}")
        verdicts = spec.get("verdicts", ["approve"])
        if (
            not isinstance(verdicts, list)
            or not verdicts
            or any(v not in ("approve", "request_changes") for v in verdicts)
        ):
            raise ValueError("verdicts lists approve or request_changes, one per attempt")
        name = spec.get("name", "scripted")
        family = spec.get("family", "scripted")
        for label, value in (("name", name), ("family", family)):
            if not isinstance(value, str) or not value.strip() or len(value) > 60:
                raise ValueError(f"{label} must be a short string")
        return {
            "adapter": cls.adapter,
            "name": name,
            "family": family,
            "steps": _validate_steps(spec.get("steps", []), "steps"),
            "review_steps": _validate_steps(spec.get("review_steps", []), "review_steps"),
            "verdicts": list(verdicts),
        }

    @classmethod
    def family_for(cls, spec: dict) -> str:
        return spec.get("family", "scripted")

    @classmethod
    def describe(cls, spec: dict) -> str:
        return f"scripted:{spec.get('name', 'scripted')}"

    def run(self, task: AgentTask) -> AgentResult:
        steps = self.spec["review_steps"] if task.read_only else self.spec["steps"]
        for step in steps:
            if task.cancel.is_set():
                return AgentResult("cancelled", "stopped by the operator")
            if "attempts" in step and task.attempt not in step["attempts"]:
                continue
            kind = step["kind"]
            if kind == "sleep":
                if task.cancel.wait(step["seconds"]):
                    return AgentResult("cancelled", "stopped by the operator")
                continue
            if kind == "fail":
                return AgentResult("failed", step["message"])
            if kind == "block":
                return AgentResult("blocked", step["message"])
            target = self._target(task, step["path"])
            if kind == "write":
                if _read(target) == step["content"]:
                    task.progress(f"kept {step['path']}")
                    continue
                _write(target, step["content"], "w")
                task.progress(f"wrote {step['path']}")
            elif kind == "append":
                if (_read(target) or "").endswith(step["text"]):
                    task.progress(f"kept {step['path']}")
                    continue
                _write(target, step["text"], "a")
                task.progress(f"appended to {step['path']}")
            elif kind == "delete" and os.path.lexists(target):
                os.remove(target)
                task.progress(f"deleted {step['path']}")
        if task.read_only:
            verdicts = self.spec["verdicts"]
            verdict = verdicts[min(task.attempt, len(verdicts)) - 1]
            summary = "looks right" if verdict == "approve" else "the change needs another pass"
            return AgentResult("done", summary, verdict=verdict)
        return AgentResult("done", "script finished")

    @staticmethod
    def _target(task: AgentTask, relative: str) -> str:
        root = os.path.realpath(task.worktree)
        target = os.path.realpath(os.path.join(root, *relative.split("/")))
        if os.path.commonpath([root, target]) != root:
            raise ValueError(f"{relative} resolves outside the worktree")
        return target


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _write(path: str, text: str, mode: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as handle:
        handle.write(text)
