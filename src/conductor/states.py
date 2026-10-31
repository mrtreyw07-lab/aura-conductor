"""The package state machine. Pure: no I/O and no clock.

One table decides what may happen next. Every change of a package's state is checked here and becomes a
``package.state_changed`` event that carries its cause. The fourth column says what the move does to the
attempt counter, which is how a stopped attempt continues as the same attempt.
"""

from __future__ import annotations

from collections.abc import Iterable

# State → kind.
STATES = {
    "waiting": "waiting",
    "ready": "waiting",
    "running": "active",
    "verifying": "active",
    "awaiting_approval": "owner",
    "queued_for_merge": "settling",
    "merging": "settling",
    "merge_held": "waiting",
    "failed": "settling",
    "held": "paused",
    "uncertain": "paused",
    "budget_spent": "terminal",
    "stopped": "terminal",
    "accepted": "terminal",
}
ACTIVE = frozenset({"running", "verifying", "merging", "awaiting_approval"})
TERMINAL = frozenset(state for state, kind in STATES.items() if kind == "terminal")
NON_TERMINAL = frozenset(STATES) - TERMINAL
# What a Stop holds. A waiting package simply keeps waiting; a failed one is settled in the same step.
HOLDABLE = frozenset(
    {"ready", "running", "verifying", "awaiting_approval", "queued_for_merge", "merging", "merge_held"}
)
STAGES = ("implement", "verify", "approve", "land")
FAILURES = (
    "agent_failed",
    "agent_blocked",
    "timeout",
    "scope_failure",
    "empty",
    "check_failure",
    "review_changes",
    "review_scope",
    "merge_conflict",
    "recheck_failure",
    "branch_moved",
    "error",
)


def _rows(sources: Iterable[str] | str, target: str, trigger: str, attempt: str = "unchanged"):
    if isinstance(sources, str):
        sources = (sources,)
    return [(source, target, trigger, attempt) for source in sorted(sources) if source != target]


TABLE = [
    *_rows("waiting", "ready", "every dependency has landed"),
    *_rows("ready", "running", "a slot opened; the implement stage starts", "spends if new"),
    *_rows("ready", "verifying", "a continued attempt resumes at its checks"),
    *_rows("ready", "awaiting_approval", "a continued attempt resumes at its approval"),
    *_rows("ready", "queued_for_merge", "a continued attempt rejoins the merge queue"),
    *_rows("running", "verifying", "the implement stage committed inside its declared files"),
    *_rows(
        "running",
        "failed",
        "the agent failed, timed out, changed nothing, or left files outside its scope",
        "new attempt",
    ),
    *_rows("verifying", "queued_for_merge", "the checks passed on the stage head, and the review approved by tier"),
    *_rows("verifying", "awaiting_approval", "tier T3: the approver decides before the landing"),
    *_rows(
        "verifying",
        "failed",
        "a check failed, the review asked for changes, or the base refresh conflicted",
        "new attempt",
    ),
    *_rows("awaiting_approval", "queued_for_merge", "the approver approved this exact head"),
    *_rows("awaiting_approval", "stopped", "the approver refused the landing"),
    *_rows("queued_for_merge", "merging", "the queue's turn: one landing at a time"),
    *_rows("merging", "accepted", "the re-check passed on the landing commit and main fast-forwarded to it"),
    *_rows("merging", "merge_held", "the main checkout refused the fast-forward"),
    *_rows("merging", "failed", "a conflict or a failed re-check, returned as rework", "new attempt"),
    *_rows("merge_held", "merging", "the main checkout changed, or the run resumed"),
    *_rows("failed", "ready", "the retry rule: a new attempt within max_attempts", "new attempt"),
    *_rows("failed", "budget_spent", "the next attempt would pass max_attempts"),
    *_rows(
        HOLDABLE,
        "held",
        "the operator's Stop (a running stage is stopped; implement work is kept), or a land "
        "step that failed max_landing_errors times in a row",
        "continuation",
    ),
    *_rows("held", "ready", "Resume continues the same attempt", "continuation"),
    *_rows(ACTIVE, "uncertain", "the run ended without a clean stop; found when it resumed"),
    *_rows("uncertain", "ready", "Resume keeps implement work and continues the attempt", "continuation"),
    *_rows(NON_TERMINAL, "stopped", "the operator cancelled the run"),
]

TRANSITIONS: dict[tuple[str, str], tuple[str, str]] = {}
for _source, _target, _trigger, _attempt in TABLE:
    TRANSITIONS.setdefault((_source, _target), (_trigger, _attempt))


class TransitionRefused(ValueError):
    """A move the table does not allow. The caller's state is unchanged."""


def allowed(source: str, target: str) -> bool:
    return (source, target) in TRANSITIONS


def check(source: str, target: str) -> tuple[str, str]:
    """The (trigger, attempt effect) of a move, or TransitionRefused."""
    if source not in STATES or target not in STATES:
        raise TransitionRefused(f"Unknown package state: {source if source not in STATES else target}.")
    try:
        return TRANSITIONS[(source, target)]
    except KeyError:
        raise TransitionRefused(f"A package cannot move from {source} to {target}.") from None
