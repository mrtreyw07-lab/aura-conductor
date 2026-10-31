"""The scheduler end to end on throwaway repositories, with scripted agents."""

from __future__ import annotations

import os
import threading
import time

import pytest

from conductor import merge_queue as mq
from conductor import permissions, scheduler, store
from conductor.permissions import PermissionRefused, PolicyApprover
from conductor.worktrees import WorktreeError

from .conftest import ALLOW_ALL, FAIL, PASS, Harness, events_of, git, package, workflow, write_workflow

pytestmark = pytest.mark.integration


def write_step(name: str, text: str, **extra) -> dict:
    return {"write": name, "content": text, **extra}


def states_of(run) -> dict:
    return {pid: entry["state"] for pid, entry in run.load()["packages"].items()}


def first_parent_log(repo: str) -> list[str]:
    return git(repo, "log", "--first-parent", "--format=%s", "main").splitlines()


def test_dependencies_land_in_order_and_main_only_fast_forwards(harness):
    run, status = harness.run(
        workflow(
            package("A", [write_step("a.txt", "a")]),
            package("B", [write_step("b.txt", "b")], depends_on=("A",)),
            package("C", [write_step("c.txt", "c")]),
        )
    )
    assert status == "completed" and set(states_of(run).values()) == {"accepted"}
    log = first_parent_log(harness.repo)
    assert sorted(log[:3]) == ["A: Package A", "B: Package B", "C: Package C"] and log[3] == "Initial commit"
    events = events_of(run)
    landed_a = next(e["seq"] for e in events if e["kind"] == "merge.landed" and e["package_id"] == "A")
    started_b = next(e["seq"] for e in events if e["kind"] == "stage.started" and e["package_id"] == "B")
    assert started_b > landed_a  # B starts only after A lands
    provisioned_b = next(e for e in events if e["kind"] == "worktree.provisioned" and e["package_id"] == "B")
    assert (
        git(
            harness.repo,
            "merge-base",
            "--is-ancestor",
            git(harness.repo, "log", "--format=%H", "-n", "1", "--grep", "Conductor-Landing: .*:A$", "main"),
            provisioned_b["base_sha"],
        )
        == ""
    )
    for line in git(harness.repo, "reflog", "--format=%gs", "refs/heads/main").splitlines()[:-1]:
        assert "Fast-forward" in line or line.startswith("conductor: land")
    assert not os.path.exists(os.path.join(harness.repo, ".conductor", "worktrees", "A"))
    assert git(harness.repo, "branch", "--list", "conductor/*") == ""  # pruned; the receipts refs keep the commits
    assert len(git(harness.repo, "for-each-ref", "refs/conductor/receipts/").splitlines()) == 3
    assert git(harness.repo, "status", "--porcelain") == ""


def test_a_failed_check_is_rework_and_the_next_attempt_lands(harness):
    check = {"argv": ["{python}", "-c", "import sys; sys.exit(open('a.txt').read() != 'right')"], "timeout_seconds": 60}
    run, status = harness.run(
        workflow(
            package(
                "A",
                [write_step("a.txt", "wrong", attempts=[1]), write_step("a.txt", "right", attempts=[2])],
                checks=[check],
            )
        )
    )
    entry = run.load()["packages"]["A"]
    assert status == "completed" and entry["attempts"] == 2 and entry["failure"] == "check_failure"
    causes = [e["cause"] for e in events_of(run, "package.state_changed") if e["to"] == "failed"]
    assert causes and causes[0].startswith("check_failure: checks failed")


def test_a_budget_spent_package_holds_back_its_dependents(harness):
    run, status = harness.run(
        workflow(
            package("A", [{"fail": "cannot do it"}], max_attempts=2),
            package("B", [write_step("b.txt", "b")], depends_on=("A",)),
        )
    )
    assert status == "failed" and states_of(run) == {"A": "budget_spent", "B": "waiting"}
    assert run.load()["packages"]["A"]["attempts"] == 2
    assert first_parent_log(harness.repo) == ["Initial commit"]


def test_files_outside_the_declared_scope_are_discarded_and_reworked(harness):
    run, status = harness.run(
        workflow(package("A", [write_step("a.txt", "a"), write_step("outside.txt", "nope", attempts=[1])]))
    )
    entry = run.load()["packages"]["A"]
    assert status == "completed" and entry["attempts"] == 2 and entry["failure"] == "scope_failure"
    assert "outside.txt" not in git(harness.repo, "ls-tree", "-r", "--name-only", "main")


