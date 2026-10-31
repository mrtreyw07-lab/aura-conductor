"""Acceptance checks: the approved argv arrays, run in order, bound to the commit and tree they ran on.

A receipt records the contract it ran (its fingerprint binds the dispatch key, the directory and every argv),
each check's exit code and output hash, and HEAD and the tree status before and after. A caller can then refuse
a receipt produced on another commit, or one whose checks changed the tree they were checking.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence

from . import procs

PYTHON = "{python}"


def expand(argv: Sequence[str]) -> list[str]:
    """``{python}`` names the interpreter conductor runs on, so a workflow's checks run the same everywhere."""
    return [sys.executable if part == PYTHON else str(part) for part in argv]


def contract_fingerprint(key: str, cwd: str, checks: Sequence[dict]) -> str:
    bound = {
        "dispatch_key": key,
        "cwd": os.path.normcase(os.path.realpath(cwd)),
        "checks": [{"argv": list(check["argv"]), "timeout_seconds": check["timeout_seconds"]} for check in checks],
    }
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_environment(allowlist: Sequence[str] | None = None) -> dict[str, str]:
    """The base environment plus the names a workflow lists in ``environment.env``; secrets are dropped either way."""
    return procs.scrubbed_env(os.environ, extend=allowlist or ())


def _tree(cwd: str) -> tuple[str | None, bool, str | None]:
    """HEAD, a clean-tree verdict and its diagnostic. Index flags that hide tracked contents fail closed."""
    from . import worktrees

    try:
        head = worktrees.head(cwd)
        clean = worktrees.is_clean(cwd)
        if clean:
            return head, True, None
        hidden = worktrees.hidden_index_paths(cwd)
        if hidden:
            return head, False, f"index flags hide tracked contents (assume-unchanged or skip-worktree): {hidden[0]}"
        return head, False, None
    except worktrees.WorktreeError as exc:
        return None, False, str(exc)


def run_checks(
    key: str,
    cwd: str,
    checks: Sequence[dict],
    *,
    allowlist: Sequence[str] | None = None,
    cancel: threading.Event | None = None,
    on_result: Callable[[dict], None] | None = None,
    require_git: bool = True,
) -> dict:
    """Run the checks in order and stop at the first failure. ``cancel`` stops the running check's process tree.

    In a git tree (``require_git``) the checks run only on a clean tree, and the tree is looked at again after each
    check: a check that moves HEAD or leaves any change, even one a later check would undo, ends the run as a
    failure. A single check that changes files and restores them before it exits cannot be seen from outside; the
    checks are code the operator approved, and the threat model says what that leaves open.

    ``on_result`` sees each check as it finishes; the returned receipt is the authority and does not depend on it.
    """
    env = check_environment(allowlist)
    head_before, clean_before, problem = _tree(cwd) if require_git else (None, True, None)
    results: list[dict] = []
    cancelled = False
    untouched = clean_before
    problem = problem or (None if clean_before else "the tree had uncommitted changes before the checks ran")
    for index, check in enumerate(checks if clean_before else ()):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        argv = expand(check["argv"])
        started = time.monotonic()
        code: int | None = None
        error: str | None = None
        with tempfile.TemporaryFile() as output:
            try:
                process = procs.spawn(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
            except OSError as exc:
                error = f"The check could not start: {type(exc).__name__}"
            else:
                deadline = started + float(check["timeout_seconds"])
                while True:
                    try:
                        code = process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel is not None and cancel.is_set():
                        procs.stop_tree(process)
                        cancelled, error = True, "The check was stopped."
                        break
                    if time.monotonic() >= deadline:
                        procs.stop_tree(process)
                        error = f"The check timed out after {check['timeout_seconds']}s."
                        break
                if error is not None:
                    code = None
            output.seek(0)
            digest, size, tail = hashlib.sha256(), 0, b""
            while block := output.read(65536):
                digest.update(block)
                size += len(block)
                tail = (tail + block)[-4000:]
        result = {
            "argv": list(check["argv"]),
            "exit_code": code,
            "error": error,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output_sha256": digest.hexdigest(),
            "output_bytes": size,
            "output_tail": tail.decode("utf-8", errors="replace"),
        }
        results.append(result)
        if on_result is not None:
            try:
                on_result({"key": key, "index": index, **{k: v for k, v in result.items() if k != "output_tail"}})
            except Exception:
                pass  # a faulty listener must not cost the remaining checks or the receipt
        if require_git and not cancelled:
            head_now, clean_now, tree_problem = _tree(cwd)
            if head_now != head_before or not clean_now:
                untouched = False
                problem = tree_problem or f"check {index + 1} changed the tree it was checking"
                break
        if cancelled or code != 0:
            break
    head_after = _tree(cwd)[0] if require_git else None
    passed = (
        untouched
        and not cancelled
        and len(results) == len(checks)
        and bool(results)
        and all(result["exit_code"] == 0 for result in results)
    )
    return {
        "dispatch_key": key,
        "cwd": os.path.realpath(cwd),
        "contract_sha256": contract_fingerprint(key, cwd, checks),
        "checks": results,
        "head_sha_before": head_before,
        "head_sha_after": head_after,
        "clean_before": clean_before,
        "untouched": untouched and head_before == head_after,
        "problem": problem,
        "cancelled": cancelled,
        "passed": passed,
    }


def bound(receipt: dict, key: str, cwd: str, checks: Sequence[dict], head_sha: str) -> str | None:
    """Why ``receipt`` does not prove these checks passed on ``head_sha`` in ``cwd``; None when it does."""
    results = receipt.get("checks") if isinstance(receipt.get("checks"), list) else []
    if receipt.get("dispatch_key") != key or receipt.get("contract_sha256") != contract_fingerprint(key, cwd, checks):
        return "the receipt is for another contract"
    if os.path.normcase(str(receipt.get("cwd"))) != os.path.normcase(os.path.realpath(cwd)):
        return "the receipt ran in another directory"
    if not (receipt.get("head_sha_before") == head_sha == receipt.get("head_sha_after")):
        return f"the checks did not run on {head_sha[:12]}"
    if not receipt.get("clean_before"):
        return receipt.get("problem") or "the tree had uncommitted changes before the checks ran"
    if not receipt.get("untouched"):
        return receipt.get("problem") or "the checks changed the tree they were checking"
    failing = next((result for result in results if result.get("exit_code") != 0), None)
    if failing is not None:
        reason = failing.get("error") or f"exited {failing.get('exit_code')}"
        return f"{' '.join(map(str, failing.get('argv') or []))[:200]} {reason}"
    if not receipt.get("passed") or len(results) != len(checks):
        return "not every check ran"
    return None
