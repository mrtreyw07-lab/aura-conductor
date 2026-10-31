"""The merge queue: one landing at a time, built in an integration worktree, re-checked, then fast-forwarded.

Each repository and integration branch gets a worktree at ``.conductor/worktrees/@integration-<branch>``
('@' is outside a package id's characters, so no package can take the name), detached at the branch's tip:
git checks a branch out in one place only, and the operator's checkout keeps it.

* ``build`` squashes a verified package branch onto that tip and commits once. A conflict comes back with its
  paths and both SHAs, and the integration worktree goes back to the tip. Nothing touches the branch.
* ``recheck`` runs the package's checks on that commit, in the integration worktree, and returns a verdict
  bound to the landing SHA: the checks ran on exactly that commit and left its tree as they found it.
* ``land`` moves the integration branch to the landing commit by fast-forward only. When the branch is checked
  out in the main checkout it is ``git merge --ff-only`` under guards (nothing staged, no edited or untracked
  file in the way); when it is checked out nowhere it is ``git update-ref <branch> <landing> <parent>``, a
  compare-and-swap that moves only from the exact parent. Nothing is forced. A refusal is a hold that names what
  refused, with a fingerprint of the checkout so the caller retries when it changes. A branch that moved since
  the build is ``stale``: the caller builds again on the new tip and re-checks again.

Every git call goes through ``worktrees``' bounded helpers. Nothing here pushes or contacts a network service.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Sequence

from . import checks as checks_module
from . import receipts
from . import worktrees as wt

WorktreeError = wt.WorktreeError

INTEGRATION_PREFIX = "@integration-"
LANDING_TRAILER = "Conductor-Landing"
PACKAGE_TRAILER = "Conductor-Package"
MERGE_TIMEOUT = 300
FAST_FORWARD_TIMEOUT = 180
HELD_PATHS = 50
# State a stopped merge, squash, cherry-pick or rebase leaves in a worktree's git directory.
INTERRUPTED_STATE = ("MERGE_HEAD", "SQUASH_MSG", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _integration(raw: object) -> str:
    return wt.check_integration_branch(raw)


def _tip(root: str, integration: str) -> str:
    return wt._checked(
        root,
        ["rev-parse", "--verify", f"refs/heads/{integration}^{{commit}}"],
        f"Integration branch {integration} has no commit:",
        timeout=15,
    )


def _commit(root: str, raw: object, label: str) -> str:
    wanted = wt.sha(raw, label)
    return wt._checked(
        root,
        ["rev-parse", "--verify", f"{wanted}^{{commit}}"],
        f"The {label} commit {wanted[:12]} is not in this repository:",
        timeout=15,
    )


def _is_ancestor(root: str, ancestor: str, descendant: str) -> bool:
    return wt._git(root, ["merge-base", "--is-ancestor", ancestor, descendant], timeout=30).returncode == 0


def _output(result) -> str:
    """Both streams: git reports a conflict on stdout and its warnings on stderr."""
    parts = (os.fsdecode(result.stdout or b"").strip(), os.fsdecode(result.stderr or b"").strip())
    return "\n".join(part for part in parts if part)[:2000]


def integration_path(root: str, integration_branch: str) -> str:
    name = INTEGRATION_PREFIX + _integration(integration_branch).replace("/", "-")
    return os.path.join(wt.worktree_parent(wt._directory(root, "workspace")), name)


# ── the integration worktree ─────────────────────────────────────────────


def _registered(root: str, path: str) -> bool:
    """Whether the integration worktree exists. Anything else at its path is refused, never deleted."""
    if not os.path.lexists(path):
        return False
    foreign = {"path": path, "foreign": True}
    if wt.is_link(path):
        raise WorktreeError(f"{path} is a link; the merge queue will not use it.", foreign)
    if not os.path.isdir(path) or os.path.normcase(os.path.realpath(path)) not in wt.registered_worktrees(root):
        raise WorktreeError(
            f"{path} exists but is not a worktree of this repository; the merge queue will neither use nor delete it.",
            foreign,
        )
    wt.linked_worktree(path)
    if os.path.normcase(wt.common_git_dir(path)) != os.path.normcase(wt.common_git_dir(root)):
        raise WorktreeError(f"{path} belongs to another repository; the merge queue will not use it.", foreign)
    return True


def _interrupted(path: str) -> bool:
    listed = wt._checked(
        path,
        ["rev-parse", *[part for name in INTERRUPTED_STATE for part in ("--git-path", name)]],
        "Could not read the integration worktree's state:",
        timeout=15,
    )
    for line in listed.splitlines():
        candidate = line.strip()
        if candidate and os.path.lexists(candidate if os.path.isabs(candidate) else os.path.join(path, candidate)):
            return True
    return False


def _needs_repair(path: str, tip: str) -> bool:
    if wt._out(wt._git(path, ["rev-parse", "HEAD"], timeout=15)) != tip:
        return True
    if wt._git(path, ["symbolic-ref", "-q", "HEAD"], timeout=15).returncode == 0:
        return True  # a branch is checked out there; the queue works detached
    if _interrupted(path):
        return True
    status = wt._git(path, ["status", "--porcelain", "-z", "--untracked-files=all"], timeout=60)
    return status.returncode != 0 or bool(status.stdout)


def _reset_detached(path: str, target: str) -> None:
    """A clean detached checkout of ``target``. ``clean`` never takes ``-x``: linked directories survive."""
    wt._checked(
        path, ["checkout", "--detach", "--force", target], "Could not detach the integration worktree:", timeout=180
    )
    wt._checked(path, ["reset", "--hard", target], "Could not reset the integration worktree:", timeout=180)
    wt._checked(path, ["clean", "-fd"], "Could not clear the integration worktree:", timeout=180)


def ensure(
    root: str, integration_branch: str, manifest: dict | None = None, cancel: threading.Event | None = None
) -> dict:
    """Create or repair the integration worktree: a clean detached checkout of the branch's tip.

    A merge left half-applied, a stray file, or a branch someone checked out there is reset away. A directory at
    the path that git does not list as one of this repository's worktrees is refused and left alone. ``cancel``
    stops the setup commands of a new one (``worktrees.SetupCancelled``).
    """
    root = wt.require_repository(root)
    integration = _integration(integration_branch)
    path = integration_path(root, integration)
    tip = _tip(root, integration)
    wt.exclude_conductor(root)
    if _registered(root, path):
        repaired = _needs_repair(path, tip)
        if repaired:
            _reset_detached(path, tip)
        return {"path": path, "tip_sha": tip, "created": False, "repaired": repaired}
    wt._git(root, ["worktree", "prune"], timeout=30)
    wt._checked(
        root, ["worktree", "add", "--detach", path, tip], "Could not create the integration worktree:", timeout=180
    )
    try:
        wt._link_manifest_directories(root, path, manifest)
        # The re-check needs the environment the package's own checks ran in.
        setup = wt._run_setup(path, f"integration-{integration.replace('/', '-')}", manifest, cancel)
        if setup is not None and not setup.get("passed"):
            raise WorktreeError(
                "Environment setup failed in the integration worktree; it was removed.", {"setup": setup}
            )
    except WorktreeError:
        try:
            wt.remove(path)
        except WorktreeError:
            pass
        raise
    return {"path": path, "tip_sha": tip, "created": True, "repaired": False}


def remove_integration(root: str, integration_branch: str) -> dict:
    return wt.remove(integration_path(wt.require_repository(root), integration_branch))


# ── building a landing ───────────────────────────────────────────────────


def _replayed(path: str, tip: str, landing_line: str, package_line: str) -> str | None:
    """HEAD when it already is this entry's landing on ``tip`` with a clean tree. Anything less is rebuilt."""
    head = wt._out(wt._git(path, ["rev-parse", "HEAD"], timeout=15))
    if not head or head == tip:
        return None
    if wt._out(wt._git(path, ["rev-list", "--parents", "-n", "1", "HEAD"], timeout=15)).split() != [head, tip]:
        return None
    lines = wt._out(wt._git(path, ["log", "-1", "--format=%B", "HEAD"], timeout=15)).splitlines()
    if landing_line not in lines or package_line not in lines:
        return None
    status = wt._git(path, ["status", "--porcelain", "--untracked-files=no"], timeout=60)
    if status.returncode != 0 or status.stdout or _interrupted(path):
        return None
    return head


