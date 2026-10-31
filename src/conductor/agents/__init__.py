"""Agent adapters, by name. A workflow names one per package (``agent``) and, from tier T1, a ``reviewer``.

* ``scripted``: deterministic fake agents for demos and tests (``scripted.py``).
* ``codex_cli`` and ``claude_code``: thin adapters over those command-line agents (optional; nothing imports
  them until a workflow names them).
"""

from __future__ import annotations

import importlib

from .base import Agent, AgentResult, AgentTask, SubprocessAgent, prompt, verdict_from

ADAPTERS = {
    "scripted": "conductor.agents.scripted:ScriptedAgent",
    "codex_cli": "conductor.agents.codex_cli:CodexCLIAgent",
    "claude_code": "conductor.agents.claude_code:ClaudeCodeAgent",
}

__all__ = [
    "ADAPTERS",
    "Agent",
    "AgentResult",
    "AgentTask",
    "SubprocessAgent",
    "create",
    "describe",
    "family_of",
    "prompt",
    "validate_spec",
    "verdict_from",
]


def adapter_class(name: object) -> type[Agent]:
    if not isinstance(name, str) or name not in ADAPTERS:
        raise ValueError(f"adapter must be one of {', '.join(sorted(ADAPTERS))}")
    module, _, attribute = ADAPTERS[name].partition(":")
    return getattr(importlib.import_module(module), attribute)


def validate_spec(spec: object) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("an agent is a table with an adapter")
    return adapter_class(spec.get("adapter")).validate(spec)


def family_of(spec: dict) -> str:
    return adapter_class(spec["adapter"]).family_for(spec)


def describe(spec: dict) -> str:
    return adapter_class(spec["adapter"]).describe(spec)


def create(spec: dict) -> Agent:
    return adapter_class(spec["adapter"])(spec)
