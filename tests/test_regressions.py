"""One test per defect an independent review found, each named after what broke. Each failed before its fix."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conductor import agents, checks, cli, permissions, scheduler, store
from conductor import merge_queue as mq
from conductor import worktrees as wt
from conductor.agents.base import AgentResult
from conductor.events import EventLog, read_events
from conductor.worktrees import WorktreeError

from .conftest import ALLOW_ALL, PASS, Harness, events_of, git, make_repo, package, symlinks_available, workflow

pytestmark = pytest.mark.integration
FAKES = Path(__file__).resolve().parent / "fakes"


def write_step(name: str, text: str, **extra) -> dict:
    return {"write": name, "content": text, **extra}


def states_of(run) -> dict:
    return {pid: entry["state"] for pid, entry in run.load()["packages"].items()}


def commit_file(repo: str, name: str, text: str) -> str:
    Path(repo, name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


# ── worktrees ────────────────────────────────────────────────────────────


def test_a_package_branch_cannot_be_the_integration_branch(repo):
    git(repo, "branch", "conductor/A")
    with pytest.raises(WorktreeError, match="cannot be the integration branch"):
        wt.provision(repo, "A", "conductor/A")
    assert not os.path.exists(wt.worktree_path(repo, "A"))


def test_a_linked_worktrees_directory_is_never_deleted_through(repo, tmp_path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks need a privilege this machine does not grant")
    target = Path(repo, "user-data", "A")
    target.mkdir(parents=True)
    (target / "precious.txt").write_text("keep", encoding="utf-8")
    Path(repo, ".conductor").mkdir()
    os.symlink(Path(repo, "user-data"), Path(repo, ".conductor", "worktrees"), target_is_directory=True)
    with pytest.raises(WorktreeError, match="is a link"):
        wt.remove(os.path.join(repo, ".conductor", "worktrees", "A"))
    with pytest.raises(WorktreeError, match="is a link"):
        wt.provision(repo, "B", "main")  # nothing is provisioned (or set up) through the link either
    with pytest.raises(WorktreeError, match="is a link"):
        mq.ensure(repo, "main")
    assert (target / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_setup_is_stopped_by_a_cancel(repo):
    manifest = {
        "link": [],
        "setup": [{"argv": ["{python}", "-c", "import time; time.sleep(60)"], "timeout_seconds": 120}],
        "env": [],
    }
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(wt.SetupCancelled):
        wt.provision(repo, "A", "main", manifest, cancel=cancel)
    assert time.monotonic() - started < 20 and not os.path.exists(wt.worktree_path(repo, "A"))


def test_another_runs_worktree_is_recreated_not_reused(repo):
    first = wt.provision(repo, "A", "main", run_id="run-1")
    Path(first["path"], "partial.txt").write_text("run 1's work", encoding="utf-8")
    assert not wt.provision(repo, "A", "main", run_id="run-1")["created"]
    second = wt.provision(repo, "A", "main", run_id="run-2")
    assert second["created"] and not Path(second["path"], "partial.txt").exists()
    assert wt.worktree_owner(repo, "A") == "run-2"


def test_worktree_paths_are_read_exactly(tmp_path, monkeypatch):
    """Controlled porcelain must preserve newlines and whitespace, even on filesystems that refuse those names."""
    main = str(tmp_path / "répo with space")
    linked = str(tmp_path / " leading\nbranch decoy trailing ")
    raw = f"worktree {main}\0HEAD abc\0branch refs/heads/main\0\0worktree {linked}\0HEAD def\0detached\0\0"

    def porcelain(root, args, **kwargs):
        assert args[:3] == ["worktree", "list", "--porcelain"]
        return subprocess.CompletedProcess(args, 0, os.fsencode(raw), b"")

    monkeypatch.setattr(wt, "_git", porcelain)
    assert wt.registered_worktrees(main) == {
        os.path.normcase(os.path.realpath(main)): "main",
        os.path.normcase(os.path.realpath(linked)): "",
    }


# ── the merge queue ──────────────────────────────────────────────────────


def test_a_branch_that_moved_after_verification_is_not_built(repo):
    path = wt.provision(repo, "A", "main")["path"]
    Path(path, "a.txt").write_text("verified", encoding="utf-8")
    verified = wt.commit(path, "implement", "run-1:A:implement:1", "A")["commit_sha"]
    Path(path, "b.txt").write_text("nobody checked this", encoding="utf-8")
    moved = wt.commit(path, "implement", "run-1:A:implement:2", "A")["commit_sha"]
    built = mq.build(repo, "main", "A", "run-1", "A", verified_sha=verified)
    assert built["moved"] and built["package_sha"] == moved and "landing_sha" not in built


def test_deleting_the_file_in_the_way_changes_the_fingerprint(repo):
    Path(repo, "a.txt").write_text("in the way", encoding="utf-8")
    before = mq.checkout_fingerprint(repo, ["a.txt"], "main")
    assert mq.checkout_fingerprint(repo, ["a.txt"], "main") == before
    os.remove(os.path.join(repo, "a.txt"))
    assert mq.checkout_fingerprint(repo, ["a.txt"], "main") != before


# ── the event log ────────────────────────────────────────────────────────


def test_reopening_a_torn_log_never_reuses_a_sequence_number(tmp_path):
    path = tmp_path / "events.ndjson"
    path.write_text(json.dumps({"seq": 1, "ts": 0, "kind": "x"}) + "\n" + '{"seq": 2, "ki', encoding="utf-8")
    for _ in range(2):
        log = EventLog(str(path))
        log.emit("merge.stale", package_id="A", head_sha="h")
        log.emit("merge.stale", package_id="A", head_sha="h")
        log.close()
    sequence = [event["seq"] for event in read_events(str(path))]
    assert sequence == [1, 2, 3, 4, 5]


# ── grants ───────────────────────────────────────────────────────────────


def test_two_processes_granting_at_once_keep_both_grants(tmp_path, conductor_home):
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first = permissions.GrantStore(conductor_home, static_roots=[])
    second = permissions.GrantStore(conductor_home, static_roots=[])  # loaded before the first one grants
    first.grant(os.path.realpath(first_root), record=True)
    second.grant(os.path.realpath(second_root), record=True)
    with open(os.path.join(conductor_home, permissions.GRANTS_FILE), encoding="utf-8") as handle:
        recorded = json.load(handle)["roots"]
    assert sorted(recorded) == sorted([os.path.realpath(first_root), os.path.realpath(second_root)])
    assert not [name for name in os.listdir(conductor_home) if name.endswith(".partial")]


# ── the leak check ───────────────────────────────────────────────────────


def leak_check():
    spec = importlib.util.spec_from_file_location("leak_check", Path(__file__).parents[1] / "scripts/leak_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_leak_check_allows_only_github_noreply_identities():
    check = leak_check()
    assert check.identity_findings("someone", "1+someone@users.noreply.github.com", set()) == []
    assert check.identity_findings("dependabot[bot]", "1+dependabot[bot]@users.noreply.github.com", set()) == []
    assert check.identity_findings("GitHub", "noreply@github.com", set()) == []
    assert check.identity_findings("Some Person", "person@mail.example", set()) == [
        "an email that is not a GitHub noreply address"
    ]
    assert check.identity_findings("Real Name", "1+someone@users.noreply.github.com", set()) == [
        "a name that is not the noreply address's login"
    ]


# ── the command line ─────────────────────────────────────────────────────


def policy(directory) -> str:
    path = Path(directory, "policy.toml")
    path.write_text(
        '[policy]\nkinds = ["run", "land", "grant"]\nadapters = ["scripted"]\ncommands = ["{python}"]\n'
        f'max_tier = "T3"\nroots = [{json.dumps(str(Path(directory).parent))}]\n',
        encoding="utf-8",
    )
    return str(path)


def test_a_policy_inside_any_registered_project_or_the_granted_repo_is_refused(tmp_path, capsys):
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")
    permissions.ProjectRegistry().add(two)
    assert cli.main(["grant", one, "--policy", policy(one)]) == 1
    assert "where agents work" in capsys.readouterr().err
    os.environ["CONDUCTOR_DISPATCH_ROOTS"] = str(tmp_path)
    try:
        path = Path(tmp_path, "workflow.json")
        path.write_text(json.dumps(workflow(package("A", [write_step("a.txt", "a")]))), encoding="utf-8")
        assert cli.main(["--repo", one, "run", str(path), "--policy", policy(two)]) == 1
        assert "where agents work" in capsys.readouterr().err
    finally:
        del os.environ["CONDUCTOR_DISPATCH_ROOTS"]


# ── the scheduler ────────────────────────────────────────────────────────


def test_unverified_commits_on_the_package_branch_never_land(harness):
    data = workflow(package("A", [write_step("a.txt", "verified")], max_attempts=1))
    run = harness.create(data)

    def sneak_in(event: dict) -> None:
        if event["kind"] == "merge.queued":
            worktree = wt.worktree_path(harness.repo, "A")
            Path(worktree, "a.txt").write_text("never checked", encoding="utf-8")
            git(worktree, "commit", "-q", "-am", "after the checks")

    assert harness.drive(run, on_event=sneak_in) == "failed"
    entry = run.load()["packages"]["A"]
    assert entry["failure"] == "branch_moved" and "nothing unverified lands" in entry["error"]
    assert git(harness.repo, "log", "--format=%s", "main").splitlines() == ["Initial commit"]


def test_a_package_belongs_to_one_unfinished_run(harness):
    data = workflow(package("A", [write_step("a.txt", "a"), {"sleep": 2.0}], files=["a.txt"]))
    first = harness.create(data)
    assert harness.drive(first, on_event=lambda e: e["kind"] == "agent.progress" and first.request_stop()) == "held"
    with pytest.raises(scheduler.SchedulerError, match=f"belongs to run {first.run_id}"):
        harness.create(workflow(package("a", [write_step("a.txt", "b")])), "second.json")
    worktree = wt.worktree_path(harness.repo, "A")
    assert Path(worktree, "a.txt").read_text(encoding="utf-8") == "a"
    assert harness.drive(first) == "completed"


def test_cancel_leaves_another_runs_worktree_alone(harness):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    other = wt.provision(harness.repo, "A", "main", run_id="some-other-run")
    Path(other["path"], "theirs.txt").write_text("not ours", encoding="utf-8")
    scheduler.cancel_run(run)
    assert Path(other["path"], "theirs.txt").exists()


def test_a_queue_behind_a_held_landing_settles(harness):
    Path(harness.repo, "a.txt").write_text("in the way", encoding="utf-8")
    data = workflow(
        package("A", [write_step("a.txt", "a")]),
        package("B", [{"sleep": 1.0}, write_step("b.txt", "b")]),
    )
    run = harness.create(data)
    box = {}
    driver = threading.Thread(target=lambda: box.setdefault("status", harness.drive(run)), daemon=True)
    driver.start()
    driver.join(90)
    assert box.get("status") == "held"
    assert states_of(run) == {"A": "merge_held", "B": "queued_for_merge"}
    os.remove(os.path.join(harness.repo, "a.txt"))
    assert harness.drive(run) == "completed"


@pytest.mark.parametrize("left", ["failed", "uncertain"])
def test_a_package_a_crash_left_failed_or_uncertain_resumes(harness, left):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")], max_attempts=2)))
    state = run.load()
    state["status"] = "running"
    state["packages"]["A"].update(state=left, attempts=1, stage="implement")
    run.save(state)
    assert harness.drive(run) == "completed"


def test_a_crash_before_provisioning_still_starts_the_rework_from_the_tip(harness, monkeypatch):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")], max_attempts=3)))
    old = wt.provision(harness.repo, "A", "main", run_id=run.run_id)
    Path(old["path"], "stale.txt").write_text("the conflicted attempt", encoding="utf-8")
    wt.commit(old["path"], "implement", f"{run.run_id}:A:implement:1", "A")
    state = run.load()
    state["status"] = "running"
    state["packages"]["A"].update(state="failed", attempts=1, stage="implement", fresh=True, new_attempt=True)
    run.save(state)
    original = wt.provision

    def die_before_provision(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(wt, "provision", die_before_provision)
    with pytest.raises(SimulatedCrash):
        harness.drive(run)
    saved = run.load()["packages"]["A"]
    assert saved["state"] == "running" and saved["attempts"] == 2 and saved["fresh"]
    monkeypatch.setattr(wt, "provision", original)
    assert harness.drive(run) == "completed"
    provisioned = [e for e in events_of(run, "worktree.provisioned") if e["package_id"] == "A"]
    assert provisioned[0]["created"] and provisioned[0]["preserved_branch"] == "conductor/A@attempt-1"
    assert "stale.txt" not in git(harness.repo, "ls-tree", "-r", "--name-only", "main")


def test_an_engine_built_before_a_cancel_does_not_drive_it(harness):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    engine = scheduler.Conductor(run, approver=harness.approver, grants=harness.grants, poll=0.02)
    scheduler.cancel_run(run)
    with pytest.raises(store.RunError, match="nothing to resume"):
        engine.run_until_settled()
    assert run.load()["status"] == "cancelled"


def test_deleting_the_untracked_file_in_the_way_lets_the_landing_retry(harness):
    Path(harness.repo, "a.txt").write_text("in the way", encoding="utf-8")
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))

    def clear_when_held(event: dict) -> None:
        if event["kind"] == "merge.held":
            threading.Timer(0.3, os.remove, args=(os.path.join(harness.repo, "a.txt"),)).start()

    assert harness.drive(run, on_event=clear_when_held, hold_wait=30) == "completed"
    assert git(harness.repo, "log", "-1", "--format=%s", "main") == "A: Package A"


def test_stop_reaches_a_setup_command(harness):
    data = workflow(
        package("A", [write_step("a.txt", "a")]),
        environment={"setup": [{"argv": ["{python}", "-c", "import time; time.sleep(60)"], "timeout_seconds": 120}]},
    )
    run = harness.create(data)
    threading.Timer(1.5, run.request_stop).start()
    started = time.monotonic()
    assert harness.drive(run) == "held"
    assert time.monotonic() - started < 30
    assert run.load()["packages"]["A"]["attempts"] == 1


def test_a_stage_discarded_for_its_scope_keeps_its_commit_and_patch(harness):
    run, status = harness.run(
        workflow(package("A", [write_step("a.txt", "a"), write_step("outside.txt", "x", attempts=[1])]))
    )
    assert status == "completed"
    discarded = [e for e in events_of(run, "receipt.recorded") if e["receipt"] == "discarded"]
    assert len(discarded) == 1
    receipt = json.loads(Path(run.receipts_path, *discarded[0]["path"].split("/")).read_text(encoding="utf-8"))
    assert receipt["outside"] == ["outside.txt"] and "outside.txt" in receipt["patch"]
    assert git(harness.repo, "rev-parse", receipt["ref"]) == receipt["commit_sha"]
    assert "outside.txt" in git(harness.repo, "ls-tree", "-r", "--name-only", receipt["commit_sha"])


def test_a_resumed_review_continues_its_own_session(repo, conductor_home, tmp_path, monkeypatch):
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    harness = Harness(repo, conductor_home, {**ALLOW_ALL, "adapters": ["claude_code", "codex_cli"], "max_tier": "T2"})
    data = workflow(
        {
            "id": "A",
            "title": "Write a",
            "instruction": "write a.txt reviewed\nsleep 2\nverdict approve",
            "files": ["a.txt"],
            "verification_tier": "T2",
            "agent": {"adapter": "claude_code", "command": [sys.executable, str(FAKES / "fake_claude.py")]},
            "reviewer": {
                "adapter": "codex_cli",
                "command": [sys.executable, str(FAKES / "fake_codex.py")],
                "env_allow": ["FAKE_LOG"],
            },
            "acceptance": {"checks": [PASS]},
        }
    )
    run = harness.create(data)

    def stop_during_review(event: dict) -> None:
        if event["kind"] == "stage.started" and event["stage"] == "review":
            # The review stage is recorded before the checks run; stop only once the reviewer process has logged.
            def stop_once_the_reviewer_runs() -> None:
                deadline = time.monotonic() + 30
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                run.request_stop()

            threading.Thread(target=stop_once_the_reviewer_runs, daemon=True).start()

    assert harness.drive(run, on_event=stop_during_review) == "held"
    session = run.load()["packages"]["A"]["review_session_id"]
    assert session
    assert harness.drive(run) == "completed"
    calls = [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[-1][:3] == ["exec", "resume", session]


class SimulatedCrash(BaseException):
    """Leave the scheduler's last saved state intact, as a process death would, without killing pytest."""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("before", [False, True])