def _numstat(path: str, parent: str, landing: str) -> list[dict]:
    """Per-file line counts of the landing against its parent; binary files count None."""
    result = wt._git(path, ["diff", "--numstat", "--no-renames", "-z", parent, landing], timeout=60)
    if result.returncode != 0:
        raise WorktreeError(f"Could not count the landing's changes: {wt._err(result)}")
    counted: list[dict] = []
    for record in (result.stdout or b"").split(b"\0"):
        fields = record.split(b"\t", 2)
        if len(fields) == 3:
            added, deleted, name = (os.fsdecode(field) for field in fields)
            counted.append(
                {
                    "path": name.replace("\\", "/"),
                    "added": int(added) if added.isdigit() else None,
                    "deleted": int(deleted) if deleted.isdigit() else None,
                }
            )
    return counted[: wt.MAX_LISTED_FILES]


def build(
    root: str,
    integration_branch: str,
    package_id: str,
    run_id: str,
    title: str,
    package_branch: str | None = None,
    *,
    manifest: dict | None = None,
    identity: Sequence[str] | None = None,
    verified_sha: str | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Build one landing commit on the integration tip: the package branch squashed into one commit.

    ``verified_sha`` is the head the package's checks, review and approval were bound to. The landing is built from
    exactly that commit, and when the branch has moved past it the result is ``moved``: commits nobody verified
    never reach a landing. Nothing here touches the integration branch or the main checkout. A replay (the same
    package commit already built on the same tip) returns the commit it built before.
    """
    root = wt.require_repository(root)
    integration = _integration(integration_branch)
    package = wt.package_id(package_id)
    if not wt.RUN_ID.fullmatch(str(run_id)):
        raise WorktreeError("A bounded run identifier is required.")
    name = wt.branch(package_branch) if package_branch else wt.branch_name(package)
    package_sha = wt._checked(
        root,
        ["rev-parse", "--verify", f"refs/heads/{name}^{{commit}}"],
        f"Package branch {name} has no commit:",
        timeout=15,
    )
    if verified_sha is not None and package_sha != _commit(root, verified_sha, "verified"):
        return {
            "conflict": False,
            "empty": False,
            "replayed": False,
            "moved": True,
            "package_sha": package_sha,
            "verified_sha": verified_sha,
        }
    landing_line = f"{LANDING_TRAILER}: {run_id}:{package}"
    package_line = f"{PACKAGE_TRAILER}: {name}@{package_sha}"
    path = integration_path(root, integration)
    if _registered(root, path):
        tip = _tip(root, integration)
        replayed = _replayed(path, tip, landing_line, package_line)
        if replayed:
            return _landing(path, replayed, tip, package_sha, replayed=True)
    tip = ensure(root, integration, manifest, cancel)["tip_sha"]
    detail = {"integration_sha": tip, "package_sha": package_sha, "path": path}
    try:
        squash = wt._git(
            path, ["merge", "--squash", "--no-commit", "--no-overwrite-ignore", package_sha], timeout=MERGE_TIMEOUT
        )
    except WorktreeError as exc:
        _reset_detached(path, tip)
        raise WorktreeError(
            f"The squash of {name} onto {integration} did not finish; the integration worktree is "
            f"back at {tip[:12]}. {exc}"[:2000],
            detail,
        ) from exc
    if squash.returncode != 0:
        paths = wt.unmerged_paths(path)
        _reset_detached(path, tip)
        return {
            "conflict": True,
            "empty": False,
            "replayed": False,
            "paths": paths,
            **detail,
            "reason": _output(squash),
        }
    staged = wt._git(path, ["diff", "--cached", "--quiet"], timeout=60)
    if staged.returncode == 0:
        _reset_detached(path, tip)
        return {"conflict": False, "empty": True, "replayed": False, **detail}
    if staged.returncode != 1:
        raise WorktreeError(f"Could not read the staged landing: {wt._err(staged)}", detail)
    summary = " ".join(str(title or "").split())[:200]
    subject = f"{package}: {summary}" if summary else package
    committed = wt._git(
        path,
        [*wt.identity_arguments(root, identity), "commit", "-m", subject, "-m", f"{landing_line}\n{package_line}"],
        timeout=120,
    )
    if committed.returncode != 0:
        reason = wt._err(committed) or "git commit failed"
        _reset_detached(path, tip)
        raise WorktreeError(
            f"The landing commit for {package} was refused; the integration worktree is back at {tip[:12]}. {reason}"[
                :2000
            ],
            {**detail, "committed": False, "reason": reason},
        )
    landing = wt._checked(path, ["rev-parse", "HEAD"], "Could not read the landing commit:", timeout=15)
    return _landing(path, landing, tip, package_sha, replayed=False)


def _landing(path: str, landing: str, parent: str, package_sha: str, *, replayed: bool) -> dict:
    return {
        "conflict": False,
        "empty": False,
        "replayed": replayed,
        "landing_sha": landing,
        "parent_sha": parent,
        "package_sha": package_sha,
        "path": path,
        "files": _numstat(path, parent, landing),
    }


def recheck(
    root: str,
    integration_branch: str,
    landing_sha: str,
    checks: Sequence[dict],
    key: str,
    *,
    allowlist: Sequence[str] | None = None,
    cancel: threading.Event | None = None,
    on_result: Callable[[dict], None] | None = None,
) -> dict:
    """Run the package's checks on the landing commit in the integration worktree.

    The verdict is bound to ``landing_sha``: HEAD was that commit before and after, the tree was untouched, and
    the receipt's fingerprint is this exact contract. ``passed`` is true only then.
    """
    root = wt.require_repository(root)
    path = integration_path(root, integration_branch)
    landing = _commit(root, landing_sha, "landing")
    if not _registered(root, path) or wt._out(wt._git(path, ["rev-parse", "HEAD"], timeout=15)) != landing:
        return {
            "passed": False,
            "cancelled": False,
            "receipt": None,
            "reason": f"the integration worktree no longer holds {landing[:12]}",
        }
    receipt = checks_module.run_checks(key, path, checks, allowlist=allowlist, cancel=cancel, on_result=on_result)
    if receipt["cancelled"]:
        receipt["cleanup"] = wt.reset(path, landing)
        return {"passed": False, "cancelled": True, "receipt": receipt, "reason": "the re-check was stopped"}
    reason = checks_module.bound(receipt, key, path, checks, landing)
    return {"passed": reason is None, "cancelled": False, "receipt": receipt, "reason": reason}


# ── landing it ───────────────────────────────────────────────────────────


def checkout_fingerprint(root: str, watch: Sequence[str] = (), branch: str | None = None) -> str:
    """A cheap fingerprint of what holds a landing: the main checkout's symbolic HEAD, HEAD and index (size and
    mtime), each path in ``watch`` (the files that refused the landing: whether each exists, its size and mtime),
    and, with ``branch``, which worktrees have that branch checked out. It changes when the operator commits,
    switches branch, stages, unstages, or moves, edits or deletes a file that was in the way; no status walk runs."""
    root = wt.require_repository(root)
    symbolic = wt._git(root, ["symbolic-ref", "-q", "HEAD"], timeout=15)
    head = wt._git(root, ["rev-parse", "-q", "--verify", "HEAD"], timeout=15)
    git_dir = wt._checked(root, ["rev-parse", "--git-dir"], "Could not resolve the repository:", timeout=15)
    index = os.path.join(git_dir if os.path.isabs(git_dir) else os.path.join(root, git_dir), "index")
    try:
        entry = os.stat(index)
        size, modified = entry.st_size, entry.st_mtime_ns
    except OSError:
        size, modified = -1, -1
    try:
        lock = os.stat(index + ".lock")
        index_lock = [lock.st_size, lock.st_mtime_ns]
    except OSError:
        index_lock = None
    watched = []
    for name in list(watch)[: HELD_PATHS * 3]:
        try:
            stat = os.lstat(os.path.join(root, *str(name).split("/")))
            watched.append([name, stat.st_size, stat.st_mtime_ns])
        except OSError:
            watched.append([name, None, None])
    bound = [
        wt._out(symbolic) if symbolic.returncode == 0 else "detached",
        wt._out(head) if head.returncode == 0 else "",
        size,
        modified,
        index_lock,
        watched,
        sorted(_checked_out(root, branch)) if branch else [],
    ]
    return hashlib.sha256(json.dumps(bound).encode("utf-8")).hexdigest()


def _checked_out(root: str, integration: str) -> list[str]:
    """Every worktree (canonical path) that has the integration branch checked out."""
    return [path for path, name in wt.registered_worktrees(root).items() if name == integration]


def _refusal(root: str, integration: str, parent: str, landing: str) -> dict | None:
    """What in the main checkout refuses this fast-forward, checked before anything is touched."""
    staged = wt.staged_paths(root)
    if staged:
        return {
            "reason": f"{integration} has staged changes, starting with {staged[0]}; the landing waits until "
            "they are committed or unstaged.",
            "staged": staged[:HELD_PATHS],
        }
    collisions = wt.untracked_collisions(root, parent, landing)
    if collisions:
        return {
            "reason": "The landing would overwrite files the main checkout keeps untracked: "
            + ", ".join(collisions[:10])
            + ".",
            "collisions": collisions[:HELD_PATHS],
        }
    changed = wt._git(root, ["diff", "--name-only", "--no-renames", "-z", parent, landing], timeout=60)
    if changed.returncode != 0:
        raise WorktreeError(f"Could not list the landing's paths: {wt._err(changed)}")
    modified = wt.modified_paths(root, wt._names(changed))
    if modified:
        return {
            "reason": "The main checkout has uncommitted edits to files the landing changes: "
            + ", ".join(modified[:10])
            + ".",
            "modified": modified[:HELD_PATHS],
        }
    return None


def land(
    root: str,
    integration_branch: str,
    package_id: str,
    landing_sha: str,
    parent_sha: str,
    package_sha: str | None = None,
) -> dict:
    """Fast-forward the integration branch to a landing commit, or hold; never force.

    Already landed is success (a replay). A branch that moved since the build is ``stale``. A refusal by the
    main checkout is ``held`` with what refused, and nothing changes.
    """
    root = wt.require_repository(root)
    integration = _integration(integration_branch)
    package = wt.package_id(package_id)
    landing = _commit(root, landing_sha, "landing")
    parent = _commit(root, parent_sha, "parent")
    package_commit = _commit(root, package_sha, "package") if package_sha else None
    shape = wt._out(wt._git(root, ["rev-list", "--parents", "-n", "1", landing], timeout=15)).split()
    message = wt._out(wt._git(root, ["log", "-1", "--format=%B", landing], timeout=15)).splitlines()
    if shape != [landing, parent] or not any(
        line.startswith(f"{LANDING_TRAILER}: ") and line.endswith(f":{package}") for line in message
    ):
        raise WorktreeError(
            f"{landing[:12]} is not a merge-queue landing of {package} on {parent[:12]}.",
            {"sha": landing, "parent_sha": parent},
        )
    base = {"sha": landing, "parent_sha": parent, "integration_branch": integration}
    tip = _tip(root, integration)
    if tip == landing or _is_ancestor(root, landing, tip):
        ref, error = receipts.keep_after_landing(root, package, package_commit)
        _settle_integration_worktree(root, integration, landing)
        return {**base, "landed": True, "already": True, "receipts_ref": ref, "receipts_error": error, "head_sha": tip}
    if tip != parent:
        return {**base, "landed": False, "stale": True, "held": False, "head_sha": tip}
    holders = _checked_out(root, integration)
    main = os.path.normcase(os.path.realpath(root))
    if holders and holders != [main]:
        return {
            **base,
            "landed": False,
            "held": True,
            "stale": False,
            "head_sha": tip,
            "reason": f"{integration} is checked out in another worktree; the landing waits until it is not.",
            "watch": [],
            "fingerprint": checkout_fingerprint(root, (), integration),
        }
    failure = None
    if holders:
        refusal = _refusal(root, integration, parent, landing)
        if refusal:
            watch = [*refusal.get("staged", []), *refusal.get("collisions", []), *refusal.get("modified", [])]
            return {
                **base,
                "landed": False,
                "held": True,
                "stale": False,
                **refusal,
                "watch": watch,
                "fingerprint": checkout_fingerprint(root, watch, integration),
                "head_sha": tip,
            }
        try:
            result = wt._git(
                root, ["merge", "--ff-only", "--no-overwrite-ignore", landing], timeout=FAST_FORWARD_TIMEOUT
            )
            if result.returncode != 0:
                failure = wt._err(result) or "git merge --ff-only failed"
        except WorktreeError as exc:
            failure = str(exc)
    else:
        # Checked out nowhere: a compare-and-swap that moves the branch only from the exact parent.
        result = wt._git(
            root,
            ["update-ref", "-m", f"conductor: land {package}", f"refs/heads/{integration}", landing, parent],
            timeout=30,
        )
        if result.returncode != 0:
            failure = wt._err(result) or "git update-ref refused"
    after = _tip(root, integration)
    if after != landing and not _is_ancestor(root, landing, after):
        if failure is None:
            raise WorktreeError(
                f"The fast-forward left {integration} at {after[:12]} instead of {landing[:12]}.",
                {**base, "head_sha": after},
            )
        if after != parent:
            return {**base, "landed": False, "stale": True, "held": False, "head_sha": after}
        # No abort and no reset: the main checkout may hold the operator's own merge in progress.
        return {
            **base,
            "landed": False,
            "held": True,
            "stale": False,
            "head_sha": after,
            "reason": f"{integration} could not fast-forward to {landing[:12]}; nothing was forced. {failure}"[:2000],
            "watch": [],
            "retry_periodically": True,
            "fingerprint": checkout_fingerprint(root, (), integration),
        }
    ref, error = receipts.keep_after_landing(root, package, package_commit)
    _settle_integration_worktree(root, integration, landing)
    return {**base, "landed": True, "already": False, "receipts_ref": ref, "receipts_error": error, "head_sha": after}


def _settle_integration_worktree(root: str, integration: str, landing: str) -> None:
    """Best effort: clear what the re-check left, only while the integration worktree still holds this landing."""
    try:
        path = integration_path(root, integration)
        if _registered(root, path) and wt._out(wt._git(path, ["rev-parse", "HEAD"], timeout=15)) == landing:
            _reset_detached(path, landing)
    except WorktreeError:
        pass
