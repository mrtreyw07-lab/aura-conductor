"""The scripted demo: three packages in a throwaway repository, no API keys.

* A and C start at once, each in its own worktree. B depends on A and waits until A has landed.
* C appends to the same README lines as A. A lands first, so C no longer squashes onto main: the merge queue
  returns it as rework, and its second attempt starts again from the new tip.
* While B is implementing, the operator stops the run. Everything holds where it is.
* Resume continues B's attempt (the same attempt, not a new one) and the run finishes: three landings, main
  moved only by fast-forward.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import permissions, scheduler, store
from . import worktrees as wt
from .cli import EventPrinter, status_table

IDENTITY = ("Demo Operator", "operator@example.invalid")
POLICY = {
    "kinds": ["run", "land"],
    "adapters": ["scripted"],
    "commands": ["{python}"],
    "max_tier": "T3",
    "max_parallel": 4,
}


def example_dir() -> Path:
    """The example's files: packaged with the wheel, or the source checkout's ``examples/`` folder."""
    packaged = resources.files("conductor") / "_example"
    if packaged.is_dir():
        return Path(str(packaged))
    source = Path(__file__).resolve().parents[2] / "examples" / "two-dependent-packages"
    if source.is_dir():
        return source
    raise FileNotFoundError("The demo's example files are missing from this installation.")


def _git(root: str, *args: str) -> str:
    return wt._checked(root, list(args), f"git {args[0]} failed:", timeout=60)


def build_repository(target: str) -> str:
    """A fresh repository with the example's seed files and workflow committed on main."""
    os.makedirs(target, exist_ok=True)
    repo = os.path.join(target, "greeter")
    if os.path.exists(repo):
        raise FileExistsError(f"{repo} already exists.")
    source = example_dir()
    shutil.copytree(source / "seed", repo)
    shutil.copy(source / "workflow.toml", os.path.join(repo, "workflow.toml"))
    wt._checked(target, ["init", "-q", "-b", "main", repo], "git init failed:")
    for key, value in (
        ("user.name", IDENTITY[0]),
        ("user.email", IDENTITY[1]),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial commit")
    return os.path.realpath(repo)


def run_demo(
    directory: str | None = None,
    *,
    keep: bool = False,
    quiet: bool = False,
    stream=None,
    stop_on: str = "B",
    pace: float = 0.0,
) -> dict:
    """Run the whole story and return what happened. ``stop_on`` is the package whose first agent step
    triggers the operator's Stop (None runs straight through)."""
    out = stream or sys.stdout
    # Resolved even when made here: a temporary directory reached through a link (macOS's /var) or an 8.3 short name
    # (a Windows runner's RUNNER~1) is not canonical, and the grant below would refuse it.
    target = os.path.realpath(directory or tempfile.mkdtemp(prefix="conductor-demo-"))
    printer = EventPrinter(out, verbose=not quiet, pace=pace)
    try:
        repo = build_repository(target)
        home = os.path.join(target, ".conductor-home")
        grants = permissions.GrantStore(home, static_roots=[target])
        registry = permissions.ProjectRegistry(home)
        approver = permissions.PolicyApprover(POLICY, source="demo-policy.toml")
        printer.note("$ conductor run workflow.toml   (3 packages: A and C in parallel, B after A)")
        run = scheduler.create_run(
            repo, os.path.join(repo, "workflow.toml"), approver=approver, grants=grants, registry=registry
        )
        stopped = {"done": stop_on is None}

        def operator(event: dict) -> None:
            printer(event)
            if not stopped["done"] and event["kind"] == "agent.progress" and event.get("package_id") == stop_on:
                stopped["done"] = True
                printer.note(f"$ conductor stop   ({stop_on} is half-way through its implement stage)")
                run.request_stop()

        first = scheduler.Conductor(run, approver=approver, grants=grants, on_event=operator).run_until_settled()
        if first == "held":
            printer.note("$ conductor status")
            for line in status_table(run.load()):
                out.write(f"  {line}\n")
            printer.note("$ conductor resume   (the same attempts continue)")
            scheduler.check_resumable(run)
            final = scheduler.Conductor(run, approver=approver, grants=grants, on_event=printer).run_until_settled()
        else:
            final = first
        state = run.load()
        log = _git(repo, "log", "--format=%h %s", "--first-parent", "main")
        summary = _summary(run, state, first, final, log)
        printer.note("$ git log --oneline main")
        for line in log.splitlines():
            out.write(f"  {line}\n")
        printer.note("Result")
        for line in summary["lines"]:
            out.write(f"  {line}\n")
        out.flush()
        summary["repo"] = repo
        return summary
    finally:
        if not keep and not directory:
            remove_tree(target)


def remove_tree(path: str) -> None:
    """Delete a throwaway tree, including git's read-only object files on Windows."""

    def writable(function, name, _error):
        try:
            os.chmod(name, stat.S_IWRITE)
            function(name)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=writable)


def _summary(run: store.RunDir, state: dict, first: str, final: str, log: str) -> dict:
    packages = state["packages"]
    landings = [line for line in log.splitlines() if line.split(" ", 1)[1][:2] in {"A:", "B:", "C:"}]
    continued = [r for r in packages["B"]["runs"] if r["stage"] == "implement" and r["continuation"]]
    # Which of the parallel pair lands second depends on timing; the story is the same either way.
    reworked = [pid for pid in ("C", "A") if packages[pid]["attempts"] > 1]
    if reworked:
        loser = reworked[0]
        winner = "A" if loser == "C" else "C"
        parallel = (
            f"A and C ran in parallel. {winner} landed first, so {loser} hit a "
            f"{packages[loser].get('failure') or 'failure'}, was reworked, and landed on attempt "
            f"{packages[loser]['attempts']}."
        )
    else:
        parallel = "A and C ran in parallel and both landed on their first attempt."
    lines = [
        parallel,
        f"B waited for A, was stopped mid-implement, and landed on attempt {packages['B']['attempts']}"
        + (" (its resumed stage continued the same attempt)." if continued else "."),
        f"main: {len(landings)} landing commits, fast-forward only. Run {run.run_id}: {first} -> {final}.",
    ]
    ok = final == "completed" and all(p["state"] == "accepted" for p in packages.values()) and len(landings) == 3
    return {
        "ok": ok,
        "first": first,
        "final": final,
        "lines": lines,
        "run_id": run.run_id,
        "attempts": {pid: p["attempts"] for pid, p in packages.items()},
        "failures": {pid: p.get("failure") for pid, p in packages.items()},
        "landings": landings,
    }
