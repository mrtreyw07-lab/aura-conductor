"""The scheduler: packages through implement, verify, approval and the merge queue, in dependency order.

One thread owns the run state. Stages run on worker threads and hand back a result; only the owner thread changes
a package's state, always through the state table (``states.check``), and every change is saved before the next
step. A package starts only when every package it depends on has landed, so it is provisioned from a tip that
already holds them.

Stop cancels the running stages (their process trees are stopped), waits for them, and holds every package where
it was. Implement work is kept; cancelled checks restore their clean starting head with evidence. Resume picks
each one up at the stage it stopped in, as the same attempt. Reviewer changes still fail the attempt.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from . import agents, checks, contract, permissions, states, store
from . import merge_queue as mq
from . import worktrees as wt
from .events import EventLog
from .receipts import ReceiptStore, keep_discarded

LANDING_KINDS = ("build", "check", "land")
# How often a landing held by the main checkout looks at the checkout again.
FINGERPRINT_POLL = 1.0
UNWATCHED_RETRY = 5.0
LANDING_BACKOFF_CAP = 30.0


class SchedulerError(RuntimeError):
    pass


@dataclass
class Work:
    kind: str
    future: Future
    cancel: threading.Event = field(default_factory=threading.Event)
    abandonable: bool = False


def _relative(root: str, path: str) -> str:
    try:
        return os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:
        return path


def _package_state() -> dict:
    return {
        "state": "waiting",
        "stage": "implement",
        "attempts": 0,
        "continuation": False,
        "new_attempt": False,
        "fresh": False,
        "feedback": None,
        "failure": None,
        "error": None,
        "runs": [],
        "worktree": None,
        "verified_head": None,
        "stage_head": None,
        "landing": None,
        "joined_at": None,
        "landed_sha": None,
        "held_from": None,
        "session_id": None,
        "approval": None,
    }


# ── creating and resuming a run ──────────────────────────────────────────


def create_run(
    repo: str,
    workflow_path: str,
    *,
    approver: permissions.Approver,
    grants: permissions.GrantStore,
    registry: permissions.ProjectRegistry,
    clock: Callable[[], float] = time.time,
    run_id: str | None = None,
) -> store.RunDir:
    """Validate the workflow, have the approver approve its fingerprint, and write the run's first state.

    It holds the repository's lock throughout, so it fails at once while another process is working there.
    """
    root = wt.require_repository(repo)
    grants.require(root)
    wt.exclude_conductor(root)
    with store.repository_lock(root, "creating a run"):
        return _create(root, workflow_path, approver=approver, registry=registry, clock=clock, run_id=run_id)


def claimed_packages(root: str, *, besides: str | None = None) -> dict[str, str]:
    """Package ids (case-folded) that unfinished runs in this repository still hold, mapped to the run.

    A package's worktree and branch are named after its id alone, so while one run has the package unfinished, no
    other run may provision, reuse or remove them.
    """
    claimed: dict[str, str] = {}
    for run_id in store.runs(root):
        if run_id == besides:
            continue
        try:
            state = store.RunDir(root, run_id).load()
        except store.RunError:
            continue
        if state.get("status") in {"completed", "cancelled"}:
            continue
        for package_id, entry in state.get("packages", {}).items():
            if entry.get("state") not in states.TERMINAL:
                claimed.setdefault(package_id.casefold(), run_id)
    return claimed


def _create(
    root: str,
    workflow_path: str,
    *,
    approver: permissions.Approver,
    registry: permissions.ProjectRegistry,
    clock: Callable[[], float],
    run_id: str | None,
) -> store.RunDir:
    plan = contract.load(workflow_path)
    integration = plan["integration_branch"]
    if wt._git(root, ["rev-parse", "--verify", f"refs/heads/{integration}^{{commit}}"]).returncode != 0:
        raise SchedulerError(f"The integration branch {integration} has no commit in {root}.")
    claimed = claimed_packages(root)
    for package in plan["packages"]:
        owner = claimed.get(package["id"].casefold())
        if owner:
            raise SchedulerError(
                f"Package {package['id']} belongs to run {owner}, which has not finished; resume it, or cancel it "
                "with `conductor cancel`, before starting another run with that package."
            )
    fingerprint = contract.fingerprint(plan)
    request = permissions.run_request(plan, fingerprint, contract.summary(plan) + contract.overlaps(plan))
    decision = approver.approve(request)
    if not decision.binds(request):
        raise permissions.PermissionRefused(f"The run was not approved: {decision.reason}.")
    registry.add(root)
    run = store.RunDir(root, run_id or store.new_run_id(clock))
    if run.exists():
        raise SchedulerError(f"Run {run.run_id} already exists.")
    workflow = os.path.realpath(workflow_path)
    now = clock()
    state = {
        "version": store.STATE_VERSION,
        "run_id": run.run_id,
        "repo": root,
        "workflow": _relative(root, workflow) if wt.within(workflow, root) else workflow,
        "workflow_sha256": plan["workflow_sha256"],
        "fingerprint": fingerprint,
        "plan": plan,
        "approval": decision.record(),
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "packages": {package["id"]: _package_state() for package in plan["packages"]},
    }
    run.save(state)
    events = EventLog(run.events_path, clock=clock)
    try:
        events.emit("approval.requested", approval="run", subject=plan["name"], fingerprint=fingerprint)
        events.emit(
            "approval.decided",
            approval="run",
            subject=plan["name"],
            fingerprint=fingerprint,
            approved=True,
            approver=decision.approver,
            reason=decision.reason,
        )
        events.emit(
            "run.started",
            run_id=run.run_id,
            workflow=state["workflow"],
            workflow_sha256=plan["workflow_sha256"],
            fingerprint=fingerprint,
            packages=[package["id"] for package in plan["packages"]],
            integration_branch=integration,
        )
    finally:
        events.close()
    return run


def check_resumable(run: store.RunDir, state: dict | None = None) -> dict:
    """The stored state, when the run can resume: unfinished, and its plan still the one that was approved."""
    state = run.load() if state is None else state
    if state["status"] in {"completed", "cancelled"}:
        raise store.RunError(f"Run {run.run_id} is {state['status']}; there is nothing to resume.")
    if (
        contract.fingerprint(state["plan"]) != state["fingerprint"]
        or state["approval"]["fingerprint"] != state["fingerprint"]
    ):
        raise permissions.PermissionRefused(f"Run {run.run_id}'s plan no longer matches its approval.")
    workflow = state["workflow"]
    path = workflow if os.path.isabs(workflow) else os.path.join(state["repo"], *workflow.split("/"))
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != state["workflow_sha256"]:
                raise permissions.PermissionRefused(
                    f"{workflow} changed after run {run.run_id} was approved; the approved plan does not resume "
                    "under a changed contract. Start a new run to approve the new one."
                )
    return state


def cancel_run(run: store.RunDir, clock: Callable[[], float] = time.time) -> dict:
    """End a run for good: every unfinished package is stopped and its worktree removed. Branches are kept."""
    with store.repository_lock(run.root, f"cancelling run {run.run_id}"), run.lock():
        state = run.load()
        events = EventLog(run.events_path, clock=clock)
        stopped = []
        try:
            for package_id, package in state["packages"].items():
                if package["state"] in states.TERMINAL:
                    continue
                states.check(package["state"], "stopped")
                events.emit(
                    "package.state_changed",
                    package_id=package_id,
                    **{"from": package["state"], "to": "stopped"},
                    cause="the operator cancelled the run",
                    attempt=package["attempts"],
                )
                package["state"] = "stopped"
                stopped.append(package_id)
                path = wt.worktree_path(state["repo"], package_id)
                # Only a worktree this run provisioned: another run's partial work under the same id is not ours.
                if os.path.isdir(path) and wt.worktree_owner(state["repo"], package_id) == run.run_id:
                    wt.remove(path)
                    events.emit("worktree.removed", package_id=package_id, path=_relative(state["repo"], path))
            if not claimed_packages(state["repo"], besides=run.run_id):
                try:
                    mq.remove_integration(state["repo"], state["plan"]["integration_branch"])
                except wt.WorktreeError:
                    pass
            state["status"] = "cancelled"
            state["updated_at"] = clock()
            run.save(state)
            events.emit("run.cancelled", run_id=run.run_id, packages=stopped)
        finally:
            events.close()
        return state


# ── the engine ───────────────────────────────────────────────────────────


class Conductor:
    """Drives one run until every package has landed, the run is stopped, or nothing more can move."""

    # A land step that errors may already have moved main, so it is never reworked. It is retried after a pause that
    # doubles each time (capped at LANDING_BACKOFF_CAP), and after this many errors in a row the package is held.
    max_landing_errors = 3
    landing_backoff = 1.0

    def __init__(
        self,
        run: store.RunDir,
        *,
        approver: permissions.Approver,
        grants: permissions.GrantStore,
        clock: Callable[[], float] = time.time,
        poll: float = 0.05,
        hold_wait: float = 0.0,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.run = run
        self.state = run.load()
        self.plan = self.state["plan"]
        self.root = self.state["repo"]
        self.run_id = self.state["run_id"]
        self.integration = self.plan["integration_branch"]
        self.identity = self.plan.get("identity")
        self.manifest = self.plan["environment"]
        self.approver = approver
        self.grants = grants
        self.clock = clock
        self.poll = poll
        self.hold_wait = hold_wait
        self._on_event = on_event
        self.events: EventLog | None = None  # opened under the run's locks
        self.receipts = ReceiptStore(run.receipts_path)
        self.leases = wt.LeaseManager()
        self.work: dict[str, Work] = {}
        self.specs = {package["id"]: package for package in self.plan["packages"]}
        self.order = [package["id"] for package in self.plan["packages"]]
        self._stop = threading.Event()
        self._stopping = False
        self._held_since: float | None = None
        self._fingerprint_due = 0.0
        self._pool: ThreadPoolExecutor | None = None

    # -- public ------------------------------------------------------------

    def request_stop(self) -> None:
        """Stop running stages and hold the run. Implement work is kept; cancelled checks are cleaned up."""
        self._stop.set()

    def run_until_settled(self) -> str:
        with store.repository_lock(self.root, f"run {self.run_id}"), self.run.lock():
            # Read again under the locks: another process may have cancelled, finished or edited the run since this
            # engine was built, and it must not drive a stale copy over that.
            self.state = check_resumable(self.run, self.run.load())
            self.grants.require(self.root)
            self.events = EventLog(self.run.events_path, clock=self.clock)
            if self._on_event is not None:
                self.events.subscribe(self._on_event)
            self._pool = ThreadPoolExecutor(max_workers=self.plan["max_parallel"] + 1, thread_name_prefix="conductor")
            try:
                self._recover()
                while True:
                    if not self._stopping and (self._stop.is_set() or self.run.stop_requested()):
                        self._begin_stop()
                    self._collect()
                    if self._stopping:
                        if not self.work:
                            self._finish_stop()
                            break
                    else:
                        self._advance()
                        if not self.work and self._settled():
                            self._finish()
                            break
                    time.sleep(self.poll)
            except BaseException:
                for work in self.work.values():
                    work.cancel.set()
                raise
            finally:
                self._pool.shutdown(wait=True)
                self.events.close()
        return self.state["status"]

    # -- state -------------------------------------------------------------

    def _pkg(self, package_id: str) -> dict:
        return self.state["packages"][package_id]

    def _save(self) -> None:
        self.state["updated_at"] = self.clock()
        self.run.save(self.state)

    def _move(self, package_id: str, target: str, cause: str) -> None:
        package = self._pkg(package_id)
        source = package["state"]
        states.check(source, target)
        package["state"] = target
        self.events.emit(
            "package.state_changed",
            package_id=package_id,
            **{"from": source, "to": target},
            cause=cause,
            attempt=package["attempts"],
        )
        self._save()

    def _key(self, package_id: str, stage: str) -> str:
        number = 1 + sum(1 for run in self._pkg(package_id)["runs"] if run["stage"] == stage)
        if number > 99:
            raise SchedulerError(f"{package_id} has run its {stage} stage 99 times.")
        return f"{self.run_id}:{package_id}:{stage}:{number}"

    def _record_run(self, package_id: str, key: str, stage: str, continuation: bool, label: str | None = None) -> None:
        package = self._pkg(package_id)
        package["runs"].append(
            {
                "key": key,
                "stage": stage,
                "label": label or stage,
                "attempt": package["attempts"],
                "continuation": continuation,
                "started_at": self.clock(),
                "ended_at": None,
                "outcome": None,
            }
        )
        self.events.emit(
            "stage.started",
            package_id=package_id,
            key=key,
            stage=label or stage,
            attempt=package["attempts"],
            continuation=continuation,
        )

    def _end_run(self, package_id: str, key: str, outcome: str, detail: str = "") -> None:
        package = self._pkg(package_id)
        for run in reversed(package["runs"]):
            if run["key"] == key:
                run.update(ended_at=self.clock(), outcome=outcome)
                self.events.emit(
                    "stage.ended",
                    package_id=package_id,
                    key=key,
                    stage=run.get("label", run["stage"]),
                    attempt=run["attempt"],
                    outcome=outcome,
                    detail=detail[:500],
                )
                return

    def _submit(self, package_id: str, kind: str, job: Callable, *args, abandonable: bool = False) -> Work:
        cancel = threading.Event()
        if abandonable:
            # Its own daemon thread: an approval that waits on a person must not keep a Stop waiting.
            future: Future = Future()

            def target() -> None:
                future.set_result(self._guard(job, *args, cancel))

            threading.Thread(target=target, name=f"conductor-{kind}-{package_id}", daemon=True).start()
        else:
            future = self._pool.submit(self._guard, job, *args, cancel)
        work = Work(kind, future, cancel, abandonable)
        self.work[package_id] = work
        return work

    @staticmethod
    def _guard(job: Callable, *args) -> dict:
        try:
            return job(*args)
        except (wt.WorktreeError, wt.LeaseTimeout, permissions.PermissionRefused, OSError, ValueError) as exc:
            return {"outcome": "error", "detail": f"{type(exc).__name__}: {exc}"[:2000]}
        except Exception as exc:  # a defect: kept whole in the receipt
            return {
                "outcome": "error",
                "detail": f"{type(exc).__name__}: {exc}"[:2000],
                "trace": traceback.format_exc()[-4000:],
            }

    # -- the loop ----------------------------------------------------------

    def _recover(self) -> None:
        self.run.clear_stop()
        first = self.state["status"] == "ready"
        resumed = []
        for package_id in self.order:
            package = self._pkg(package_id)
            current = package["state"]
            if package.get("landing"):
                package["landing"].update(errors=0, retry_at=0)  # a resume gives a failing landing fresh tries
            if current == "held":
                self._move(package_id, "ready", "Resume continues the same attempt")
                package["continuation"] = True
                resumed.append(package_id)
            elif current in states.ACTIVE:
                self._move(package_id, "uncertain", "the run ended without a clean stop")
                self._move(package_id, "ready", "Resume keeps implement work and continues the attempt")
                package["continuation"] = True
                if current == "merging" and (package.get("landing") or {}).get("phase") == "check":
                    package["landing"]["phase"] = "build"
                resumed.append(package_id)
            elif current == "uncertain":
                # A process that died between saving uncertain and ready left it here.
                self._move(package_id, "ready", "Resume keeps implement work and continues the attempt")
                package["continuation"] = True
                resumed.append(package_id)
            elif current == "failed":
                # A process that died between saving failed and the retry decision left it here.
                self._decide_retry(package_id)
                resumed.append(package_id)
            elif current == "merge_held":
                package["landing"]["fingerprint"] = None  # try the landing again now
                resumed.append(package_id)
        self.state["status"] = "running"
        self._save()
        if not first:
            self.events.emit("run.resumed", run_id=self.run_id, packages=resumed)

    def _advance(self) -> None:
        for package_id in self.order:
            package = self._pkg(package_id)
            if package["state"] == "waiting" and all(
                self._pkg(dep)["state"] == "accepted" for dep in self.specs[package_id]["depends_on"]
            ):
                deps = self.specs[package_id]["depends_on"]
                self._move(package_id, "ready", f"{', '.join(deps)} landed" if deps else "nothing to wait for")
        if self._stop.is_set() or self.run.stop_requested():
            return  # no new stage starts once a stop is asked for; the next pass begins the stop
        busy = sum(1 for work in self.work.values() if work.kind in {"implement", "verify"})
        for package_id in self.order:
            package = self._pkg(package_id)
            if package["state"] != "ready" or package_id in self.work:
                continue
            stage = package["stage"]
            if stage == "land":
                self._enqueue(package_id, "a continued attempt rejoins the merge queue")
            elif stage == "approve":
                self._start_approval(package_id)
            elif busy < self.plan["max_parallel"]:
                if stage == "implement":
                    self._start_implement(package_id)
                else:
                    self._start_verify(package_id)
                busy += 1
        self._drive_queue()

    def _collect(self) -> None:
        handlers = {
            "implement": self._implemented,
            "verify": self._verified,
            "approve": self._approved,
            "build": self._built,
            "check": self._rechecked,
            "land": self._landed,
        }
        for package_id, work in list(self.work.items()):
            if work.future.done():
                del self.work[package_id]
                handlers[work.kind](package_id, work.future.result())

    def _settled(self) -> bool:
        packages = self.state["packages"].values()
        held = any(package["state"] == "merge_held" for package in packages)
        moving = {"ready", "running", "verifying", "awaiting_approval", "merging", "failed"}
        if not held:
            moving.add("queued_for_merge")  # behind a held landing the queue cannot move, so it does not count
        if any(package["state"] in moving for package in packages):
            return False
        if any(
            package["state"] == "merge_held" and (package.get("landing") or {}).get("errors") for package in packages
        ):
            return False  # a landing error's retry is still due
        if any(package["state"] == "merge_held" for package in packages):
            if self._held_since is None:
                self._held_since = time.monotonic()
            return time.monotonic() - self._held_since >= self.hold_wait
        return True

    def _finish(self) -> None:
        packages = self.state["packages"]
        accepted = [pid for pid in self.order if packages[pid]["state"] == "accepted"]
        unfinished = [pid for pid in self.order if packages[pid]["state"] != "accepted"]
        if not unfinished:
            status = "completed"
            try:
                with self.leases.lease(mq.integration_path(self.root, self.integration), "teardown"):
                    mq.remove_integration(self.root, self.integration)
            except (wt.WorktreeError, wt.LeaseTimeout):
                pass
        elif any(packages[pid]["state"] in {"merge_held", "held"} for pid in unfinished):
            status = "held"
            reasons = [
                packages[pid]["error"] or "" for pid in unfinished if packages[pid]["state"] in {"merge_held", "held"}
            ]
            self.events.emit("run.held", run_id=self.run_id, packages=unfinished, reason=reasons[0][:500])
        else:
            status = "failed"
        self.state["status"] = status
        self._save()
        self.events.emit("run.finished", run_id=self.run_id, status=status, accepted=accepted, unfinished=unfinished)

    # -- stop --------------------------------------------------------------

    def _begin_stop(self) -> None:
        self._stopping = True
        self.events.emit(
            "run.stopping", run_id=self.run_id, by="request file" if self.run.stop_requested() else "signal"
        )
        for package_id, work in list(self.work.items()):
            if work.abandonable:
                del self.work[package_id]
            else:
                work.cancel.set()

    def _finish_stop(self) -> None:
        held = []
        for package_id in self.order:
            package = self._pkg(package_id)
            if package["state"] in states.HOLDABLE:
                package["held_from"] = package["state"]
                if package["state"] in {"queued_for_merge", "merging", "merge_held"}:
                    package["stage"] = "land"
                elif package["state"] == "awaiting_approval":
                    package["stage"] = "approve"
                self._move(package_id, "held", "the operator stopped the run; implement work is kept")
                held.append(package_id)
        self.state["status"] = "held"
        self._save()
        self.run.clear_stop()
        self.events.emit("run.held", run_id=self.run_id, packages=held, reason="stopped by the operator")

    # -- implement ---------------------------------------------------------

    def _start_implement(self, package_id: str) -> None:
        package = self._pkg(package_id)
        # A new attempt is spent only by the first start and by a rework; a resumed stage continues the attempt.
        new_attempt = package["attempts"] == 0 or bool(package["new_attempt"])
        if new_attempt:
            package["attempts"] += 1
        if self.specs[package_id]["agent"]["adapter"] == "claude_code" and (new_attempt or not package["session_id"]):
            package["session_id"] = str(uuid.uuid4())
        continuation = not new_attempt
        package.update(continuation=False, new_attempt=False)
        # Cleared only once a worktree has been provisioned: a crash before that must still start from the tip.
        fresh = bool(package["fresh"])
        key = self._key(package_id, "implement")
        attempt = package["attempts"]
        self._move(package_id, "running", f"implement, attempt {attempt}" + (" continued" if continuation else ""))
        self._record_run(package_id, key, "implement", continuation)
        self._save()
        self._submit(
            package_id,
            "implement",
            self._implement_job,
            package_id,
            key,
            attempt,
            continuation,
            fresh,
            package["feedback"],
            package["session_id"],
        )

    def _implement_job(
        self,
        package_id: str,
        key: str,
        attempt: int,
        continuation: bool,
        fresh: bool,
        feedback: str | None,
        session_id: str | None,
        cancel: threading.Event,
    ) -> dict:
        spec = self.specs[package_id]
        path = wt.worktree_path(self.root, package_id)
        with self.leases.lease(path, key):
            try:
                info = wt.provision(
                    self.root,
                    package_id,
                    self.integration,
                    self.manifest,
                    fresh=fresh,
                    preserve_label=f"attempt-{attempt - 1}" if fresh else None,
                    run_id=self.run_id,
                    attempt=attempt,
                    cancel=cancel,
                )
            except wt.SetupCancelled as exc:
                return {"outcome": "cancelled", "detail": str(exc)}
            self._require_worktree(info["path"])
            stage_base = wt.stage_base(info["path"], self.run_id, package_id, attempt)
            self.events.emit(
                "worktree.provisioned",
                package_id=package_id,
                path=_relative(self.root, info["path"]),
                branch=info["branch"],
                base_sha=info["base_sha"],
                created=info["created"],
                preserved_branch=info.get("preserved_branch"),
            )
            provision = {
                "path": _relative(self.root, info["path"]),
                "branch": info["branch"],
                "base_sha": info["base_sha"],
            }
            if spec["agent"]["adapter"] == "claude_code" and session_id:
                continuation = wt.start_session(info["path"], "implement", session_id)
            task = agents.AgentTask(
                run_id=self.run_id,
                package_id=package_id,
                key=key,
                stage="implement",
                title=spec["title"],
                instruction=spec["instruction"],
                worktree=info["path"],
                files=list(spec["files"]),
                attempt=attempt,
                continuation=continuation,
                timeout_seconds=spec["timeout_seconds"],
                cancel=cancel,
                feedback=feedback,
                session_id=session_id,
                progress=lambda message: self.events.emit(
                    "agent.progress", package_id=package_id, key=key, message=str(message)[:300]
                ),
            )
            result = agents.create(spec["agent"]).run(task)
            base = {"provision": provision, "session_id": result.session_id}
            if result.status == "cancelled":
                return {**base, "outcome": "cancelled", "detail": result.summary}
            if result.status != "done":
                failure = {"failed": "agent_failed", "blocked": "agent_blocked", "timeout": "timeout"}[result.status]
                return {
                    **base,
                    "outcome": "failed",
                    "failure": failure,
                    "detail": result.summary or f"the agent ended {result.status}",
                }
            self._require_worktree(info["path"])
            committed = wt.commit(info["path"], "implement", key, spec["title"], self.identity)
            changed = wt.changed_files(info["path"], self.integration)
            if not changed:
                return {**base, "outcome": "failed", "failure": "empty", "detail": "the stage changed nothing"}
            outside = [name for name in changed if not contract.in_scope(name, spec["files"])]
            if outside:
                discarded = self._discard(package_id, key, attempt, stage_base, outside)
                return {
                    **base,
                    "outcome": "failed",
                    "failure": "scope_failure",
                    "detail": f"changed files outside its declared files: {', '.join(outside[:10])}",
                    "receipt": discarded,
                    "receipt_kind": "discarded",
                }
            patch = committed["patch"]
            receipt = self.receipts.write(
                package_id,
                "stage",
                {
                    "key": key,
                    "attempt": attempt,
                    "continuation": continuation,
                    "commit_sha": committed["commit_sha"],
                    "head_sha": committed["head_sha"],
                    "replayed": committed["replayed"],
                    "files": changed,
                    "patch_sha256": patch["patch_sha256"],
                    "patch_bytes": patch["bytes"],
                    "patch": patch["patch"],
                    "agent_summary": result.summary,
                },
            )
            return {
                **base,
                "outcome": "done",
                "head_sha": committed["head_sha"],
                "commit_sha": committed["commit_sha"],
                "files": changed,
                "receipt": receipt,
                "detail": result.summary,
            }

    def _implemented(self, package_id: str, result: dict) -> None:
        package = self._pkg(package_id)
        key = package["runs"][-1]["key"]
        if result.get("provision"):
            package["worktree"] = result["provision"]
            package["fresh"] = False  # the fresh worktree exists now
        if result.get("session_id"):
            package["session_id"] = result["session_id"]
        outcome = result["outcome"]
        self._end_run(package_id, key, outcome, result.get("detail", ""))
        if result.get("receipt") and outcome != "done":
            self._receipt(package_id, result.get("receipt_kind", "stage"), result["receipt"])
        if outcome == "cancelled":
            package["continuation"] = True
            self._save()
            return
        if outcome != "done":
            self._rework(package_id, result.get("failure") or "error", result.get("detail", ""))
            return
        if result.get("commit_sha"):
            self.events.emit(
                "stage.committed", package_id=package_id, key=key, sha=result["commit_sha"], files=result["files"][:200]
            )
        self._receipt(package_id, "stage", result["receipt"])
        package["stage_head"] = result["head_sha"]
        package["stage"] = "verify"
        self._move(package_id, "verifying", "the implement stage committed inside its declared files")
        if not self._stopping:
            self._start_verify(package_id, moved=True)

    # -- verify and review -------------------------------------------------

    def _require_worktree(self, path: str) -> None:
        """Every resumed dispatch or reset proves repository ownership, canonical containment and the grant."""
        wt.linked_worktree(path, self.root)
        self.grants.require(path)

    def _discard(self, package_id: str, key: str, attempt: int, target: str, outside: list[str]) -> dict:
        """Keep all rejected commits and a patch before resetting, even when a resumed stage made no new commit."""
        path = wt.worktree_path(self.root, package_id)
        self._require_worktree(path)
        current = wt.head(path)
        ref = keep_discarded(self.root, package_id, current)
        patch = wt.capture(path, target)
        recorded = self.receipts.write(
            package_id,
            "discarded",
            {
                "key": key,
                "attempt": attempt,
                "commit_sha": current,
                "ref": ref,
                "files": patch["files"],
                "outside": outside,
                "since_sha": target,
                "patch_sha256": patch["patch_sha256"],
                "patch_bytes": patch["bytes"],
                "patch": patch["patch"],
                "untracked": patch["untracked"],
            },
        )
        self._require_worktree(path)
        wt.reset(path, target)
        return recorded

    def _start_verify(self, package_id: str, moved: bool = False) -> None:
        package = self._pkg(package_id)
        continuation = bool(package["continuation"])
        package["continuation"] = False
        if not moved:
            self._move(package_id, "verifying", "the attempt continues at its checks")
        key = self._key(package_id, "verify")
        self._record_run(package_id, key, "verify", continuation)
        review_key = None
        if self.specs[package_id]["verification_tier"] != "T0":
            review_key = self._key(package_id, "review")
            self._record_run(package_id, review_key, "review", continuation)
            if self.specs[package_id]["reviewer"]["adapter"] == "claude_code" and (
                not continuation or not package.get("review_session_id")
            ):
                package["review_session_id"] = str(uuid.uuid4())
        self._save()
        self._submit(
            package_id,
            "verify",
            self._verify_job,
            package_id,
            key,
            review_key,
            package["attempts"],
            package["feedback"],
            continuation,
            package.get("review_session_id"),
            package.get("stage_head"),
        )

    def _verify_job(
        self,
        package_id: str,
        key: str,
        review_key: str | None,
        attempt: int,
        feedback: str | None,
        continuation: bool,
        review_session_id: str | None,
        stage_head: str | None,
        cancel: threading.Event,
    ) -> dict:
        spec = self.specs[package_id]
        path = wt.worktree_path(self.root, package_id)
        checks_spec = spec["acceptance"]["checks"]
        with self.leases.lease(path, key):
            self._require_worktree(path)
            # Old state without a stage head is refused, never certified by adopting whatever HEAD holds now.
            baseline = (
                wt.verification_base(path, stage_head)
                if stage_head
                else (wt._read_state(self.root, package_id) or {}).get("base_sha")
                or wt._checked(self.root, ["rev-parse", self.integration], "Could not read the integration tip:")
            )
            changed = wt.changed_files(path, self.integration)
            outside = [name for name in changed if not contract.in_scope(name, spec["files"])]
            if not stage_head or wt.head(path) != baseline or outside or not wt.is_clean(path):
                discarded = self._discard(package_id, key, attempt, baseline, outside)
                return {
                    "outcome": "failed",
                    "failure": "scope_failure" if outside else "review_scope",
                    "detail": "verification found changes outside the implement head or its declared files",
                    "receipt": discarded,
                    "receipt_kind": "discarded",
                }
            try:
                refresh = wt.refresh_base(path, self.integration, self.identity, stage_head=stage_head)
            except wt.WorktreeError as exc:
                if exc.detail.get("conflict"):
                    return {
                        "outcome": "failed",
                        "failure": "merge_conflict",
                        "fresh": True,
                        "detail": str(exc),
                        "paths": exc.detail.get("paths", []),
                    }
                raise
            if refresh["refreshed"]:
                self.events.emit(
                    "worktree.refreshed",
                    package_id=package_id,
                    old_base=refresh["old_base"],
                    new_base=refresh["new_base"],
                    head_sha=refresh["head_sha"],
                )
            head = wt.head(path)
            outside = [
                name for name in wt.changed_files(path, self.integration) if not contract.in_scope(name, spec["files"])
            ]
            if outside:
                discarded = self._discard(package_id, key, attempt, stage_head, outside)
                return {
                    "outcome": "failed",
                    "failure": "scope_failure",
                    "detail": "refresh changed files outside scope",
                    "receipt": discarded,
                    "receipt_kind": "discarded",
                }
            self._require_worktree(path)
            receipt = checks.run_checks(
                key,
                path,
                checks_spec,
                allowlist=self.manifest.get("env"),
                cancel=cancel,
                on_result=self._check_listener(package_id),
            )
            if receipt["cancelled"]:
                self._require_worktree(path)
                receipt["cleanup"] = wt.reset(path, head)
            recorded = self.receipts.write(package_id, "checks", receipt)
            if receipt["cancelled"]:
                return {"outcome": "cancelled", "detail": "the checks were stopped", "receipt": recorded}
            reason = checks.bound(receipt, key, path, checks_spec, head)
            if reason:
                return {
                    "outcome": "failed",
                    "failure": "check_failure",
                    "detail": f"checks failed: {reason}",
                    "receipt": recorded,
                }
            review = None
            if review_key:
                self._require_worktree(path)
                if spec["reviewer"]["adapter"] == "claude_code" and review_session_id:
                    continuation = wt.start_session(path, "review", review_session_id)
                task = agents.AgentTask(
                    run_id=self.run_id,
                    package_id=package_id,
                    key=review_key,
                    stage="review",
                    title=spec["title"],
                    instruction=spec["instruction"],
                    worktree=path,
                    files=list(spec["files"]),
                    attempt=attempt,
                    continuation=continuation,
                    timeout_seconds=spec["timeout_seconds"],
                    cancel=cancel,
                    feedback=feedback,
                    session_id=review_session_id,
                    progress=lambda message: self.events.emit(
                        "agent.progress", package_id=package_id, key=review_key, message=str(message)[:300]
                    ),
                )
                verdict = agents.create(spec["reviewer"]).run(task)
                sessions = {"review_session_id": verdict.session_id or review_session_id}
                if verdict.status == "cancelled":
                    return {"outcome": "cancelled", "detail": "the review was stopped", "receipt": recorded, **sessions}
                # The checks left the tree clean (checks.bound), so a reviewer that changed anything shows here.
                self._require_worktree(path)
                if wt.head(path) != head or not wt.is_clean(path):
                    discarded = self._discard(package_id, review_key, attempt, head, [])
                    return {
                        "outcome": "failed",
                        "failure": "review_scope",
                        "receipt": discarded,
                        "receipt_kind": "discarded",
                        "detail": "the reviewer changed the worktree; its changes were discarded",
                        **sessions,
                    }
                if verdict.status != "done":
                    return {
                        "outcome": "failed",
                        "failure": "agent_failed",
                        "receipt": recorded,
                        "detail": f"the reviewer ended {verdict.status}: {verdict.summary}",
                        **sessions,
                    }
                review = {
                    "key": review_key,
                    "verdict": verdict.verdict or "request_changes",
                    "summary": verdict.summary,
                }
                self.events.emit(
                    "review.verdict",
                    package_id=package_id,
                    key=review_key,
                    verdict=review["verdict"],
                    summary=verdict.summary[:300],
                )
                if review["verdict"] != "approve":
                    return {
                        "outcome": "failed",
                        "failure": "review_changes",
                        "receipt": recorded,
                        "review": review,
                        "detail": f"the reviewer asked for changes: {verdict.summary}",
                        **sessions,
                    }
            return {
                "outcome": "done",
                "head_sha": head,
                "receipt": recorded,
                "review": review,
                "detail": "checks passed" + (", review approved" if review else ""),
            }

    def _check_listener(self, package_id: str) -> Callable[[dict], None]:
        def listener(line: dict) -> None:
            self.events.emit(
                "check.result",
                package_id=package_id,
                key=line["key"],
                index=line["index"],
                argv=line["argv"],
                exit_code=line["exit_code"],
                duration_ms=line["duration_ms"],
                output_sha256=line["output_sha256"],
            )

        return listener

    def _verified(self, package_id: str, result: dict) -> None:
        package = self._pkg(package_id)
        stages = [run for run in package["runs"] if run["outcome"] is None]
        outcome = result["outcome"]
        for run in stages:
            self._end_run(package_id, run["key"], outcome, result.get("detail", ""))
        if result.get("receipt"):
            self._receipt(package_id, result.get("receipt_kind", "checks"), result["receipt"])
        if result.get("review_session_id"):
            package["review_session_id"] = result["review_session_id"]
        if outcome == "cancelled":
            package["continuation"] = True
            self._save()
            return
        if outcome != "done":
            self._rework(
                package_id, result.get("failure") or "error", result.get("detail", ""), fresh=bool(result.get("fresh"))
            )
            return
        package["verified_head"] = result["head_sha"]
        if self.specs[package_id]["verification_tier"] == "T3":
            package["stage"] = "approve"
            if not self._stopping:
                self._start_approval(package_id)
            return
        package["stage"] = "land"
        self._enqueue(
            package_id,
            "the checks passed on the stage head" + (" and the review approved" if result.get("review") else ""),
        )

    # -- approval (T3) -----------------------------------------------------

    def _start_approval(self, package_id: str) -> None:
        package = self._pkg(package_id)
        spec = self.specs[package_id]
        head = package["verified_head"]
        self._move(package_id, "awaiting_approval", "tier T3: the approver decides before the landing")
        request = permissions.ApprovalRequest(
            "land",
            package_id,
            permissions.fingerprint({"run": self.run_id, "package": package_id, "head": head}),
            [
                f"{package_id}: {spec['title']}",
                f"head {head[:12]} on {package['worktree']['branch']}",
                f"may change {', '.join(spec['files'][:6])}",
            ],
            {"tiers": [spec["verification_tier"]], "head": head},
        )
        self.events.emit("approval.requested", approval="land", subject=package_id, fingerprint=request.fingerprint)
        self._submit(package_id, "approve", self._approval_job, request, abandonable=True)

    def _approval_job(self, request: permissions.ApprovalRequest, cancel: threading.Event) -> dict:
        decision = self.approver.approve(request)
        return {"outcome": "decided", "request": request, "decision": decision}

    def _approved(self, package_id: str, result: dict) -> None:
        if result["outcome"] != "decided":
            self._move(package_id, "stopped", f"the approval could not be asked: {result.get('detail')}")
            return
        request, decision = result["request"], result["decision"]
        self.events.emit(
            "approval.decided",
            approval="land",
            subject=package_id,
            fingerprint=request.fingerprint,
            approved=decision.binds(request),
            approver=decision.approver,
            reason=decision.reason,
        )
        package = self._pkg(package_id)
        if decision.binds(request):
            package["approval"] = decision.record()
            package["stage"] = "land"
            self._enqueue(package_id, f"the approver approved head {package['verified_head'][:12]}")
        else:
            package["error"] = f"the approver refused the landing: {decision.reason}"
            self._move(package_id, "stopped", package["error"])

    # -- the merge queue ---------------------------------------------------

    def _enqueue(self, package_id: str, cause: str) -> None:
        package = self._pkg(package_id)
        package["joined_at"] = self.clock()
        self._move(package_id, "queued_for_merge", cause)
        position = sum(1 for pid in self.order if self._pkg(pid)["state"] == "queued_for_merge")
        self.events.emit("merge.queued", package_id=package_id, position=position)

    def _drive_queue(self) -> None:
        if any(work.kind in LANDING_KINDS for work in self.work.values()):
            return
        merging = [pid for pid in self.order if self._pkg(pid)["state"] == "merging"]
        if merging:
            self._start_landing_phase(merging[0])
            return
        held = [pid for pid in self.order if self._pkg(pid)["state"] == "merge_held"]
        if held:
            package_id = held[0]
            landing = self._pkg(package_id)["landing"]
            if self.clock() < float(landing.get("retry_at") or 0):
                return  # backing off after a landing error
            if landing.get("fingerprint") is not None:
                # A change to the checkout retries for as long as the run lives. A refusal with no watched cause
                # also retries periodically, but only while hold_wait lasts.
                if time.monotonic() < self._fingerprint_due:
                    return
                self._fingerprint_due = time.monotonic() + FINGERPRINT_POLL
                watch = landing.get("watch") or ()
                changed = mq.checkout_fingerprint(self.root, watch, self.integration) != landing["fingerprint"]
                due = landing.get("periodic_retry_at")
                waiting = self._held_since is None or time.monotonic() - self._held_since < self.hold_wait
                if not changed and (due is None or self.clock() < due or not waiting):
                    return
            landing["phase"] = "land"
            self._move(package_id, "merging", "the main checkout changed, a retry fell due, or the run resumed")
            self._start_landing_phase(package_id)
            return  # a held landing goes first: main cannot move past it
        queued = sorted(
            (self._pkg(pid)["joined_at"] or 0, self.order.index(pid), pid)
            for pid in self.order
            if self._pkg(pid)["state"] == "queued_for_merge"
        )
        if not queued:
            return
        package_id = queued[0][2]
        package = self._pkg(package_id)
        landing = package.get("landing") or {}
        resumable = landing.get("phase") == "land" and landing.get("landing_sha")
        package["landing"] = landing if resumable else {"phase": "build"}
        self._move(package_id, "merging", "the queue's turn: one landing at a time")
        self._start_landing_phase(package_id)

    def _start_landing_phase(self, package_id: str) -> None:
        package = self._pkg(package_id)
        landing = package["landing"]
        phase = landing["phase"]
        spec = self.specs[package_id]
        if phase == "build":
            self._submit(package_id, "build", self._build_job, package_id, spec["title"], package["verified_head"])
        elif phase == "check":
            key = self._key(package_id, "verify")
            landing["check_key"] = key
            self._record_run(package_id, key, "verify", False, label="recheck")
            self._save()
            self._submit(package_id, "check", self._recheck_job, package_id, key, landing["landing_sha"])
        elif phase == "land":
            self._submit(
                package_id,
                "land",
                self._land_job,
                package_id,
                landing["landing_sha"],
                landing["parent_sha"],
                landing.get("package_sha"),
            )
        else:
            raise SchedulerError(f"{package_id}: unknown landing phase {phase!r}.")

    def _build_job(self, package_id: str, title: str, verified_sha: str, cancel: threading.Event) -> dict:
        with self.leases.lease(mq.integration_path(self.root, self.integration), f"build:{package_id}"):
            try:
                built = mq.build(
                    self.root,
                    self.integration,
                    package_id,
                    self.run_id,
                    title,
                    wt.branch_name(package_id),
                    manifest=self.manifest,
                    identity=self.identity,
                    verified_sha=verified_sha,
                    cancel=cancel,
                )
            except wt.SetupCancelled as exc:
                return {"outcome": "cancelled", "detail": str(exc)}
        return {"outcome": "built", **built}

    def _recheck_job(self, package_id: str, key: str, landing_sha: str, cancel: threading.Event) -> dict:
        with self.leases.lease(mq.integration_path(self.root, self.integration), key):
            self._require_worktree(mq.integration_path(self.root, self.integration))
            checks_spec = self.specs[package_id]["acceptance"]["checks"]
            verdict = mq.recheck(
                self.root,
                self.integration,
                landing_sha,
                checks_spec,
                key,
                allowlist=self.manifest.get("env"),
                cancel=cancel,
                on_result=self._check_listener(package_id),
            )
        recorded = (
            self.receipts.write(package_id, "recheck", {**verdict, "landing_sha": landing_sha})
            if verdict.get("receipt")
            else None
        )
        return {"outcome": "rechecked", **verdict, "recorded": recorded}

    def _land_job(
        self, package_id: str, landing_sha: str, parent_sha: str, package_sha: str | None, cancel: threading.Event
    ) -> dict:
        with self.leases.lease(os.path.join(self.root, ".git"), f"land:{package_id}"):
            landed = mq.land(self.root, self.integration, package_id, landing_sha, parent_sha, package_sha)
        return {"outcome": "landed" if landed.get("landed") else "not_landed", **landed}

    def _built(self, package_id: str, result: dict) -> None:
        package = self._pkg(package_id)
        if result["outcome"] == "error":
            self._rework(package_id, "merge_conflict", f"the landing could not be built: {result['detail']}")
            return
        if result["outcome"] == "cancelled":
            self._save()  # the integration worktree's setup was stopped; the build runs again on resume
            return
        if result.get("moved"):
            self._rework(
                package_id,
                "branch_moved",
                f"the package branch moved to {result['package_sha'][:12]} after its checks passed on "
                f"{result['verified_sha'][:12]}; nothing unverified lands",
            )
            return
        if result.get("conflict") or result.get("empty"):
            paths = list(result.get("paths") or [])
            self.events.emit(
                "merge.conflict",
                package_id=package_id,
                paths=paths[:200],
                integration_sha=result.get("integration_sha"),
                package_sha=result.get("package_sha"),
            )
            what = ", ".join(paths[:10]) if paths else "nothing to land: the branch adds nothing to it"
            self._rework(
                package_id,
                "merge_conflict" if result.get("conflict") else "empty",
                f"the package does not squash onto {self.integration} at "
                f"{str(result.get('integration_sha'))[:12]}: {what}",
                fresh=True,
            )
            return
        package["landing"].update(
            phase="check",
            landing_sha=result["landing_sha"],
            parent_sha=result["parent_sha"],
            package_sha=result["package_sha"],
            files=result["files"][:200],
        )
        self.events.emit(
            "merge.built",
            package_id=package_id,
            landing_sha=result["landing_sha"],
            parent_sha=result["parent_sha"],
            package_sha=result["package_sha"],
            files=[entry["path"] for entry in result["files"][:200]],
            replayed=result["replayed"],
        )
        self._save()

    def _rechecked(self, package_id: str, result: dict) -> None:
        package = self._pkg(package_id)
        landing = package["landing"]
        key = landing.get("check_key")
        if result["outcome"] == "error":
            self._end_run(package_id, key, "error", result["detail"])
            landing["phase"] = "build"  # built again from a clean tip on the next turn
            self._rework(package_id, "recheck_failure", f"the re-check could not run: {result['detail']}")
            return
        if result.get("recorded"):
            self._receipt(package_id, "recheck", result["recorded"])
        if result.get("cancelled"):
            self._end_run(package_id, key, "cancelled", "the re-check was stopped")
            landing["phase"] = "build"  # the integration tree may be part-written; it is rebuilt clean
            self._save()
            return
        self.events.emit(
            "merge.rechecked",
            package_id=package_id,
            landing_sha=landing["landing_sha"],
            passed=bool(result["passed"]),
            reason=result.get("reason"),
        )
        if result["passed"]:
            self._end_run(package_id, key, "done", f"checks passed on landing {landing['landing_sha'][:12]}")
            landing["phase"] = "land"
            self._save()
            return
        self._end_run(package_id, key, "failed", result.get("reason") or "")
        self._rework(
            package_id,
            "recheck_failure",
            f"the checks failed on the landing commit {landing['landing_sha'][:12]}: {result.get('reason')}",
        )

    def _landed(self, package_id: str, result: dict) -> None:
        package = self._pkg(package_id)
        landing = package["landing"]
        if result["outcome"] == "error":
            # The fast-forward may have happened: a landing that may be on the branch is never reworked. It is
            # retried after a growing pause (a replay reports it landed), and held for the operator after
            # max_landing_errors in a row.
            errors = int(landing.get("errors") or 0) + 1
            pause = min(self.landing_backoff * 2 ** (errors - 1), LANDING_BACKOFF_CAP)
            package["error"] = (f"the landing step failed ({errors} of {self.max_landing_errors}): {result['detail']}")[
                :2000
            ]
            landing.update(errors=errors, fingerprint=None, phase="land", retry_at=self.clock() + pause)
            self.events.emit("merge.held", package_id=package_id, reason=package["error"][:500])
            if errors >= self.max_landing_errors:
                package.update(stage="land", held_from="merging")
                self._move(package_id, "held", f"{package['error'][:400]}; `conductor resume` tries it again")
            else:
                self._move(package_id, "merge_held", f"{package['error'][:400]}; retrying in {pause:.1f}s")
            return
        landing.update(errors=0, retry_at=0)
        if result.get("stale"):
            self.events.emit("merge.stale", package_id=package_id, head_sha=result["head_sha"])
            landing["phase"] = "build"
            self._save()
            return
        if not result.get("landed"):
            if self._held_since is None:
                self._held_since = time.monotonic()
            reason = str(result.get("reason") or "the main checkout refused the fast-forward")
            landing["fingerprint"] = result.get("fingerprint")
            landing["watch"] = list(result.get("watch") or [])
            landing["periodic_retry_at"] = self.clock() + UNWATCHED_RETRY if result.get("retry_periodically") else None
            package["error"] = f"landing held: {reason}"
            self._move(package_id, "merge_held", package["error"][:500])
            self.events.emit("merge.held", package_id=package_id, reason=reason[:500])
            return
        package["landed_sha"] = result["sha"]
        self._held_since = None
        package["error"] = None
        landing.update(phase="landed", receipts_ref=result.get("receipts_ref"))
        recorded = self.receipts.write(
            package_id,
            "landing",
            {
                "landing_sha": result["sha"],
                "parent_sha": result["parent_sha"],
                "package_sha": landing.get("package_sha"),
                "integration_branch": self.integration,
                "receipts_ref": result.get("receipts_ref"),
                "receipts_error": result.get("receipts_error"),
                "already": result.get("already"),
                "files": landing.get("files"),
                "approval": package.get("approval"),
            },
        )
        self._receipt(package_id, "landing", recorded)
        self.events.emit(
            "merge.landed",
            package_id=package_id,
            sha=result["sha"],
            parent_sha=result["parent_sha"],
            receipts_ref=result.get("receipts_ref"),
            already=bool(result.get("already")),
        )
        self._move(package_id, "accepted", f"landed on {self.integration} as {result['sha'][:12]}")
        self._teardown(package_id, result.get("receipts_ref"))

    def _teardown(self, package_id: str, receipts_ref: str | None) -> None:
        """Remove the landed package's worktree; prune its branch when the receipts ref holds its tip."""
        path = wt.worktree_path(self.root, package_id)
        try:
            with self.leases.lease(path, f"teardown:{package_id}"):
                if os.path.isdir(path):
                    wt.remove(path)
                    self.events.emit("worktree.removed", package_id=package_id, path=_relative(self.root, path))
                if receipts_ref:
                    wt.delete_branch_if_kept(self.root, wt.branch_name(package_id), receipts_ref)
        except (wt.WorktreeError, wt.LeaseTimeout):
            pass  # the landing stands; a leftover worktree is cleaned by the next provision or a cancel

    # -- rework ------------------------------------------------------------

    def _rework(self, package_id: str, failure: str, detail: str, *, fresh: bool = False) -> None:
        """A stage or landing that cannot land goes back to the package as a new attempt, or spends its budget."""
        package = self._pkg(package_id)
        package.update(
            failure=failure,
            error=detail[:2000],
            feedback=detail[:2000],
            landing=None,
            stage="implement",
            continuation=False,
            new_attempt=True,
            fresh=fresh,
            verified_head=None,
            review_session_id=None,
        )
        self._move(package_id, "failed", f"{failure}: {detail}"[:500])
        self._decide_retry(package_id)

    def _decide_retry(self, package_id: str) -> None:
        """A failed package becomes a new attempt, or spends its budget. Also run by recovery, for a package a
        crash left failed."""
        package = self._pkg(package_id)
        spec = self.specs[package_id]
        if package["attempts"] >= spec["max_attempts"]:
            self._move(package_id, "budget_spent", f"{package['attempts']} of {spec['max_attempts']} attempts spent")
        else:
            package["new_attempt"] = True
            self._move(
                package_id,
                "ready",
                f"a new attempt ({package['attempts'] + 1} of {spec['max_attempts']})"
                + (" from the current tip" if package.get("fresh") else ""),
            )

    def _receipt(self, package_id: str, kind: str, recorded: dict) -> None:
        self.events.emit(
            "receipt.recorded", package_id=package_id, receipt=kind, path=recorded["path"], sha256=recorded["sha256"]
        )
