"""Package worktrees on real git repositories: containment, idempotent stage commits, merges never rebases."""

from __future__ import annotations

import os
import threading
import time

import pytest

from conductor import worktrees as wt
from conductor.worktrees import LeaseManager, WorktreeError

from .conftest import git, symlinks_available

pytestmark = pytest.mark.integration
KEY = "run-1:A:implement:1"


def write(path: str, name: str, text: str) -> None:
    target = os.path.join(path, *name.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def test_provision_creates_a_worktree_on_its_own_branch_and_reuses_it(repo):
    info = wt.provision(repo, "A", "main")
    assert info["created"] and info["branch"] == "conductor/A" and info["base_sha"] == git(repo, "rev-parse", "main")
    assert os.path.isfile(os.path.join(info["path"], ".git"))
    assert git(info["path"], "symbolic-ref", "--short", "HEAD") == "conductor/A"
    assert git(repo, "status", "--porcelain") == ""  # .conductor/ is excluded through info/exclude
    write(info["path"], "partial.txt", "kept")
    again = wt.provision(repo, "A", "main")
    assert not again["created"] and os.path.exists(os.path.join(again["path"], "partial.txt"))


def test_a_fresh_provision_preserves_a_branch_that_carries_commits(repo):
    info = wt.provision(repo, "A", "main")
    write(info["path"], "a.txt", "a")
    committed = wt.commit(info["path"], "implement", KEY, "A")
    fresh = wt.provision(repo, "A", "main", fresh=True, preserve_label="attempt-1")
    assert fresh["created"] and fresh["preserved_branch"] == "conductor/A@attempt-1"
    assert git(repo, "rev-parse", "conductor/A@attempt-1") == committed["commit_sha"]
    assert git(repo, "rev-parse", "conductor/A") == git(repo, "rev-parse", "main")


def test_operations_refuse_anything_but_a_linked_worktree(repo):
    with pytest.raises(WorktreeError, match="not a package worktree"):
        wt.linked_worktree(repo)
    os.makedirs(os.path.join(repo, "sub"))
    with pytest.raises(WorktreeError, match="not a package worktree"):
        wt.reset(os.path.join(repo, "sub"), git(repo, "rev-parse", "HEAD"))
    write(repo, "uncommitted.txt", "the operator's work")
    with pytest.raises(WorktreeError):
        wt.commit(repo, "implement", KEY, "A")
    assert os.path.exists(os.path.join(repo, "uncommitted.txt"))


def test_stage_commits_are_idempotent_per_dispatch_key(repo):
    path = wt.provision(repo, "A", "main")["path"]
    write(path, "a.txt", "a")
    first = wt.commit(path, "implement", KEY, "Package A")
    assert not first["empty"] and first["patch"]["files"] == ["a.txt"]
    message = git(path, "log", "-1", "--format=%B")
    assert f"Dispatch-Key: {KEY}" in message and "Package: Package A" in message
    replay = wt.commit(path, "implement", KEY, "Package A")
    assert replay["replayed"] and replay["commit_sha"] == first["commit_sha"]
    assert wt.commit(path, "implement", "run-1:A:implement:2", "A")["empty"]
    with pytest.raises(WorktreeError, match="dispatch key"):
        wt.commit(path, "implement", "not-a-key", "A")
    with pytest.raises(WorktreeError, match="does not match"):
        wt.commit(path, "verify", KEY, "A")
    assert wt.changed_files(path, "main") == ["a.txt"]
    assert wt.commits_since(path, git(repo, "rev-parse", "main")) == 1


def test_reset_keeps_the_discarded_work_as_evidence(repo):
    path = wt.provision(repo, "A", "main")["path"]
    base = wt.head(path)
    write(path, "a.txt", "a")
    write(path, "new.txt", "untracked")
    evidence = wt.reset(path, base)
    assert evidence["reset_to"] == base and "new.txt" in evidence["untracked"]
    assert sorted(os.listdir(path)) == sorted([".git", ".gitignore", "README.md"])


def test_refresh_merges_and_never_rewrites_stage_commits(repo):
    path = wt.provision(repo, "A", "main")["path"]
    write(path, "a.txt", "a")
    stage = wt.commit(path, "implement", KEY, "A")["commit_sha"]
    assert not wt.refresh_base(path, "main")["refreshed"]
    write(repo, "main.txt", "moved on")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moves")
    refreshed = wt.refresh_base(path, "main")
    assert refreshed["refreshed"] and refreshed["new_base"] == git(repo, "rev-parse", "main")
    assert git(path, "merge-base", "--is-ancestor", stage, "HEAD") == ""  # the stage commit is still an ancestor
    assert len(git(path, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3  # a merge commit, not a rebase


def test_a_conflicting_refresh_aborts_and_names_the_paths(repo):
    path = wt.provision(repo, "A", "main")["path"]
    write(path, "README.md", "# package\n")
    wt.commit(path, "implement", KEY, "A")
    head = wt.head(path)
    write(repo, "README.md", "# main\n")
    git(repo, "commit", "-q", "-am", "main edits the README")
    with pytest.raises(WorktreeError) as raised:
        wt.refresh_base(path, "main")
    assert raised.value.detail["conflict"] and raised.value.detail["paths"] == ["README.md"]
    assert wt.head(path) == head and git(path, "status", "--porcelain") == ""


def test_remove_only_touches_worktrees_and_never_follows_links(repo, tmp_path):
    with pytest.raises(WorktreeError, match="Only a directory under"):
        wt.remove(repo)
    info = wt.provision(repo, "A", "main")
    keep = tmp_path / "shared-cache"
    keep.mkdir()
    (keep / "precious.txt").write_text("not the worktree's", encoding="utf-8")
    if symlinks_available(tmp_path):
        os.symlink(keep, os.path.join(info["path"], "cache"), target_is_directory=True)
    removed = wt.remove(info["path"])
    assert removed["removed"] and not os.path.exists(info["path"])
    assert (keep / "precious.txt").read_text(encoding="utf-8") == "not the worktree's"
    assert git(repo, "rev-parse", "--verify", "conductor/A")  # the branch is kept
    assert not wt.remove(info["path"])["removed"]


def test_remove_refuses_a_path_that_resolves_outside_its_repository(repo, tmp_path):
    outside = tmp_path / "outside"
    (outside / "worktrees" / "A").mkdir(parents=True)
    (outside / "worktrees" / "A" / "precious.txt").write_text("keep", encoding="utf-8")
    loose = tmp_path / "loose" / ".conductor" / "worktrees" / "A"  # the right shape, but in no repository
    loose.mkdir(parents=True)
    with pytest.raises(WorktreeError, match="not in a git repository"):
        wt.remove(str(loose))
    assert loose.is_dir()
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks need a privilege this machine does not grant")
    os.makedirs(os.path.join(repo, ".conductor", "worktrees"))
    linked = os.path.join(repo, ".conductor", "worktrees", "A")
    os.symlink(outside / "worktrees" / "A", linked, target_is_directory=True)
    with pytest.raises(WorktreeError, match="does not resolve to itself"):
        wt.remove(linked)
    os.rmdir(linked) if os.name == "nt" else os.unlink(linked)
    os.rmdir(os.path.join(repo, ".conductor", "worktrees"))
    os.rmdir(os.path.join(repo, ".conductor"))
    os.symlink(outside, os.path.join(repo, ".conductor"), target_is_directory=True)  # the whole parent is a link
    with pytest.raises(WorktreeError, match="is a link"):
        wt.remove(os.path.join(repo, ".conductor", "worktrees", "A"))
    assert (outside / "worktrees" / "A" / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_manifest_links_are_symlinks_into_the_main_checkout(repo, tmp_path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks need a privilege this machine does not grant")
    os.makedirs(os.path.join(repo, "node_modules", "dep"))
    write(repo, ".gitignore", "node_modules/\n__pycache__/\n")
    git(repo, "commit", "-q", "-am", "ignore node_modules")
    info = wt.provision(repo, "A", "main", {"link": ["node_modules"], "setup": [], "env": None})
    link = os.path.join(info["path"], "node_modules")
    assert wt.is_link(link) and os.path.isdir(os.path.join(link, "dep"))
    wt.reset(info["path"], info["base_sha"])  # the link is excluded and clean never takes -x: it survives
    assert wt.is_link(link)
    write(info["path"], "a.txt", "a")
    assert wt.commit(info["path"], "implement", KEY, "A")["patch"]["files"] == ["a.txt"]  # never committed
    assert wt.remove(info["path"])["unlinked"] == [link]
    assert os.path.isdir(os.path.join(repo, "node_modules", "dep"))


def test_failed_setup_removes_the_worktree(repo):
    manifest = {
        "link": [],
        "setup": [{"argv": ["{python}", "-c", "raise SystemExit(1)"], "timeout_seconds": 30}],
        "env": None,
    }
    with pytest.raises(WorktreeError, match="Environment setup failed"):
        wt.provision(repo, "A", "main", manifest)
    assert not os.path.exists(wt.worktree_path(repo, "A"))


def test_delete_branch_only_when_another_ref_keeps_its_tip(repo):
    path = wt.provision(repo, "A", "main")["path"]
    write(path, "a.txt", "a")
    tip = wt.commit(path, "implement", KEY, "A")["commit_sha"]
    wt.remove(path)
    assert not wt.delete_branch_if_kept(repo, "conductor/A", "refs/conductor/receipts/A/tip")
    git(repo, "update-ref", "refs/conductor/receipts/A/tip", tip)
    assert wt.delete_branch_if_kept(repo, "conductor/A", "refs/conductor/receipts/A/tip")
    assert git(repo, "rev-parse", "refs/conductor/receipts/A/tip") == tip


def test_identity_falls_back_to_conductor(repo, tmp_path):
    assert wt.identity_arguments(repo) == ["-c", "user.name=Test Operator", "-c", "user.email=operator@example.invalid"]
    assert wt.identity_arguments(repo, ["Bot", "bot@example.invalid"])[1] == "user.name=Bot"
    assert wt.identity_arguments(repo, ["Bad\nName", "x@example.invalid"])[1] == "user.name=conductor"


def test_validators_refuse_unbounded_input():
    for bad in ("", "a b", "../x", "-x", "x" * 81, "x."):
        with pytest.raises(WorktreeError):
            wt.package_id(bad)
    for bad in ("a..b", "a@{1}", "x.lock", "/x", "x/"):
        with pytest.raises(WorktreeError):
            wt.branch(bad)
    with pytest.raises(WorktreeError):
        wt.sha("not-a-sha", "base")


def test_leases_keep_siblings_parallel_and_nested_trees_exclusive(tmp_path):
    leases = LeaseManager()
    root = str(tmp_path)
    a, b = os.path.join(root, "w", "A"), os.path.join(root, "w", "B")
    for path in (a, b):
        os.makedirs(path)
    assert leases.acquire(a, "one", timeout=0) and leases.acquire(b, "two", timeout=0)
    assert not leases.acquire(root, "three", timeout=0)  # the parent conflicts with both
    assert not leases.acquire(a, "four", timeout=0)
    leases.release("one")
    leases.release("two")
    assert leases.acquire(root, "three", timeout=0)
    released = threading.Timer(0.3, leases.release, args=("three",))
    released.start()
    started = time.monotonic()
    with leases.lease(a, "five", timeout=5):
        assert time.monotonic() - started >= 0.2
        assert list(leases.held().values()) == ["five"]
    assert leases.held() == {}
    leases.acquire(a, "six", timeout=0)
    with pytest.raises(wt.LeaseTimeout), leases.lease(a, "seven", timeout=0.1):
        pass
