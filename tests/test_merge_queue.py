"""The merge queue on real repositories: squash, re-check bound to the landing SHA, fast-forward or hold."""

from __future__ import annotations

import os

import pytest

from conductor import merge_queue as mq
from conductor import receipts
from conductor import worktrees as wt
from conductor.worktrees import WorktreeError

from .conftest import PASS, git

pytestmark = pytest.mark.integration


def write(path: str, name: str, text: str) -> None:
    target = os.path.join(path, *name.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def branch_with(repo: str, package_id: str, files: dict[str, str]) -> str:
    path = wt.provision(repo, package_id, "main")["path"]
    for name, text in files.items():
        write(path, name, text)
    return wt.commit(path, "implement", f"run-1:{package_id}:implement:1", package_id)["commit_sha"]


def landing(repo: str, package_id: str = "A", files: dict[str, str] | None = None) -> dict:
    branch_with(repo, package_id, files or {f"{package_id.lower()}.txt": package_id})
    return mq.build(repo, "main", package_id, "run-1", f"Package {package_id}")


def reflog(repo: str) -> list[str]:
    return git(repo, "reflog", "--format=%gs", "refs/heads/main").splitlines()


def test_build_squashes_into_one_commit_on_the_tip_and_replays(repo):
    built = landing(repo, files={"a.txt": "a", "dir/b.txt": "b"})
    tip = git(repo, "rev-parse", "main")
    assert not built["conflict"] and built["parent_sha"] == tip
    assert git(repo, "rev-list", "--parents", "-n", "1", built["landing_sha"]).split() == [built["landing_sha"], tip]
    message = git(repo, "log", "-1", "--format=%B", built["landing_sha"])
    assert message.startswith("A: Package A") and "Conductor-Landing: run-1:A" in message
    assert f"Conductor-Package: conductor/A@{built['package_sha']}" in message
    assert sorted(entry["path"] for entry in built["files"]) == ["a.txt", "dir/b.txt"]
    assert git(repo, "rev-parse", "main") == tip  # building touches neither main nor the checkout
    assert mq.build(repo, "main", "A", "run-1", "Package A") == {**built, "replayed": True}


def test_the_integration_worktree_is_repaired_or_refused(repo):
    ensured = mq.ensure(repo, "main")
    assert ensured["created"] and git(ensured["path"], "rev-parse", "HEAD") == git(repo, "rev-parse", "main")
    write(ensured["path"], "stray.txt", "left over")
    assert mq.ensure(repo, "main")["repaired"] and not os.path.exists(os.path.join(ensured["path"], "stray.txt"))
    mq.remove_integration(repo, "main")
    os.makedirs(ensured["path"])
    write(ensured["path"], "foreign.txt", "someone else's")
    with pytest.raises(WorktreeError, match="not a worktree of this repository"):
        mq.ensure(repo, "main")
    assert os.path.exists(os.path.join(ensured["path"], "foreign.txt"))


def test_a_conflict_comes_back_with_its_paths_and_nothing_changes(repo):
    branch_with(repo, "A", {"README.md": "# from the package\n"})
    write(repo, "README.md", "# from main\n")
    git(repo, "commit", "-q", "-am", "main edits the README")
    tip = git(repo, "rev-parse", "main")
    built = mq.build(repo, "main", "A", "run-1", "A")
    assert built["conflict"] and built["paths"] == ["README.md"] and built["integration_sha"] == tip
    integration = mq.integration_path(repo, "main")
    assert git(integration, "rev-parse", "HEAD") == tip and git(integration, "status", "--porcelain") == ""


def test_a_branch_main_already_holds_is_empty(repo):
    branch_with(repo, "A", {"a.txt": "a"})
    git(repo, "merge", "-q", "--ff-only", "conductor/A")
    assert mq.build(repo, "main", "A", "run-1", "A")["empty"]


def test_the_recheck_is_bound_to_the_landing_sha(repo):
    built = landing(repo)
    verdict = mq.recheck(repo, "main", built["landing_sha"], [PASS], "run-1:A:verify:2")
    assert verdict["passed"] and verdict["receipt"]["head_sha_before"] == built["landing_sha"]
    failing = mq.recheck(
        repo,
        "main",
        built["landing_sha"],
        [{"argv": ["{python}", "-c", "raise SystemExit(4)"], "timeout_seconds": 30}],
        "run-1:A:verify:3",
    )
    assert not failing["passed"] and "exited 4" in failing["reason"]
    dirty = {"argv": ["{python}", "-c", "open('made-by-check.txt', 'w').write('x')"], "timeout_seconds": 30}
    changed = mq.recheck(repo, "main", built["landing_sha"], [dirty], "run-1:A:verify:4")
    assert not changed["passed"] and changed["reason"] == "check 1 changed the tree it was checking"
    other = mq.recheck(repo, "main", git(repo, "rev-parse", "main"), [PASS], "run-1:A:verify:5")
    assert not other["passed"] and "no longer holds" in other["reason"]


def test_land_fast_forwards_a_clean_checkout_and_keeps_the_receipts(repo):
    built = landing(repo)
    landed = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"], built["package_sha"])
    assert landed["landed"] and not landed["already"] and landed["head_sha"] == built["landing_sha"]
    assert git(repo, "rev-parse", "HEAD") == built["landing_sha"] and git(repo, "status", "--porcelain") == ""
    assert landed["receipts_ref"] == "refs/conductor/receipts/A/tip"
    assert receipts.listed(repo, "A") == {"refs/conductor/receipts/A/tip": built["package_sha"]}
    assert reflog(repo)[0].startswith("merge") and "Fast-forward" in reflog(repo)[0]
    again = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"], built["package_sha"])
    assert again["landed"] and again["already"]


def test_land_moves_a_branch_checked_out_nowhere_by_compare_and_swap(repo):
    git(repo, "checkout", "-q", "-b", "elsewhere")
    built = landing(repo)
    landed = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"])
    assert landed["landed"] and git(repo, "rev-parse", "main") == built["landing_sha"]
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "elsewhere"
    assert reflog(repo)[0] == "conductor: land A"


def test_a_moved_tip_is_stale_and_nothing_is_forced(repo):
    built = landing(repo)
    write(repo, "other.txt", "x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "someone else lands first")
    tip = git(repo, "rev-parse", "main")
    stale = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"])
    assert stale["stale"] and not stale["landed"] and git(repo, "rev-parse", "main") == tip


@pytest.mark.parametrize(
    "arrange,field",
    [
        (lambda repo: (write(repo, "staged.txt", "x"), git(repo, "add", "staged.txt")), "staged"),
        (lambda repo: write(repo, "a.txt", "the operator's own a.txt"), "collisions"),
        (lambda repo: write(repo, "README.md", "edited, not committed\n"), "modified"),
    ],
)
def test_the_main_checkout_holds_a_landing_and_nothing_changes(repo, arrange, field):
    files = {"a.txt": "a", "README.md": "# fixture\nthe package's line\n"}
    built = landing(repo, files=files)
    arrange(repo)
    before = git(repo, "rev-parse", "main")
    held = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"])
    assert held["held"] and not held["landed"] and held[field] and held["fingerprint"]
    assert git(repo, "rev-parse", "main") == before


def test_a_branch_checked_out_in_another_worktree_holds(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "worktree", "add", "-q", str(tmp_path / "other"), "main")
    built = landing(repo)
    held = mq.land(repo, "main", "A", built["landing_sha"], built["parent_sha"])
    assert held["held"] and "another worktree" in held["reason"]


def test_land_refuses_a_commit_that_is_not_a_landing(repo):
    tip = git(repo, "rev-parse", "main")
    branch_with(repo, "A", {"a.txt": "a"})
    with pytest.raises(WorktreeError, match="not a merge-queue landing"):
        mq.land(repo, "main", "A", git(repo, "rev-parse", "conductor/A"), tip)


def test_the_checkout_fingerprint_moves_with_the_index(repo):
    first = mq.checkout_fingerprint(repo)
    assert first == mq.checkout_fingerprint(repo)
    write(repo, "new.txt", "x")
    git(repo, "add", "new.txt")
    assert mq.checkout_fingerprint(repo) != first


def test_an_earlier_tip_the_new_one_does_not_contain_keeps_its_own_ref(repo):
    first = branch_with(repo, "A", {"a.txt": "one"})
    assert receipts.keep_stage_commits(repo, "A", first) == "refs/conductor/receipts/A/tip"
    second = wt.provision(repo, "A", "main", fresh=True, preserve_label="attempt-1")["path"]
    write(second, "a.txt", "two")
    tip = wt.commit(second, "implement", "run-1:A:implement:2", "A")["commit_sha"]
    receipts.keep_stage_commits(repo, "A", tip)
    assert receipts.listed(repo, "A") == {
        "refs/conductor/receipts/A/tip": tip,
        f"refs/conductor/receipts/A/{first}": first,
    }


def test_receipt_files_are_numbered_and_hashed(tmp_path):
    store = receipts.ReceiptStore(str(tmp_path))
    first = store.write("A", "stage", {"n": 1})
    second = store.write("A", "checks", {"n": 2})
    assert (first["path"], second["path"]) == ("A/001-stage.json", "A/002-checks.json")
    assert store.read(second["path"]) == {"n": 2} and store.listing("A") == ["001-stage.json", "002-checks.json"]
    assert store.listing("B") == []
    with pytest.raises(ValueError):
        store.write("A", "../x", {})