def test_an_agent_that_changes_nothing_is_reworked(harness):
    run, status = harness.run(workflow(package("A", [], max_attempts=1)))
    assert status == "failed" and run.load()["packages"]["A"]["failure"] == "empty"


def test_review_can_ask_for_changes_and_a_reviewer_must_not_write(harness):
    reviewer = {"adapter": "scripted", "name": "reviewer", "verdicts": ["request_changes", "approve"]}
    run, status = harness.run(workflow(package("A", [write_step("a.txt", "a")], tier="T1", reviewer=reviewer)))
    entry = run.load()["packages"]["A"]
    assert status == "completed" and entry["attempts"] == 2 and entry["failure"] == "review_changes"
    assert [e["verdict"] for e in events_of(run, "review.verdict")] == ["request_changes", "approve"]

    meddler = {"adapter": "scripted", "name": "meddler", "review_steps": [write_step("a.txt", "edited")]}
    run, status = harness.run(
        workflow(package("B", [write_step("b.txt", "b")], tier="T1", reviewer=meddler, max_attempts=1))
    )
    assert status == "failed" and run.load()["packages"]["B"]["failure"] == "review_scope"


def test_tier_three_lands_only_with_the_approvers_yes(harness, conductor_home):
    reviewer = {"adapter": "scripted", "family": "other"}
    data = workflow(package("A", [write_step("a.txt", "a")], tier="T3", reviewer=reviewer))
    run, status = harness.run(data)
    assert status == "completed"
    decided = [e for e in events_of(run, "approval.decided") if e["approval"] == "land"]
    assert decided and decided[0]["approved"] and decided[0]["subject"] == "A"

    refusing = Harness(harness.repo, conductor_home, {**ALLOW_ALL, "kinds": ["run"]})
    run, status = refusing.run(workflow(package("B", [write_step("b.txt", "b")], tier="T3", reviewer=reviewer)))
    assert status == "failed" and states_of(run) == {"B": "stopped"}
    assert "the policy does not approve land requests" in run.load()["packages"]["B"]["error"]


def test_a_failed_recheck_on_the_landing_commit_is_rework(harness):
    # Passes in the package worktree, fails in the integration worktree: only the re-check can catch it.
    only_outside = {
        "argv": ["{python}", "-c", "import os, sys; sys.exit('@integration' in os.getcwd())"],
        "timeout_seconds": 60,
    }
    run, status = harness.run(workflow(package("A", [write_step("a.txt", "a")], checks=[only_outside], max_attempts=1)))
    assert status == "failed" and run.load()["packages"]["A"]["failure"] == "recheck_failure"
    rechecked = events_of(run, "merge.rechecked")
    assert rechecked and not rechecked[0]["passed"]
    assert first_parent_log(harness.repo) == ["Initial commit"]


def test_a_parallel_conflict_goes_back_as_rework_from_the_new_tip(harness):
    slow = {"argv": ["{python}", "-c", "import time; time.sleep(0.8)"], "timeout_seconds": 60}
    run, status = harness.run(
        workflow(
            package("A", [{"append": "README.md", "text": "from A\n"}], files=["README.md"], checks=[PASS, slow]),
            package(
                "C",
                [{"sleep": 0.3, "attempts": [1]}, {"append": "README.md", "text": "from C\n"}],
                files=["README.md"],
                max_attempts=3,
            ),
        )
    )
    packages = run.load()["packages"]
    # Whichever lands second no longer applies to main; which one that is depends on timing.
    winner, loser = sorted(packages, key=lambda pid: packages[pid]["attempts"])
    assert status == "completed" and packages[winner]["attempts"] == 1 and packages[loser]["attempts"] == 2
    assert packages[loser]["failure"] == "merge_conflict"
    readme = git(harness.repo, "show", "main:README.md")
    assert readme == f"# fixture\nfrom {winner}\nfrom {loser}"
    preserved = next(
        e for e in events_of(run, "worktree.provisioned") if e["package_id"] == loser and e["preserved_branch"]
    )
    assert preserved["preserved_branch"] == f"conductor/{loser}@attempt-1"