def test_hidden_index_flags_cannot_certify_different_contents(repo, flag, before):
    committed = commit_file(repo, "a.txt", "bad")
    hide = {"argv": ["git", "update-index", flag, "a.txt"], "timeout_seconds": 10}
    rewrite = {"argv": ["{python}", "-c", "open('a.txt','w').write('good')"], "timeout_seconds": 10}
    inspect = {"argv": ["{python}", "-c", "assert open('a.txt').read() == 'good'"], "timeout_seconds": 10}
    if before:
        git(repo, "update-index", flag, "a.txt")
        Path(repo, "a.txt").write_text("good", encoding="utf-8")
    contract = [inspect] if before else [hide, rewrite, inspect]
    receipt = checks.run_checks("flags", repo, contract)
    assert not receipt["passed"]
    assert "index flags" in checks.bound(receipt, "flags", repo, contract, committed)
    assert len(receipt["checks"]) == (0 if before else 1)
    assert not wt.is_clean(repo)
    assert git(repo, "show", "HEAD:a.txt") == "bad"


def test_resumed_verification_refuses_a_worktree_moved_through_a_link(harness, tmp_path, monkeypatch):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks need a privilege this machine does not grant")
    harness.grants = permissions.GrantStore(harness.home, static_roots=[harness.repo])
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")], max_attempts=1)))
    original = checks.run_checks

    def stop_checks(*args, **kwargs):
        receipt = original(*args, **kwargs)
        receipt["cancelled"] = True
        run.request_stop()
        return receipt

    monkeypatch.setattr(checks, "run_checks", stop_checks)
    assert harness.drive(run) == "held"
    path = Path(wt.worktree_path(harness.repo, "A"))
    outside = tmp_path / "ungranted"
    outside.mkdir()
    git(harness.repo, "worktree", "move", str(path), str(outside / "A"))
    path.parent.rmdir()
    os.symlink(outside, path.parent, target_is_directory=True)
    calls = []

    def unexpected_check(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(checks, "run_checks", unexpected_check)
    assert harness.drive(run) == "failed"
    assert calls == []
    assert "link" in run.load()["packages"]["A"]["error"]
    for operation in (lambda: wt.refresh_base(str(path), "main"), lambda: wt.reset(str(path), wt.head(harness.repo))):
        with pytest.raises(wt.WorktreeError, match="link"):
            operation()
    assert (outside / "A" / "a.txt").read_text(encoding="utf-8") == "a"


@pytest.mark.parametrize("crash,name", [(False, "outside.txt"), (True, "a.txt")])
def test_a_stopped_review_cannot_supply_the_next_verification_head(harness, monkeypatch, crash, name):
    data = workflow(
        package("A", [write_step("a.txt", "implemented")], tier="T1", reviewer={"adapter": "scripted", "steps": []})
    )
    run = harness.create(data)
    original = agents.create
    unexpected = []

    class Reviewer:
        def run(self, task):
            if not unexpected:
                unexpected.append(commit_file(task.worktree, name, "reviewer commit"))
                if crash:
                    raise SimulatedCrash()
                run.request_stop()
                assert task.cancel.wait(10)
                return AgentResult("cancelled")
            return AgentResult("done", verdict="approve")

    monkeypatch.setattr(
        agents,
        "create",
        lambda spec: Reviewer() if spec == run.load()["plan"]["packages"][0]["reviewer"] else original(spec),
    )
    if crash:
        with pytest.raises(SimulatedCrash):
            harness.drive(run)
    else:
        assert harness.drive(run) == "held"
    assert harness.drive(run) == "completed"
    assert git(harness.repo, "show", "main:a.txt") == "implemented"
    assert "outside.txt" not in git(harness.repo, "ls-tree", "-r", "--name-only", "main")
    assert run.load()["packages"]["A"]["attempts"] == 2
    discarded = [e for e in events_of(run, "receipt.recorded") if e["receipt"] == "discarded"]
    assert len(discarded) == 1
    receipt = json.loads(Path(run.receipts_path, discarded[0]["path"]).read_text(encoding="utf-8"))
    assert git(harness.repo, "rev-parse", receipt["ref"]) == unexpected[0]
    assert "reviewer commit" in receipt["patch"]


def test_a_crash_after_fresh_provisioning_keeps_this_attempts_partial_work(harness, monkeypatch):
    run = harness.create(
        workflow(package("A", [write_step("a.txt", "a")], files=["a.txt", "partial.txt"], max_attempts=3))
    )
    old = wt.provision(harness.repo, "A", "main", run_id=run.run_id)
    commit_file(old["path"], "stale.txt", "conflicted attempt")
    state = run.load()
    state["status"] = "running"
    state["packages"]["A"].update(state="failed", attempts=1, fresh=True, new_attempt=True)
    run.save(state)
    original = agents.create

    class InterruptedAgent:
        def run(self, task):
            Path(task.worktree, "partial.txt").write_text("keep this attempt", encoding="utf-8")
            raise SimulatedCrash()

    monkeypatch.setattr(agents, "create", lambda spec: InterruptedAgent())
    with pytest.raises(SimulatedCrash):
        harness.drive(run)
    assert run.load()["packages"]["A"]["fresh"]
    monkeypatch.setattr(agents, "create", original)
    assert harness.drive(run) == "completed"
    assert git(harness.repo, "show", "main:partial.txt") == "keep this attempt"
    assert "stale.txt" not in git(harness.repo, "ls-tree", "-r", "--name-only", "main")
    provisioned = events_of(run, "worktree.provisioned")
    assert [event["created"] for event in provisioned] == [True, False]
    assert run.load()["packages"]["A"]["attempts"] == 2


@pytest.mark.parametrize("stage", ["verify", "recheck"])
def test_stopping_a_check_restores_its_clean_start_without_spending_the_attempt(harness, tmp_path, monkeypatch, stage):
    marker = tmp_path / "check-started"
    script = (
        "from pathlib import Path; import sys,time; p=Path('scratch.txt'); p.write_text('temporary'); "
        "m=Path(sys.argv[1]); first=not m.exists(); "
        "selected=Path.cwd().name.startswith('@integration') == (sys.argv[2]=='recheck'); "
        "m.touch() if selected else None; time.sleep(60 if first and selected else 0); p.unlink()"
    )
    check = {"argv": ["{python}", "-c", script, str(marker), stage], "timeout_seconds": 90}
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")], checks=[check], max_attempts=1)))
    original = checks.run_checks
    stopped = []

    def with_stop(key, path, *args, **kwargs):
        selected = Path(path).name.startswith("@integration") == (stage == "recheck")
        watcher = None
        if selected and not stopped:
            stopped.append(path)

            def stop_after_write():
                deadline = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                run.request_stop()

            watcher = threading.Thread(target=stop_after_write, daemon=True)
            watcher.start()
        receipt = original(key, path, *args, **kwargs)
        if watcher:
            watcher.join(15)
        return receipt

    monkeypatch.setattr(checks, "run_checks", with_stop)
    assert harness.drive(run) == "held"
    assert not Path(stopped[0], "scratch.txt").exists()
    assert wt.is_clean(stopped[0])
    kind = "checks" if stage == "verify" else "recheck"
    entry = next(e for e in events_of(run, "receipt.recorded") if e["receipt"] == kind)
    evidence = json.loads(Path(run.receipts_path, entry["path"]).read_text(encoding="utf-8"))
    evidence = evidence if kind == "checks" else evidence["receipt"]
    assert evidence["cleanup"]["untracked"] == ["scratch.txt"]
    assert harness.drive(run) == "completed"
    assert run.load()["packages"]["A"]["attempts"] == 1


def test_restore_reads_projects_after_acquiring_the_records_lock(tmp_path, conductor_home, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = permissions.ProjectRegistry(conductor_home)
    registry.add(str(first))
    permissions.GrantStore(conductor_home).grant(str(first), record=True)
    original = permissions._records_locked
    interleaved = []

    @contextmanager
    def register_before_lock(home, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            # A separate process finishes registration and grant exactly before A takes the restore lock.
            program = (
                "import sys; from conductor.permissions import ProjectRegistry, GrantStore; "
                "ProjectRegistry(sys.argv[1]).add(sys.argv[2]); "
                "GrantStore(sys.argv[1]).grant(sys.argv[2], record=True)"
            )
            subprocess.run(
                [sys.executable, "-c", program, conductor_home, str(second)],
                check=True,
                capture_output=True,
                timeout=20,
                env=wt.git_environment(),
            )
        with original(home, *args, **kwargs):
            yield

    monkeypatch.setattr(permissions, "_records_locked", register_before_lock)
    restored, _ = cli._permissions()
    assert sorted(restored.recorded()) == sorted([os.path.realpath(first), os.path.realpath(second)])
    record = json.loads(Path(conductor_home, permissions.GRANTS_FILE).read_text(encoding="utf-8"))
    assert sorted(record["roots"]) == sorted(restored.recorded())


def test_scope_cleanup_survives_a_crash_after_the_implement_commit(harness, monkeypatch):
    run = harness.create(
        workflow(package("A", [write_step("a.txt", "a"), write_step("outside.txt", "rejected", attempts=[1])]))
    )
    original = wt.commit
    rejected = []

    def commit_then_die(*args, **kwargs):
        result = original(*args, **kwargs)
        rejected.append(result["head_sha"])
        raise SimulatedCrash()

    monkeypatch.setattr(wt, "commit", commit_then_die)
    with pytest.raises(SimulatedCrash):
        harness.drive(run)
    monkeypatch.setattr(wt, "commit", original)
    assert harness.drive(run) == "completed"
    discarded = [e for e in events_of(run, "receipt.recorded") if e["receipt"] == "discarded"]
    assert len(discarded) == 1
    receipt = json.loads(Path(run.receipts_path, discarded[0]["path"]).read_text(encoding="utf-8"))
    assert receipt["outside"] == ["outside.txt"] and "rejected" in receipt["patch"]
    assert git(harness.repo, "rev-parse", receipt["ref"]) == rejected[0]
    assert "outside.txt" not in git(harness.repo, "ls-tree", "-r", "--name-only", "main")


def test_a_stopped_claude_review_keeps_its_session_without_output(harness, tmp_path, monkeypatch):
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    harness.approver = permissions.PolicyApprover({**ALLOW_ALL, "adapters": ["scripted", "claude_code"]})
    spec = package(
        "A",
        [write_step("a.txt", "a")],
        tier="T1",
        reviewer={
            "adapter": "claude_code",
            "command": [sys.executable, str(FAKES / "fake_claude.py")],
            "env_allow": ["FAKE_LOG"],
        },
    )
    spec["instruction"] = "sleep 0.8\nverdict approve"
    run = harness.create(workflow(spec))
    original_checks = checks.run_checks

    def stop_before_review(*args, **kwargs):
        receipt = original_checks(*args, **kwargs)
        receipt["cancelled"] = True
        run.request_stop()
        return receipt

    monkeypatch.setattr(checks, "run_checks", stop_before_review)
    assert harness.drive(run) == "held"
    assert not log.exists()
    monkeypatch.setattr(checks, "run_checks", original_checks)

    def stop_review(event):
        if event["kind"] == "agent.progress" and ":review:" in event["key"]:
            threading.Timer(0.15, run.request_stop).start()

    assert harness.drive(run, on_event=stop_review) == "held"
    session = run.load()["packages"]["A"].get("review_session_id")
    assert session
    # Another result with no id must leave the already known id in the saved state.
    original = agents.create

    class NoOutput:
        def run(self, task):
            run.request_stop()
            assert task.cancel.wait(10)
            return AgentResult("cancelled", session_id=None)

    monkeypatch.setattr(agents, "create", lambda spec: NoOutput())
    assert harness.drive(run) == "held"
    assert run.load()["packages"]["A"]["review_session_id"] == session
    monkeypatch.setattr(agents, "create", original)
    assert harness.drive(run) == "completed"
    calls = [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[0][calls[0].index("--session-id") + 1] == session
    assert calls[-1][calls[-1].index("--resume") + 1] == session


def test_a_claude_stage_stopped_before_its_session_was_saved_keeps_its_attempt(harness, tmp_path, monkeypatch):
    log, sessions = tmp_path / "claude.log", tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SESSIONS", str(sessions))
    harness.approver = permissions.PolicyApprover({**ALLOW_ALL, "adapters": ["scripted", "claude_code"]})
    spec = package("A", [], max_attempts=1)
    spec["agent"] = {
        "adapter": "claude_code",
        "command": [sys.executable, str(FAKES / "fake_claude.py")],
        "env_allow": ["FAKE_LOG", "FAKE_SESSIONS"],
    }
    spec["instruction"] = "write a.txt a\nsleep 2"
    run = harness.create(workflow(spec))

    def stop_once_claude_runs(event):
        if event["kind"] == "stage.started" and event["stage"] == "implement":

            def stop() -> None:
                deadline = time.monotonic() + 30
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                run.request_stop()

            threading.Thread(target=stop, daemon=True).start()

    assert harness.drive(run, on_event=stop_once_claude_runs) == "held"
    assert not any(sessions.iterdir())  # stopped before Claude saved anything to resume
    # With one attempt allowed, spending it on the Stop would fail the package.
    assert harness.drive(run) == "completed"
    package_state = run.load()["packages"]["A"]
    assert package_state["attempts"] == 1
    calls = [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]
    flags = ["--session-id", "--resume", "--session-id"]
    assert [call[call.index(flag) + 1] for call, flag in zip(calls, flags, strict=True)] == [
        package_state["session_id"]
    ] * 3


def test_a_refresh_merge_survives_a_crash_before_its_result_is_saved(harness, monkeypatch):
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")], max_attempts=1)))
    original = wt._write_state

    def lose_refresh_result(root, package, value):
        if (value.get("refresh") or {}).get("head"):
            raise SimulatedCrash()
        original(root, package, value)

    def advance_main(event):
        if event["kind"] == "stage.committed":
            commit_file(harness.repo, "other.txt", "new tip")

    monkeypatch.setattr(wt, "_write_state", lose_refresh_result)
    with pytest.raises(SimulatedCrash):
        harness.drive(run, on_event=advance_main)
    monkeypatch.setattr(wt, "_write_state", original)
    assert harness.drive(run) == "completed"
    assert run.load()["packages"]["A"]["attempts"] == 1
    assert git(harness.repo, "show", "main:a.txt") == "a"
    assert git(harness.repo, "show", "main:other.txt") == "new tip"


@pytest.mark.parametrize("obstruction", ["ancestor", "index.lock"])
def test_removing_a_landing_ancestor_or_index_lock_retries(harness, monkeypatch, obstruction):
    monkeypatch.setattr(scheduler, "FINGERPRINT_POLL", 0.05)
    path = Path(harness.repo, "dir" if obstruction == "ancestor" else ".git/index.lock")
    run = harness.create(workflow(package("A", [write_step("dir/file.txt", "a")], files=["dir/"])))
    fingerprints = []

    def obstruct_then_clear(event):
        if event["kind"] == "merge.rechecked":
            path.write_text("in the way", encoding="utf-8")
        if event["kind"] == "merge.held":
            landing = run.load()["packages"]["A"]["landing"]
            watch = landing.get("watch", [])
            fingerprints.append(mq.checkout_fingerprint(harness.repo, watch, "main"))
            path.unlink()
            fingerprints.append(mq.checkout_fingerprint(harness.repo, watch, "main"))

    assert harness.drive(run, on_event=obstruct_then_clear, hold_wait=2) == "completed"
    assert len(fingerprints) == 2 and fingerprints[0] != fingerprints[1]


@pytest.mark.parametrize("cleared", [True, False])
def test_unfingerprinted_landing_refusals_retry_only_while_hold_wait_lasts(harness, monkeypatch, cleared):
    monkeypatch.setattr(scheduler, "FINGERPRINT_POLL", 0.02)
    monkeypatch.setattr(scheduler, "UNWATCHED_RETRY", 0.05, raising=False)
    original = wt._git
    refusals = []

    def refuse(root, args, **kwargs):
        if wt.same_path(root, harness.repo) and args[:2] == ["merge", "--ff-only"]:
            refusals.append(True)
            if len(refusals) == 1 or not cleared:
                return subprocess.CompletedProcess(args, 1, b"", b"temporary refusal without a watched path")
        return original(root, args, **kwargs)

    monkeypatch.setattr(wt, "_git", refuse)
    run = harness.create(workflow(package("A", [write_step("a.txt", "a")])))
    assert harness.drive(run, hold_wait=1) == ("completed" if cleared else "held")
    assert 2 <= len(refusals) < 12


def test_a_checkout_change_after_hold_wait_still_retries_while_other_work_runs(harness, monkeypatch):
    monkeypatch.setattr(scheduler, "FINGERPRINT_POLL", 0.05)
    blocker = Path(harness.repo, "a.txt")
    run = harness.create(
        workflow(package("A", [write_step("a.txt", "a")]), package("B", [{"sleep": 3}, write_step("b.txt", "b")]))
    )

    def obstruct_then_clear_late(event):
        if event["kind"] == "merge.rechecked" and event["package_id"] == "A":
            blocker.write_text("in the way", encoding="utf-8")
        if event["kind"] == "merge.held" and event["package_id"] == "A":
            # Cleared after hold_wait has run out, while B is still running: the change alone must retry.
            threading.Timer(0.8, lambda: blocker.unlink(missing_ok=True)).start()

    assert harness.drive(run, on_event=obstruct_then_clear_late, hold_wait=0.2) == "completed"
    assert git(harness.repo, "show", "main:a.txt") == "a"


@pytest.mark.parametrize(
    "tail", [b'{"seq": 9, "kind": "complete"}', b'{"seq": 9, "kind": "torn', b'{"seq": 9, "kind": "\xe2']
)
def test_log_repair_preserves_complete_tails_and_uses_the_highest_sequence(tmp_path, tail):
    path = tmp_path / "events.ndjson"
    path.write_bytes(b'{"seq": 1}\n{"seq": 7}\n' + tail)
    complete = tail.endswith(b"}")
    observed = list(read_events(str(path)))
    log = EventLog(str(path))
    appended = log.emit("merge.stale", package_id="A", head_sha="h")
    log.close()
    assert appended["seq"] == (10 if complete else 8)
    assert list(read_events(str(path))) == observed + [appended]
    assert path.read_bytes().endswith(b"\n")


def test_retry_docs_distinguish_conflicts_from_failed_checks():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    architecture = (root / "docs/architecture.md").read_text(encoding="utf-8")
    decisions = (root / "docs/design-decisions.md").read_text(encoding="utf-8")
    for text in (readme, architecture, decisions):
        assert "A failed check, review or re-check continues in the same worktree" in text
    assert "a new attempt starting from the new tip" not in readme
