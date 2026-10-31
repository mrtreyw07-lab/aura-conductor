"""Receipts: what each stage, check and landing produced, kept where it can be checked later.

Two kinds of receipt:

* **Receipt refs.** After a landing, ``refs/conductor/receipts/<package>/tip`` points at the package branch's last
  stage commit, so every stage commit stays reachable after the branch is pruned. An earlier tip the new one does
  not contain keeps its own ref (``refs/conductor/receipts/<package>/<sha>``) beside it. A stage commit discarded for
  leaving its scope is kept under ``refs/conductor/discarded/<package>/<sha>``. Nothing is pushed.
* **Receipt files.** JSON documents under the run directory (``receipts/<package>/<n>-<kind>.json``): the stage
  patch, a discarded stage's patch, the check receipts, the landing. Each is recorded in the event log with its
  SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from . import worktrees as wt

RECEIPTS_REF = "refs/conductor/receipts/{package_id}/tip"
RECEIPTS_PREFIX = "refs/conductor/receipts/"
DISCARDED_PREFIX = "refs/conductor/discarded/"


def receipts_ref(root: str, package_id: str) -> str:
    ref = RECEIPTS_REF.format(package_id=wt.package_id(package_id))
    if wt._git(root, ["check-ref-format", ref], timeout=15).returncode != 0:
        raise wt.WorktreeError(f"Package id {package_id} cannot name a receipts ref.")
    return ref


def keep_stage_commits(root: str, package_id: str, package_sha: str | None) -> str | None:
    """Point the receipts ref at the package's last stage commit; keep an earlier tip it does not contain."""
    if not package_sha:
        return None
    ref = receipts_ref(root, package_id)
    current = wt._git(root, ["rev-parse", "-q", "--verify", ref], timeout=15)
    existing = wt._out(current) if current.returncode == 0 else ""
    if existing == package_sha:
        return ref
    if existing and wt._git(root, ["merge-base", "--is-ancestor", existing, package_sha], timeout=30).returncode:
        wt._checked(
            root,
            ["update-ref", f"{RECEIPTS_PREFIX}{package_id}/{existing}", existing],
            "Could not keep the earlier stage commits reachable:",
            timeout=30,
        )
    wt._checked(root, ["update-ref", ref, package_sha], "Could not keep the stage commits reachable:", timeout=30)
    return ref


def keep_after_landing(root: str, package_id: str, package_sha: str | None) -> tuple[str | None, str | None]:
    """The receipts ref, written after the fast-forward. The landing has happened by then, so a refusal (a stale
    ref lock) is reported, never raised: the package branch still holds the stage commits."""
    try:
        return keep_stage_commits(root, package_id, package_sha), None
    except wt.WorktreeError as exc:
        return None, str(exc)[:600]


def keep_discarded(root: str, package_id: str, commit_sha: str) -> str:
    """Keep a stage commit that was discarded for leaving its scope reachable, under
    ``refs/conductor/discarded/<package>/<sha>``, before the worktree is reset away from it."""
    commit = wt.sha(commit_sha, "discarded")
    ref = f"{DISCARDED_PREFIX}{wt.package_id(package_id)}/{commit}"
    wt._checked(root, ["update-ref", ref, commit], "Could not keep the discarded stage commit:", timeout=30)
    return ref


def listed(root: str, package_id: str | None = None) -> dict[str, str]:
    """Receipt refs → SHA, for one package or all."""
    prefix = RECEIPTS_PREFIX + (f"{wt.package_id(package_id)}/" if package_id else "")
    result = wt._git(root, ["for-each-ref", "--format=%(refname) %(objectname)", prefix], timeout=30)
    rows = (line.split(" ", 1) for line in wt._out(result).splitlines() if " " in line)
    return dict(rows)


class ReceiptStore:
    """JSON receipts under a run directory, numbered per package so their order is their history."""

    def __init__(self, directory: str):
        self.directory = directory
        self._lock = threading.Lock()

    def write(self, package_id: str, kind: str, data: dict) -> dict:
        package = wt.package_id(package_id)
        if not kind.replace("-", "").isalnum():
            raise ValueError(f"Invalid receipt kind {kind!r}.")
        body = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            folder = os.path.join(self.directory, package)
            os.makedirs(folder, exist_ok=True)
            number = 1 + sum(1 for name in os.listdir(folder) if name.endswith(".json"))
            name = f"{number:03d}-{kind}.json"
            partial = os.path.join(folder, name + ".partial")
            with open(partial, "wb") as handle:
                handle.write(body)
            os.replace(partial, os.path.join(folder, name))
        return {"path": f"{package}/{name}", "sha256": hashlib.sha256(body).hexdigest()}

    def read(self, relative: str) -> dict:
        with open(os.path.join(self.directory, *relative.split("/")), encoding="utf-8") as handle:
            return json.load(handle)

    def listing(self, package_id: str) -> list[str]:
        folder = os.path.join(self.directory, wt.package_id(package_id))
        try:
            return sorted(name for name in os.listdir(folder) if name.endswith(".json"))
        except FileNotFoundError:
            return []