def test_stop_holds_the_run_and_resume_continues_the_same_attempt(harness):
    data = workflow(
        package(
            "A", [write_step("one.txt", "1"), {"sleep": 1.0}, write_step("two.txt", "2")], files=["one.txt", "two.txt"]
        )
    )
    run = harness.create(data)

    def stop_on_first_step(event: dict) -> None:
        if event["kind"] == "agent.progress" and event["message"] == "wrote one.txt":
            run.request_stop()

    assert harness.drive(run, on_event=stop_on_first_step) == "held"
    entry = run.load()["packages"]["A"]
    assert entry["state"] == "held" and entry["held_from"] == "running" and entry["attempts"] == 1
    worktree = os.path.join(harness.repo, ".conductor", "worktrees", "A")
    assert os.path.exists(os.path.join(worktree, "one.txt")) and not os.path.exists(os.path.join(worktree, "two.txt"))
    assert not run.stop_requested()

    scheduler.check_resumable(run)
    assert harness.drive(run) == "completed"
    entry = run.load()["packages"]["A"]
    implements = [r for r in entry["runs"] if r["stage"] == "implement"]
    assert entry["attempts"] == 1 and [(r["attempt"], r["continuation"]) for r in implements] == [(1, False), (1, True)]
    assert implements[0]["outcome"] == "cancelled"
    progress = [e["message"] for e in events_of(run, "agent.progress")]
    assert progress == ["wrote one.txt", "kept one.txt", "wrote two.txt"]
    assert [e["kind"] for e in events_of(run) if e["kind"].startswith("run.")] == [
        "run.started",
        "run.stopping",
        "run.held",
        "run.resumed",
        "run.finished",
    ]


def test_a_run_that_died_resumes_as_uncertain_and_continues(harness):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    state = run.load()
    state["status"] = "running"
    state["packages"]["A"].update(state="running", attempts=1, stage="implement")
    run.save(state)  # what a killed process leaves behind
    assert harness.drive(run) == "completed"
    moves = [(e["from"], e["to"]) for e in events_of(run, "package.state_changed")][:3]
    assert moves == [("running", "uncertain"), ("uncertain", "ready"), ("ready", "running")]
    assert run.load()["packages"]["A"]["attempts"] == 1


