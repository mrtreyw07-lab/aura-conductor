"""``conductor``: run a workflow, stop it, resume it, and read what happened.

conductor grant .                     approve this repository as a place conductor may work
conductor validate workflow.toml      check a workflow without running anything
conductor run workflow.toml           approve and run it (Ctrl+C or `conductor stop` holds it)
conductor stop | resume | cancel      hold the run, continue the same attempts, or end it
conductor status | log [--follow]     the packages' states, and the event log
conductor demo                        the scripted demo in a throwaway repository
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence

from . import __version__, contract, permissions, scheduler, store
from . import worktrees as wt
from .events import follow, read_events

EXIT_OK, EXIT_FAILED, EXIT_HELD, EXIT_USAGE = 0, 1, 3, 2


# ── output ───────────────────────────────────────────────────────────────


class Style:
    def __init__(self, stream) -> None:
        self.on = bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, text: str) -> str:
        return self(text, "2")

    def bold(self, text: str) -> str:
        return self(text, "1")


PACKAGE_COLOURS = ("36", "35", "33", "34", "32", "31")
STATE_COLOURS = {
    "accepted": "32",
    "failed": "31",
    "budget_spent": "31",
    "stopped": "31",
    "held": "33",
    "merge_held": "33",
    "merging": "36",
    "queued_for_merge": "36",
    "uncertain": "33",
}


class EventPrinter:
    """One short line per event worth reading, prefixed with seconds since the printer started."""

    def __init__(
        self, stream=None, *, verbose: bool = True, clock: Callable[[], float] = time.monotonic, pace: float = 0.0
    ):
        self.stream = stream or sys.stdout
        self.style = Style(self.stream)
        self.verbose = verbose
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.colours: dict[str, str] = {}
        # A pause after each line, so a recording can be read. It slows the run too: listeners are synchronous.
        self.pace = pace

    def package(self, package_id: str) -> str:
        colour = self.colours.setdefault(package_id, PACKAGE_COLOURS[len(self.colours) % len(PACKAGE_COLOURS)])
        return self.style(f"{package_id:<3}", colour)

    def line(self, package_id: str | None, text: str) -> None:
        stamp = self.style.dim(f"{self.clock() - self.started:6.1f}s")
        who = self.package(package_id) if package_id else "   "
        with self.lock:
            self.stream.write(f"{stamp}  {who}  {text}\n")
            self.stream.flush()
            time.sleep(self.pace)

    def note(self, text: str) -> None:
        with self.lock:
            self.stream.write(f"\n{self.style.bold(text)}\n")
            self.stream.flush()
            time.sleep(self.pace * 10)

    def __call__(self, event: dict) -> None:
        kind, pid = event["kind"], event.get("package_id")
        text = self.render(event)
        if text is not None:
            self.line(pid if kind not in {"approval.requested", "approval.decided"} else None, text)

    def render(self, event: dict) -> str | None:
        kind, s = event["kind"], self.style
        if kind == "package.state_changed":
            target = s(event["to"], STATE_COLOURS.get(event["to"], "0"))
            return f"{event['from']} -> {target}  {s.dim(event['cause'][:90])}"
        if kind == "stage.started" and event["stage"] != "review" and self.verbose:
            more = " (continues)" if event["continuation"] else ""
            if event["stage"] == "recheck":
                return s.dim(f"  re-check on the landing commit, attempt {event['attempt']}")
            return s.dim(f"  {event['stage']} {event['key'].rsplit(':', 1)[-1]}, attempt {event['attempt']}{more}")
        if kind == "agent.progress" and self.verbose:
            return s.dim(f"  agent: {event['message']}")
        if kind == "worktree.provisioned" and self.verbose:
            how = "new" if event["created"] else "kept"
            return s.dim(f"  worktree {event['path']} ({how}) on {event['branch']} from {event['base_sha'][:7]}")
        if kind == "worktree.refreshed":
            return s.dim(f"  merged {event['new_base'][:7]} into the branch (never a rebase)")
        if kind == "check.result" and self.verbose:
            mark = s("ok", "32") if event["exit_code"] == 0 else s("FAIL", "31")
            argv = " ".join(os.path.basename(part) if index == 0 else part for index, part in enumerate(event["argv"]))[
                :60
            ]
            return s.dim(f"  check {mark} {argv} ({event['duration_ms'] / 1000:.1f}s)")
        if kind == "review.verdict":
            return s.dim(f"  review: {event['verdict']}")
        if kind == "merge.built":
            return s.dim(
                f"  landing {event['landing_sha'][:7]} built on {event['parent_sha'][:7]}"
                + (" (replayed)" if event["replayed"] else "")
            )
        if kind == "merge.conflict":
            return s(f"  conflict on {', '.join(event['paths']) or 'nothing to land'}", "31")
        if kind == "merge.rechecked":
            verdict = "re-check passed" if event["passed"] else f"re-check failed: {event['reason']}"
            return s.dim(f"  {verdict} on {event['landing_sha'][:7]}")
        if kind == "merge.landed":
            return s(f"  main fast-forwarded to {event['sha'][:7]}", "32")
        if kind in {"merge.held", "merge.stale"}:
            return s(f"  {kind.split('.')[1]}: {event.get('reason') or event.get('head_sha', '')[:7]}", "33")
        if kind == "approval.decided":
            verdict = "approved" if event["approved"] else "refused"
            return s.dim(f"{event['approval']} {event['subject']} {verdict} by {event['approver']}: {event['reason']}")
        if kind == "run.stopping":
            return s("stopping: running stages are cancelled; implement work is kept", "33")
        if kind == "run.held":
            return s(f"held: {', '.join(event['packages']) or 'nothing'} ({event['reason']})", "33")
        if kind == "run.resumed":
            return s(f"resumed: {', '.join(event['packages']) or 'nothing'}", "36")
        if kind == "run.finished":
            colour = "32" if event["status"] == "completed" else "31"
            return s(f"finished: {event['status']}", colour)
        return None


def status_table(state: dict) -> list[str]:
    rows = [("package", "state", "stage", "attempts", "landed", "note")]
    for package in state["plan"]["packages"]:
        entry = state["packages"][package["id"]]
        rows.append(
            (
                package["id"],
                entry["state"],
                entry["stage"],
                f"{entry['attempts']}/{package['max_attempts']}",
                (entry.get("landed_sha") or "")[:7],
                (entry.get("error") or "")[:70],
            )
        )
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows]


# ── commands ─────────────────────────────────────────────────────────────


def _repo(args) -> str:
    return wt.require_repository(os.path.realpath(args.repo))


def _approver(args, registry: permissions.ProjectRegistry, *also: str) -> permissions.Approver:
    """The approver for this command. A policy file may not live in any repository conductor runs in (every
    registered project, and the one this command touches): an agent working there could edit it."""
    if args.policy:
        forbidden = [*registry.projects(), *also]
        return permissions.PolicyApprover.from_file(args.policy, forbidden_roots=forbidden)
    return permissions.CLIApprover()


def _permissions(extra_projects: Sequence[str] = ()) -> tuple[permissions.GrantStore, permissions.ProjectRegistry]:
    grants, registry = permissions.GrantStore(), permissions.ProjectRegistry()
    grants.restore(lambda: [*registry.projects(), *extra_projects])
    return grants, registry


def _drive(run: store.RunDir, args, approver, grants) -> int:
    printer = EventPrinter(verbose=not args.quiet)
    engine = scheduler.Conductor(run, approver=approver, grants=grants, on_event=printer, hold_wait=args.hold_wait)
    previous = None
    if threading.current_thread() is threading.main_thread():

        def on_interrupt(signum, frame):
            printer.note("Stopping: Ctrl+C again to abandon the stop (the run then resumes as uncertain).")
            engine.request_stop()
            signal.signal(signal.SIGINT, previous or signal.SIG_DFL)

        previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        status = engine.run_until_settled()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    print()
    for line in status_table(run.load()):
        print(line)
    if status == "held":
        print(f"\nRun {run.run_id} is held. `conductor resume` continues the same attempts.")
        return EXIT_HELD
    return EXIT_OK if status == "completed" else EXIT_FAILED


def cmd_validate(args) -> int:
    plan = contract.load(args.workflow)
    for line in contract.summary(plan):
        print(line)
    for warning in contract.overlaps(plan):
        print(f"warning: {warning}")
    print(f"fingerprint {contract.fingerprint(plan)}")
    return EXIT_OK


def cmd_run(args) -> int:
    root = _repo(args)
    grants, registry = _permissions([root])
    approver = _approver(args, registry, root)
    run = scheduler.create_run(root, args.workflow, approver=approver, grants=grants, registry=registry)
    print(f"Run {run.run_id} approved; {len(run.load()['plan']['packages'])} packages.")
    return _drive(run, args, approver, grants)


def cmd_resume(args) -> int:
    root = _repo(args)
    grants, registry = _permissions([root])
    run = store.open_run(root, args.run)
    scheduler.check_resumable(run)
    return _drive(run, args, _approver(args, registry, root), grants)


def cmd_stop(args) -> int:
    run = store.open_run(_repo(args), args.run)
    state = run.load()
    if not run.busy():
        print(f"Run {run.run_id} is not running ({state['status']}).")
        return EXIT_OK
    run.request_stop()
    print(f"Stop requested for run {run.run_id}; it holds once its running stages are stopped.")
    return EXIT_OK


def cmd_cancel(args) -> int:
    run = store.open_run(_repo(args), args.run)
    state = scheduler.cancel_run(run)
    print(
        f"Run {run.run_id} cancelled; {sum(p['state'] == 'stopped' for p in state['packages'].values())} "
        "packages stopped. Branches are kept."
    )
    return EXIT_OK


def cmd_status(args) -> int:
    run = store.open_run(_repo(args), args.run)
    state = run.load()
    if args.json:
        print(
            json.dumps(
                {"run_id": run.run_id, "status": state["status"], "packages": state["packages"]},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    driving = " (running now)" if run.busy() else ""
    print(f"Run {run.run_id}: {state['status']}{driving}, {state['plan']['name']}")
    for line in status_table(state):
        print(line)
    return EXIT_OK


def cmd_log(args) -> int:
    run = store.open_run(_repo(args), args.run)
    printer = EventPrinter(verbose=True)
    events = follow(run.events_path, stop=lambda: not run.busy()) if args.follow else read_events(run.events_path)
    for event in events:
        if args.json:
            print(json.dumps(event, ensure_ascii=False))
        else:
            text = printer.render(event)
            if text is not None:
                stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
                print(f"{stamp}  {(event.get('package_id') or ''):<3}  {text}")
    return EXIT_OK


def cmd_grant(args) -> int:
    path = os.path.realpath(os.path.abspath(args.path))
    grants, registry = _permissions()
    approver = _approver(args, registry, path)
    request = permissions.grant_request(path)
    decision = approver.approve(request)
    if not decision.binds(request):
        print(f"Not granted: {decision.reason}.", file=sys.stderr)
        return EXIT_FAILED
    # The project first: another process restoring grants keeps only a recorded root that holds a project.
    if wt.is_git_workspace(path):
        registry.add(path)
    grants.grant(path, record=True)
    print(f"Granted {path}.")
    return EXIT_OK


def cmd_grants(args) -> int:
    grants, registry = _permissions()
    for root in grants.static_roots:
        print(f"static   {root}")
    for root in grants.recorded():
        print(f"granted  {root}")
    for project in registry.projects():
        print(f"project  {project}")
    return EXIT_OK


def cmd_revoke(args) -> int:
    grants, _ = _permissions()
    found = grants.revoke(os.path.realpath(os.path.abspath(args.path)))
    print("Revoked." if found else "That directory was not granted.")
    return EXIT_OK


def cmd_archive(args) -> int:
    _, registry = _permissions()
    path = os.path.realpath(os.path.abspath(args.path or args.repo))
    print("Archived; its grant lapses at the next start." if registry.archive(path) else "Not a registered project.")
    return EXIT_OK


def cmd_demo(args) -> int:
    from . import demo

    summary = demo.run_demo(args.dir, keep=args.keep or bool(args.dir), quiet=args.quiet, pace=args.pace)
    return EXIT_OK if summary["ok"] else EXIT_FAILED


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="conductor", description="The landing engine for parallel coding agents.")
    top.add_argument("--version", action="version", version=f"conductor {__version__}")
    top.add_argument("--repo", default=".", help="the repository (default: the current directory)")
    commands = top.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        return sub

    def driving(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--policy", help="approve from this allow-list policy instead of asking at the terminal")
        sub.add_argument("--quiet", action="store_true", help="state changes and landings only")
        sub.add_argument(
            "--hold-wait",
            type=float,
            default=0.0,
            metavar="SECONDS",
            help="how long to wait for a held landing before holding the run (default 0)",
        )

    add("validate", cmd_validate, "check a workflow file").add_argument("workflow")
    sub = add("run", cmd_run, "approve and run a workflow")
    sub.add_argument("workflow")
    driving(sub)
    sub = add("resume", cmd_resume, "continue a held or interrupted run, as the same attempts")
    sub.add_argument("run", nargs="?")
    driving(sub)
    add("stop", cmd_stop, "hold a running run (from any terminal)").add_argument("run", nargs="?")
    add("cancel", cmd_cancel, "end a run for good; worktrees are removed, branches kept").add_argument("run", nargs="?")
    sub = add("status", cmd_status, "show a run's packages")
    sub.add_argument("run", nargs="?")
    sub.add_argument("--json", action="store_true")
    sub = add("log", cmd_log, "print a run's event log")
    sub.add_argument("run", nargs="?")
    sub.add_argument("--follow", action="store_true")
    sub.add_argument("--json", action="store_true")
    sub = add("grant", cmd_grant, "approve a repository as a place conductor may work")
    sub.add_argument("path")
    sub.add_argument("--policy")
    add("grants", cmd_grants, "list dispatch roots, grants and projects")
    add("revoke", cmd_revoke, "revoke a grant").add_argument("path")
    add("archive", cmd_archive, "unregister a project; its grant lapses at the next start").add_argument(
        "path", nargs="?"
    )
    sub = add("demo", cmd_demo, "run the scripted demo in a throwaway repository")
    sub.add_argument("--dir", help="build the demo repository here and keep it")
    sub.add_argument("--keep", action="store_true", help="keep the temporary repository")
    sub.add_argument("--quiet", action="store_true")
    sub.add_argument("--pace", type=float, default=0.0, metavar="SECONDS", help="pause after each line (recordings)")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (
        contract.ContractError,
        permissions.PermissionRefused,
        store.RunError,
        scheduler.SchedulerError,
        wt.WorktreeError,
        ValueError,
        OSError,
    ) as exc:
        print(f"conductor: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