def test_a_held_landing_waits_for_the_checkout_and_resumes(harness):
    data = workflow(package("A", [write_step("a.txt", "the package's")]))
    with open(os.path.join(harness.repo, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("the operator's untracked file")
    run, status = harness.run(data)
    assert status == "held" and states_of(run) == {"A": "merge_held"}
    assert "untracked" in run.load()["packages"]["A"]["error"]
    os.remove(os.path.join(harness.repo, "a.txt"))
    assert harness.drive(run) == "completed"
    assert git(harness.repo, "show", "main:a.txt") == "the package's"


def test_a_held_landing_retries_when_the_checkout_changes(harness):
    data = workflow(package("A", [write_step("a.txt", "a")]))
    with open(os.path.join(harness.repo, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("in the way")
    run = harness.create(data)

    def clear_when_held(event: dict) -> None:
        if event["kind"] == "merge.held":

            def later():
                os.remove(os.path.join(harness.repo, "a.txt"))
                with open(os.path.join(harness.repo, "touch.txt"), "w", encoding="utf-8") as handle:
                    handle.write("x")
                git(harness.repo, "add", "touch.txt")  # the index changes: the fingerprint moves
                git(harness.repo, "commit", "-q", "-m", "operator commits")

            threading.Timer(0.2, later).start()

    assert harness.drive(run, on_event=clear_when_held, hold_wait=30) == "completed"


def test_a_landing_that_keeps_erroring_backs_off_holds_and_resumes(harness, monkeypatch):
    original, calls = mq.land, []

    def broken(*args, **kwargs):
        calls.append(time.monotonic())
        raise WorktreeError("simulated: the branch's ref lock is held")

    monkeypatch.setattr(mq, "land", broken)
    monkeypatch.setattr(scheduler.Conductor, "landing_backoff", 0.25)
    started = time.monotonic()
    run, status = harness.run(workflow(package("A", [write_step("a.txt", "a")])))
    assert status == "held" and time.monotonic() - started < 60
    entry = run.load()["packages"]["A"]
    assert entry["state"] == "held" and entry["stage"] == "land" and entry["landing"]["errors"] == 3
    assert len(calls) == 3 and entry["error"].startswith("the landing step failed (3 of 3)")
    gaps = [later - earlier for earlier, later in zip(calls, calls[1:], strict=False)]
    assert gaps[0] >= 0.25 and gaps[1] >= 0.5  # it backed off, doubling each time
    assert len([e for e in events_of(run, "merge.held")]) == 3
    assert run.load()["status"] == "held" and first_parent_log(harness.repo) == ["Initial commit"]

    monkeypatch.setattr(mq, "land", original)
    assert harness.drive(run) == "completed"  # Resume gives the landing fresh tries, and it lands
    assert run.load()["packages"]["A"]["landing"]["errors"] == 0
    assert git(harness.repo, "show", "main:a.txt") == "a"


def test_a_repository_outside_the_roots_is_refused(repo, conductor_home, tmp_path):
    grants = permissions.GrantStore(conductor_home, static_roots=[str(tmp_path / "elsewhere")])
    path = write_workflow(tmp_path, workflow(package("A", [write_step("a.txt", "a")])))
    with pytest.raises(PermissionRefused, match="outside conductor's dispatch roots"):
        scheduler.create_run(
            repo,
            path,
            approver=PolicyApprover(ALLOW_ALL),
            grants=grants,
            registry=permissions.ProjectRegistry(conductor_home),
        )


def test_an_unapproved_plan_never_starts(harness, conductor_home):
    harness.approver = PolicyApprover({**ALLOW_ALL, "commands": []})
    with pytest.raises(PermissionRefused, match="check command"):
        harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    assert store.runs(harness.repo) == []


def test_a_changed_workflow_does_not_resume(harness):
    data = workflow(package("A", [write_step("a.txt", "a"), {"sleep": 1.0}], files=["a.txt"]))
    run = harness.create(data)
    run.request_stop()  # a stop left from an earlier process is cleared when a driver starts
    assert harness.drive(run, on_event=lambda e: e["kind"] == "agent.progress" and run.request_stop()) == "held"
    data["packages"][0]["acceptance"]["checks"] = [FAIL]
    write_workflow(os.path.dirname(harness.repo), data)
    with pytest.raises(PermissionRefused, match="changed after run"):
        scheduler.check_resumable(run)


def test_cancel_stops_every_package_and_removes_its_worktree(harness):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a"), {"sleep": 2.0}])))

    def stop_once_written(event: dict) -> None:
        if event["kind"] == "agent.progress":
            run.request_stop()

    assert harness.drive(run, on_event=stop_once_written) == "held"
    state = scheduler.cancel_run(run)
    assert state["status"] == "cancelled" and states_of(run) == {"A": "stopped"}
    assert not os.path.exists(os.path.join(harness.repo, ".conductor", "worktrees", "A"))
    assert git(harness.repo, "rev-parse", "--verify", "conductor/A")
    with pytest.raises(store.RunError, match="nothing to resume"):
        scheduler.check_resumable(run)


def test_one_process_per_repository_across_runs(harness):
    slow = workflow(package("A", [write_step("a.txt", "a"), {"sleep": 2.0}], files=["a.txt"]))
    first = harness.create(slow)
    second = harness.create(workflow(package("B", [write_step("b.txt", "b")])), "second.json")
    busy = threading.Event()
    outcome = {}

    def drive_first() -> None:
        outcome["first"] = harness.drive(first, on_event=lambda e: e["kind"] == "agent.progress" and busy.set())

    driver = threading.Thread(target=drive_first)
    driver.start()
    assert busy.wait(60)
    try:
        with pytest.raises(store.RepositoryBusy, match=rf"run {first.run_id}, process {os.getpid()}"):
            harness.drive(second)
        with pytest.raises(store.RepositoryBusy):
            harness.create(workflow(package("C", [write_step("c.txt", "c")])))
        with pytest.raises(store.RepositoryBusy):
            scheduler.cancel_run(second)
        assert second.load()["status"] == "ready"  # refused before it started
    finally:
        driver.join(120)
    assert outcome["first"] == "completed"
    assert harness.drive(second) == "completed"
    assert sorted(first_parent_log(harness.repo)[:2]) == ["A: Package A", "B: Package B"]
    assert not os.path.exists(os.path.join(harness.repo, ".conductor", "conductor.lock.owner"))


def test_one_run_one_driver(harness):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    with run.lock():
        assert run.busy()
        with pytest.raises(store.RunBusy):
            harness.drive(run)
    assert not run.busy()
