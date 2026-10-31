"""Git worktrees for packages: one linked worktree and one branch per package, written under a lease.

Each package runs in ``<repo>/.conductor/worktrees/<package-id>`` on branch ``conductor/<package-id>``, so
packages are parallel by construction and every stage leaves a commit as its receipt. Every git call is an argv
list with a timeout, no shell and a scrubbed environment. Nothing here pushes or contacts a network service.

Four containment rules carry weight, and each has a test:

* Every per-worktree operation first proves the path is a linked worktree. ``os.path.isdir`` alone would let
  ``git -C <path> reset --hard`` discover the enclosing repository and destroy the main checkout's work.
* ``git clean`` never takes ``-x``, so links into ignored directories (``.venv``, ``node_modules``) survive.
* ``remove`` unlinks every link inside a worktree before deleting it. A recursive delete of a tree that still
  holds links could follow them into the directories they point at.
* ``refresh_base`` merges the integration branch into the package branch and never rebases: a rebase would
  rewrite the stage commits the receipts name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from . import procs

PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@-]{0,199}$")
SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
# Names Windows reserves for devices, with or without an extension: no directory can be called that.
WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DISPATCH_KEY = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,63}):([A-Za-z0-9][A-Za-z0-9_.-]{0,79}):"
    r"(implement|verify|review):([1-9][0-9]?)$"
)

WORKTREE_PARENT = ".conductor/worktrees"
BRANCH_PREFIX = "conductor/"
STATE_DIRECTORY = "conductor-worktrees"
MAX_PATCH_BYTES = 4 * 1024 * 1024
MAX_LISTED_FILES = 2000
MAX_WALK_ENTRIES = 200_000
# `git hash-object -t tree /dev/null`: the diff base for a branch's root commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_IDENTITY = ("conductor", "conductor@localhost")


class WorktreeError(ValueError):
    """A worktree operation could not complete inside its bounded contract."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})


# ── bounded git ──────────────────────────────────────────────────────────


def git_environment() -> dict[str, str]:
    """The scrubbed environment, with prompts and optional locks off."""
    env = procs.scrubbed_env(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(cwd: str, args: Sequence[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *[str(a) for a in args]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            shell=False,
            env=git_environment(),
        )
    except FileNotFoundError as exc:
        raise WorktreeError("Git is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"Git did not finish within {timeout:.0f}s: git {' '.join(map(str, args[:3]))}") from exc
    except OSError as exc:
        raise WorktreeError(f"Git could not run: {type(exc).__name__}") from exc


def _out(result: subprocess.CompletedProcess) -> str:
    return os.fsdecode(result.stdout or b"").strip()


def _err(result: subprocess.CompletedProcess) -> str:
    text = os.fsdecode(result.stderr or b"").strip() or os.fsdecode(result.stdout or b"").strip()
    return text[:2000]


def _checked(cwd: str, args: Sequence[str], message: str, *, timeout: float = 60.0, detail: dict | None = None) -> str:
    result = _git(cwd, args, timeout=timeout)
    if result.returncode != 0:
        raise WorktreeError(f"{message} {_err(result)}".strip(), detail)
    return _out(result)


def _names(result: subprocess.CompletedProcess) -> list[str]:
    return sorted({os.fsdecode(name).replace("\\", "/") for name in (result.stdout or b"").split(b"\0") if name})


def _directory(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise WorktreeError(f"A bounded {label} path is required.")
    candidate = os.path.abspath(raw.strip())
    if not os.path.isdir(candidate):
        raise WorktreeError(f"The {label} does not exist: {candidate}")
    return candidate


def package_id(raw: object) -> str:
    """A package id that can name a worktree directory and a branch on every platform."""
    if (
        not isinstance(raw, str)
        or not PACKAGE_ID.fullmatch(raw)
        or raw.endswith(".")
        or ".." in raw
        or raw.lower().endswith(".lock")
        or WINDOWS_DEVICE.fullmatch(raw)
    ):
        raise WorktreeError(f"Package id {raw!r} cannot name a worktree and a branch.")
    return raw


def branch(raw: object) -> str:
    if (
        not isinstance(raw, str)
        or not BRANCH_NAME.fullmatch(raw)
        or ".." in raw
        or "@{" in raw
        or raw.endswith((".", "/", ".lock"))
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in raw.split("/"))
    ):
        raise WorktreeError("A bounded branch name is required.")
    return raw


def check_integration_branch(raw: object) -> str:
    """A local branch that can be the integration branch: never a SHA, a full ref, or conductor's own namespace
    (a package's branch checked out as the integration branch would let its stage commits move it unchecked)."""
    name = branch(raw)
    folded = name.casefold()
    if SHA.fullmatch(name) or name.startswith("refs/") or folded == "conductor" or folded.startswith(BRANCH_PREFIX):
        raise WorktreeError(
            f"{name!r} cannot be the integration branch: it must be a local branch name, not a SHA, a full ref, or a "
            f"name in conductor's own {BRANCH_PREFIX} namespace."
        )
    return name


def sha(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not SHA.fullmatch(raw):
        raise WorktreeError(f"A bounded {label} commit SHA is required.")
    return raw


def identity_arguments(root: str, identity: Sequence[str] | None = None) -> list[str]:
    """``-c user.name=… -c user.email=…`` for a commit: the workflow's identity, else the repository's, else
    conductor's own, so a commit never fails on an unconfigured machine."""
    name, email = (*identity, None, None)[:2] if identity else (None, None)
    if not name:
        name = _out(_git(root, ["config", "user.name"], timeout=15)) or DEFAULT_IDENTITY[0]
    if not email:
        email = _out(_git(root, ["config", "user.email"], timeout=15)) or DEFAULT_IDENTITY[1]
    if any(ch in str(value) for value in (name, email) for ch in "\x00\n\r"):
        name, email = DEFAULT_IDENTITY
    return ["-c", f"user.name={str(name)[:120]}", "-c", f"user.email={str(email)[:200]}"]


# ── repository shape ─────────────────────────────────────────────────────


def is_git_workspace(workspace: str) -> bool:
    """True when ``workspace`` is the top level of its own git checkout."""
    try:
        root = _directory(workspace, "workspace")
    except WorktreeError:
        return False
    if not os.path.lexists(os.path.join(root, ".git")):
        return False
    result = _git(root, ["rev-parse", "--show-toplevel"], timeout=15)
    if result.returncode != 0:
        return False
    return os.path.normcase(os.path.realpath(_out(result) or root)) == os.path.normcase(os.path.realpath(root))


def require_repository(workspace: str) -> str:
    """The repository's top level, canonical. A subdirectory or a linked worktree is refused."""
    root = os.path.realpath(_directory(workspace, "workspace"))
    if not is_git_workspace(root) or not os.path.isdir(os.path.join(root, ".git")):
        raise WorktreeError(f"{root} is not the top level of a git repository's main checkout.")
    return root


def current_branch(workspace: str) -> str | None:
    """The branch checked out in ``workspace``; None when detached or absent."""
    result = _git(workspace, ["symbolic-ref", "-q", "--short", "HEAD"], timeout=15)
    name = _out(result)
    return name if result.returncode == 0 and name and BRANCH_NAME.fullmatch(name) else None


def worktree_path(root: str, package: str) -> str:
    return os.path.join(_directory(root, "workspace"), *WORKTREE_PARENT.split("/"), package_id(package))


def branch_name(package: str) -> str:
    return BRANCH_PREFIX + package_id(package)


def common_git_dir(path: str) -> str:
    common = _checked(path, ["rev-parse", "--git-common-dir"], "Could not resolve the repository:", timeout=15)
    return os.path.abspath(common if os.path.isabs(common) else os.path.join(path, common))


def main_workspace(path: str) -> str:
    """The main checkout that owns the linked worktree at ``path``."""
    return os.path.dirname(common_git_dir(_directory(path, "worktree")))


def within(child: str, root: str) -> bool:
    try:
        common = os.path.commonpath([os.path.realpath(child), os.path.realpath(root)])
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(os.path.realpath(root))


def same_path(left: object, right: object) -> bool:
    try:
        return (
            os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))
            if left and right
            else False
        )
    except (TypeError, ValueError):
        return False


def linked_worktree(path: str, root: str | None = None) -> str:
    """Prove the path is a linked worktree before any mutating git command.

    A directory check alone is not enough: ``git -C <any directory in the repo> reset --hard`` discovers the
    enclosing repository and destroys uncommitted work in the main checkout. A linked worktree has a ``.git``
    *file*, is its own top level, and is registered in the owning repository. Its real path must be exactly
    ``<root>/.conductor/worktrees/<id>``, with no linked parent. Plain subdirectories, the main checkout, moved
    worktrees, foreign repositories and missing markers are refused before an operation can change anything.
    """
    worktree = _directory(path, "worktree")
    if not os.path.isfile(os.path.join(worktree, ".git")):
        raise WorktreeError(f"{worktree} is not a package worktree: its .git marker is missing or is a repository.")
    owner = os.path.realpath(root or os.path.dirname(os.path.dirname(os.path.dirname(worktree))))
    if not os.path.isdir(os.path.join(owner, ".git")):
        raise WorktreeError(f"{owner} is not a main repository checkout.")
    expected = os.path.join(worktree_parent(owner), os.path.basename(worktree))
    if is_link(worktree) or not same_path(worktree, expected) or not same_path(os.path.realpath(worktree), expected):
        raise WorktreeError(f"{worktree} is not its canonical worktree path inside {owner}.")
    toplevel = _git(worktree, ["rev-parse", "--show-toplevel"], timeout=15)
    if toplevel.returncode != 0:
        raise WorktreeError(f"{worktree} is not inside a git repository: {_err(toplevel)}")
    resolved = _out(toplevel)
    if not resolved or os.path.normcase(os.path.realpath(resolved)) != os.path.normcase(os.path.realpath(worktree)):
        raise WorktreeError(f"{worktree} is not the top level of its own worktree; refusing to operate on it.")
    if not same_path(common_git_dir(worktree), os.path.join(owner, ".git")):
        raise WorktreeError(f"{worktree} is not a linked worktree of {owner}.")
    registered = registered_worktrees(owner)
    if os.path.normcase(expected) not in registered or os.path.normcase(owner) not in registered:
        raise WorktreeError(f"{worktree} is not registered as a linked worktree of {owner}.")
    return worktree


def registered_worktrees(root: str) -> dict[str, str]:
    """Canonical worktree directory → checked-out branch ("" when detached), from ``git worktree list -z``.

    NUL-separated, so a path is read exactly as git stores it, without quoting.
    """
    result = _git(root, ["worktree", "list", "--porcelain", "-z"], timeout=30)
    if result.returncode != 0:
        raise WorktreeError(f"Could not list worktrees: {_err(result)}")
    found: dict[str, str] = {}
    directory = None
    for field in os.fsdecode(result.stdout or b"").split("\0"):
        if field.startswith("worktree "):
            directory = os.path.normcase(os.path.realpath(field[len("worktree ") :]))
            found[directory] = ""
        elif field.startswith("branch ") and directory is not None:
            found[directory] = field[len("branch ") :].replace("refs/heads/", "", 1)
        elif not field:
            directory = None
    return found


def _state_file(root: str, package: str) -> str:
    return os.path.join(common_git_dir(root), STATE_DIRECTORY, f"{package}.json")


def _read_state(root: str, package: str) -> dict | None:
    try:
        with open(_state_file(root, package), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, WorktreeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(root: str, package: str, value: dict) -> None:
    """Replace worktree metadata whole, so recovery sees either side of a provisioning or refresh marker."""
    path = _state_file(root, package)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f"{path}.{os.getpid()}.{threading.get_ident()}.partial"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(partial, path)


def _delete_state(root: str, package: str, target: str) -> None:
    """Drop a removed worktree's state so it can never vouch for a later directory at that path."""
    state = _read_state(root, package)
    if state and same_path(state.get("path"), target):
        try:
            os.remove(_state_file(root, package))
        except OSError:
            pass


def _exclude(root: str, patterns: Sequence[str]) -> None:
    """Add patterns to the clone's ``info/exclude`` (shared by every worktree), never to a tracked file."""
    raw = _checked(root, ["rev-parse", "--git-path", "info/exclude"], "Could not find the exclude file:", timeout=15)
    exclude = raw if os.path.isabs(raw) else os.path.join(root, raw)
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    try:
        with open(exclude, "rb") as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = b""
    present = set(existing.decode("utf-8", errors="replace").splitlines())
    missing = [pattern for pattern in patterns if pattern not in present]
    if missing:
        with open(exclude, "ab") as handle:
            separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
            handle.write(separator + "".join(f"{pattern}\n" for pattern in missing).encode("utf-8"))


def exclude_conductor(root: str) -> None:
    """Keep ``.conductor/`` out of the repository."""
    if _git(root, ["check-ignore", "-q", "--", ".conductor/worktrees/x"], timeout=15).returncode != 0:
        _exclude(root, [".conductor/"])


def worktree_parent(root: str) -> str:
    """``<root>/.conductor/worktrees``, refused when ``.conductor`` or ``worktrees`` is a link.

    Everything conductor creates, runs in or deletes under it must be where it appears to be: a linked parent would
    send a provision, a setup command or a recursive delete somewhere else.
    """
    current = root
    for part in WORKTREE_PARENT.split("/"):
        current = os.path.join(current, part)
        if is_link(current):
            raise WorktreeError(
                f"{current} is a link; conductor will not create, run or delete anything through it.",
                {"foreign": True, "path": current},
            )
    return os.path.join(root, *WORKTREE_PARENT.split("/"))


def worktree_owner(root: str, package: str) -> str | None:
    """The run that provisioned the package's worktree, from its state file; None when nothing records one."""
    state = _read_state(root, package_id(package))
    return state.get("run_id") if state else None


# ── links ────────────────────────────────────────────────────────────────


def is_link(path: str) -> bool:
    """A symlink, or on Windows any reparse point (a junction someone else made counts too)."""
    try:
        entry = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(entry, "st_reparse_tag", 0)) or stat.S_ISLNK(entry.st_mode)


def _create_link(target: str, link: str) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        hint = " On Windows, enable Developer Mode or run elevated to create symlinks." if os.name == "nt" else ""
        raise WorktreeError(f"Could not link {link} to {target}: {type(exc).__name__}.{hint}") from exc


def _unlink(path: str) -> None:
    """Remove the link itself, never what it points at."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)  # a Windows directory link


def unlink_links(path: str) -> list[str]:
    """Unlink, never delete through, every link inside ``path``."""
    unlinked: list[str] = []
    if not os.path.isdir(path):
        return unlinked
    if is_link(path):
        raise WorktreeError("The worktree path is itself a link; refusing to remove it.")
    for seen, (parent, directories, files) in enumerate(os.walk(path, topdown=True), start=1):
        if seen > MAX_WALK_ENTRIES:
            raise WorktreeError("The worktree is too large to scan for links safely.")
        kept = []
        for name in directories:
            candidate = os.path.join(parent, name)
            if is_link(candidate):
                try:
                    _unlink(candidate)
                except OSError as exc:
                    raise WorktreeError(f"Could not unlink {candidate}: {type(exc).__name__}") from exc
                unlinked.append(candidate)
            else:
                kept.append(name)
        directories[:] = kept
        for name in files:
            candidate = os.path.join(parent, name)
            if is_link(candidate):
                _unlink(candidate)
                unlinked.append(candidate)
    return unlinked


def _assert_no_links(path: str) -> None:
    for parent, directories, files in os.walk(path, topdown=True):
        for name in [*directories, *files]:
            if is_link(os.path.join(parent, name)):
                raise WorktreeError(f"A link remains under {parent}; refusing a recursive delete.")


def _link_manifest_directories(root: str, path: str, manifest: dict | None) -> None:
    """Link each manifest ``link`` entry of the main checkout into the worktree (``.venv``, ``node_modules``).

    Each link's path is excluded by name. A ``node_modules/`` pattern matches a directory, not a symlink, so without
    this ``git add -A`` would commit the link and ``git clean`` would delete it.
    """
    entries = [str(entry).replace("\\", "/").strip("/") for entry in (manifest or {}).get("link") or []]
    if entries:
        _exclude(root, [f"/{entry}" for entry in entries if entry])
    for entry in entries:
        relative = entry
        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise WorktreeError(f"Environment link entry is not a contained relative directory: {entry}")
        target = os.path.abspath(os.path.join(root, *relative.split("/")))
        if not within(target, root):
            raise WorktreeError(f"Environment link entry resolves outside the repository: {entry}")
        if not os.path.isdir(target):
            raise WorktreeError(f"Environment link entry does not exist in the repository: {entry}")
        link = os.path.abspath(os.path.join(path, *relative.split("/")))
        if os.path.lexists(link):
            raise WorktreeError(f"Environment link entry already exists in the worktree: {entry}")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        _create_link(target, link)


class SetupCancelled(WorktreeError):
    """A Stop arrived while the environment's setup commands ran. The half-set-up worktree is removed."""


def _run_setup(path: str, package: str, manifest: dict | None, cancel: threading.Event | None = None) -> dict | None:
    """The manifest's setup commands in the new worktree, with the checks' environment. ``cancel`` stops them."""
    setup = (manifest or {}).get("setup") or []
    if not setup:
        return None
    from . import checks

    receipt = checks.run_checks(
        f"setup:{package}", path, setup, allowlist=(manifest or {}).get("env"), cancel=cancel, require_git=False
    )
    if receipt["cancelled"]:
        raise SetupCancelled(f"Environment setup for {package} was stopped; the worktree was removed.")
    return receipt


# ── operations ───────────────────────────────────────────────────────────


def provision(
    root: str,
    package: str,
    integration_branch: str,
    manifest: dict | None = None,
    *,
    fresh: bool = False,
    preserve_label: str | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Create the package branch at the integration tip, its worktree, its links, and run setup.

    An existing worktree on the same branch and base, provisioned by the same run, is reused (a resumed attempt
    continues in it). ``fresh`` recreates it at the current tip: the rework after a conflict starts over on what
    landed. The run and fresh attempt are recorded before dispatch, so repeating that attempt reuses its replacement.
    A branch that carries commits is renamed out of the way first, never reset, so no stage commit is lost.
    ``cancel`` stops the setup commands (``SetupCancelled``).
    """
    package = package_id(package)
    integration = check_integration_branch(integration_branch)
    root = require_repository(root)
    name = branch_name(package)
    worktree_parent(root)
    path = worktree_path(root, package)
    exclude_conductor(root)
    base_sha = _checked(
        root,
        ["rev-parse", "--verify", f"refs/heads/{integration}^{{commit}}"],
        f"Integration branch {integration} has no commit:",
        timeout=15,
    )
    registered = registered_worktrees(root)
    canonical = os.path.normcase(os.path.realpath(path)) if os.path.isdir(path) else ""
    state = _read_state(root, package)
    owned = state is not None and (run_id is None or state.get("run_id") == run_id)
    if fresh and owned and run_id is not None and attempt is not None and state.get("fresh_attempt") == attempt:
        fresh = False  # This attempt already replaced the old tree before its process died.
    if canonical and not fresh and owned and registered.get(canonical) == name and same_path(state.get("path"), path):
        linked_worktree(path, root)
        head = _checked(path, ["rev-parse", "HEAD"], "Could not read the worktree head:", timeout=15)
        return {
            "path": path,
            "branch": name,
            "base_sha": state.get("base_sha"),
            "head_sha": head,
            "setup": None,
            "created": False,
            "integration_branch": integration,
        }
    if canonical or (state and os.path.isdir(path)):
        remove(path)
    _git(root, ["worktree", "prune"], timeout=30)
    preserved = _preserve_existing_branch(root, name, base_sha, preserve_label)
    _checked(
        root,
        ["worktree", "add", "-B", name, path, base_sha],
        f"Could not create the worktree for {package}:",
        timeout=180,
    )
    try:
        _link_manifest_directories(root, path, manifest)
        setup = _run_setup(path, package, manifest, cancel)
        if setup is not None and not setup.get("passed"):
            raise WorktreeError(
                f"Environment setup failed while provisioning {package}; the worktree was removed.", {"setup": setup}
            )
    except WorktreeError:
        try:
            remove(path)
        except WorktreeError:
            pass
        raise
    head = _checked(path, ["rev-parse", "HEAD"], "Could not read the worktree head:", timeout=15)
    _write_state(
        root,
        package,
        {
            "package_id": package,
            "branch": name,
            "base_sha": base_sha,
            "path": path,
            "integration_branch": integration,
            "run_id": run_id,
            "fresh_attempt": attempt if fresh else None,
        },
    )
    return {
        "path": path,
        "branch": name,
        "base_sha": base_sha,
        "head_sha": head,
        "setup": setup,
        "created": True,
        "integration_branch": integration,
        "preserved_branch": preserved,
    }


def _preserve_existing_branch(root: str, name: str, base_sha: str, label: str | None) -> str | None:
    """Rename a package branch that still carries commits before it is recreated.

    ``git worktree add -B`` resets the branch to the base, so a branch kept from an earlier attempt would lose
    its history. It is renamed instead (``conductor/C@attempt-1``); nothing is deleted.
    """
    existing = _git(root, ["rev-parse", "--verify", f"refs/heads/{name}^{{commit}}"], timeout=15)
    if existing.returncode != 0 or _out(existing) == base_sha:
        return None
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", label or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    for suffix in ("", *(f".{n}" for n in range(1, 100))):
        candidate = f"{name}@{stem}{suffix}"
        if _git(root, ["rev-parse", "--verify", f"refs/heads/{candidate}"], timeout=15).returncode != 0:
            _checked(root, ["branch", "-m", name, candidate], f"Could not preserve {name}:", timeout=30)
            return candidate
    raise WorktreeError(f"Could not find a free name to preserve {name}.")


def capture(path: str, since_sha: str, to_sha: str | None = None) -> dict:
    """The patch receipt for what the worktree gained since ``since_sha``: its hash, its files, and the patch
    itself when it is small enough. With no ``to_sha`` it is the working tree."""
    worktree = linked_worktree(path)
    since = sha(since_sha, "base")
    endpoints = [since] if to_sha is None else [since, sha(to_sha, "target")]
    diff = _git(worktree, ["diff", "--binary", *endpoints], timeout=120)
    if diff.returncode != 0:
        raise WorktreeError(f"Could not capture the patch receipt: {_err(diff)}")
    raw = diff.stdout or b""
    files = _names(_git(worktree, ["diff", "--name-only", "-z", *endpoints], timeout=60))
    untracked = _names(_git(worktree, ["ls-files", "--others", "--exclude-standard", "-z"], timeout=60))
    truncated = len(raw) > MAX_PATCH_BYTES
    return {
        "since_sha": since,
        "to_sha": to_sha,
        "patch_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "files": files[:MAX_LISTED_FILES],
        "untracked": untracked[:MAX_LISTED_FILES],
        "truncated": truncated,
        "patch": None if truncated else raw.decode("utf-8", errors="replace"),
    }


def commit(path: str, stage: str, dispatch_key: str, title: str, identity: Sequence[str] | None = None) -> dict:
    """Commit whatever the stage left, with the dispatch key in the message.

    Idempotent per dispatch key: a stage run again after a lost result finds its own commit at HEAD and returns
    it, rather than reporting an empty stage over a commit that exists.
    """
    worktree = linked_worktree(path)
    match = DISPATCH_KEY.fullmatch(str(dispatch_key))
    if not match:
        raise WorktreeError("A dispatch key is required to commit a stage.")
    run_id, package, key_stage, run_number = match.groups()
    if str(stage) != key_stage:
        raise WorktreeError("The stage does not match its dispatch key.")
    previous = _checked(worktree, ["rev-parse", "HEAD"], "Could not read the worktree head:", timeout=15)
    key_line = f"Dispatch-Key: {dispatch_key}"
    existing = _git(worktree, ["log", "-1", "--format=%B", "HEAD"], timeout=15)
    if existing.returncode == 0 and key_line in os.fsdecode(existing.stdout or b"").splitlines():
        parent = _git(worktree, ["rev-parse", "--verify", "HEAD^"], timeout=15)
        base = _out(parent) if parent.returncode == 0 else EMPTY_TREE
        return {
            "commit_sha": previous,
            "empty": False,
            "replayed": True,
            "head_sha": previous,
            "patch": capture(worktree, base, previous),
        }
    _checked(worktree, ["add", "-A"], "Could not stage the worktree:", timeout=120)
    staged = _git(worktree, ["diff", "--cached", "--quiet"], timeout=60)
    if staged.returncode == 0:
        return {
            "commit_sha": None,
            "empty": True,
            "replayed": False,
            "head_sha": previous,
            "patch": capture(worktree, previous),
        }
    subject = f"{package} {key_stage} {run_number}"
    body = f"{key_line}\nRun: {run_id}\nPackage: {' '.join(str(title).split())[:200]}"
    _checked(
        worktree,
        [*identity_arguments(worktree, identity), "commit", "-m", subject, "-m", body],
        f"Could not commit the {key_stage} stage:",
        timeout=120,
    )
    head = _checked(worktree, ["rev-parse", "HEAD"], "Could not read the stage commit:", timeout=15)
    return {
        "commit_sha": head,
        "empty": False,
        "replayed": False,
        "head_sha": head,
        "patch": capture(worktree, previous, head),
    }


def changed_files(path: str, integration_branch: str) -> list[str]:
    """What the package branch changes against the integration branch: ``git diff integration...HEAD``."""
    worktree = linked_worktree(path)
    integration = branch(integration_branch)
    result = _git(worktree, ["diff", "--name-only", "--no-renames", "-z", f"{integration}...HEAD"], timeout=60)
    if result.returncode != 0:
        raise WorktreeError(f"Could not list the package's changes: {_err(result)}")
    return _names(result)


def commits_since(path: str, base_sha: str) -> int:
    worktree = linked_worktree(path)
    count = _out(_git(worktree, ["rev-list", "--count", f"{sha(base_sha, 'base')}..HEAD"], timeout=30))
    return int(count) if count.isdigit() else 0


def reset(path: str, to_sha: str) -> dict:
    """Keep the discarded work as evidence, then restore ``to_sha``. ``clean`` never takes ``-x``."""
    worktree = linked_worktree(path)
    target = sha(to_sha, "accepted")
    flags = hidden_index_paths(worktree)
    for start in range(0, len(flags), 200):
        _checked(
            worktree,
            ["update-index", "--no-assume-unchanged", "--no-skip-worktree", "--", *flags[start : start + 200]],
            "Could not clear hidden index flags:",
        )
    evidence = capture(worktree, target)
    _checked(worktree, ["reset", "--hard", target], "Could not restore the accepted commit:", timeout=120)
    _checked(worktree, ["clean", "-fd"], "Could not clear the discarded files:", timeout=120)
    return {**evidence, "reset_to": target}


def _abort(cwd: str, args: Sequence[str]) -> None:
    """Best effort. A failed abort must not mask the original failure."""
    try:
        _git(cwd, args, timeout=120)
    except WorktreeError:
        pass


def unmerged_paths(cwd: str) -> list[str]:
    """Paths a stopped merge left conflicted. Best effort: an unreadable index lists none."""
    try:
        result = _git(cwd, ["diff", "--name-only", "--diff-filter=U", "-z"], timeout=60)
    except WorktreeError:
        return []
    return _names(result)[:MAX_LISTED_FILES] if result.returncode == 0 else []


def stage_base(path: str, run_id: str, package: str, attempt: int) -> str:
    """Keep the attempt's pre-agent HEAD before dispatch, so a lost commit result cannot skip scope cleanup."""
    worktree = linked_worktree(path)
    if not RUN_ID.fullmatch(run_id) or not isinstance(attempt, int) or attempt < 1:
        raise WorktreeError("A run and a positive attempt are required for a stage base.")
    ref = f"refs/conductor/stage-base/{run_id}/{package_id(package)}/{attempt}"
    existing = _git(worktree, ["rev-parse", "--verify", ref], timeout=15)
    if existing.returncode == 0:
        return sha(_out(existing), "stage base")
    base = head(worktree)
    _checked(worktree, ["update-ref", ref, base], "Could not keep the stage base:")
    return base


def verification_base(path: str, stage_head: str) -> str:
    """The implement head or its recorded refresh. Recover a merge result only from its saved parent pair.

    The intent is saved before merging and the result afterwards. An unrelated reviewer commit can never become
    the baseline just because verification resumed after it.
    """
    worktree = linked_worktree(path)
    stage = sha(stage_head, "stage")
    current = head(worktree)
    state = _read_state(main_workspace(worktree), os.path.basename(worktree)) or {}
    refresh = state.get("refresh") or {}
    if refresh.get("stage_head") == stage and not refresh.get("head") and current == refresh.get("from"):
        pending = _out(_git(worktree, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], timeout=15))
        if pending == refresh.get("onto"):
            _abort(worktree, ["merge", "--abort"])
    if current == stage or refresh.get("stage_head") != stage:
        return stage
    if refresh.get("head"):
        return refresh["head"]
    parents = _out(_git(worktree, ["rev-list", "--parents", "-n", "1", current], timeout=15)).split()
    if parents == [current, refresh["from"], refresh["onto"]]:
        refresh["head"] = current
        _write_state(main_workspace(worktree), os.path.basename(worktree), state)
        return current
    return refresh["from"]


def start_session(path: str, stage: str, session_id: str) -> bool:
    """Record a Claude dispatch before it starts. A reserved id alone does not mean its process has run."""
    worktree = linked_worktree(path)
    root, package = main_workspace(worktree), os.path.basename(worktree)
    state = _read_state(root, package) or {}
    sessions = state.setdefault("sessions", {})
    resumed = sessions.get(stage) == session_id
    sessions[stage] = session_id
    _write_state(root, package, state)
    return resumed


def refresh_base(
    path: str, integration_branch: str, identity: Sequence[str] | None = None, *, stage_head: str | None = None
) -> dict:
    """Bring the package branch up to date by merging the integration branch into it.

    A merge, never a rebase: a rebase rewrites the stage commits whose SHAs the receipts name, while a merge
    keeps every one of them an ancestor of the branch. A conflict aborts the merge and raises with its paths;
    the branch is left where it was.
    """
    worktree = linked_worktree(path)
    if stage_head is not None and _checked(
        worktree, ["rev-parse", "HEAD"], "Could not read HEAD:"
    ) != verification_base(worktree, stage_head):
        raise WorktreeError("The worktree no longer holds the implement head or its recorded refresh.")
    integration = branch(integration_branch)
    new_base = _checked(
        worktree,
        ["rev-parse", "--verify", f"refs/heads/{integration}^{{commit}}"],
        f"Integration branch {integration} has no commit:",
        timeout=15,
    )
    head = _checked(worktree, ["rev-parse", "HEAD"], "Could not read the worktree head:", timeout=15)
    pending = _git(worktree, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], timeout=15)
    if pending.returncode == 0:
        # A refresh interrupted part-way (the run stopped mid-merge) is aborted, never left half-applied.
        _abort(worktree, ["merge", "--abort"])
    old_base = _checked(worktree, ["merge-base", "HEAD", new_base], "Could not find the branch point:", timeout=30)
    if old_base == new_base:
        return {"refreshed": False, "old_base": old_base, "new_base": new_base, "head_sha": head}
    message = f"Refresh {current_branch(worktree) or 'HEAD'} from {integration} at {new_base[:12]}"
    state = _read_state(main_workspace(worktree), os.path.basename(worktree)) or {}
    if stage_head is not None:
        state["refresh"] = {"stage_head": stage_head, "from": head, "onto": new_base}
        _write_state(main_workspace(worktree), os.path.basename(worktree), state)
    failure = None
    try:
        result = _git(
            worktree,
            [*identity_arguments(worktree, identity), "merge", "--no-ff", "--no-edit", "-m", message, new_base],
            timeout=300,
        )
        if result.returncode != 0:
            failure = _err(result)
    except WorktreeError as exc:
        # A timeout leaves a merge in progress just as a conflict does; the abort runs either way.
        failure = str(exc)
    if failure is not None:
        paths = unmerged_paths(worktree)
        _abort(worktree, ["merge", "--abort"])
        restored = _out(_git(worktree, ["rev-parse", "HEAD"], timeout=15)) or head
        conflicting = f" Conflicting paths: {', '.join(paths[:10])}." if paths else ""
        raise WorktreeError(
            f"The package branch does not merge cleanly with {integration}; its checks did not run. "
            f"Base moved {old_base[:12]} -> {new_base[:12]}.{conflicting} {failure}"[:2000],
            {"old_base": old_base, "new_base": new_base, "head_sha": restored, "paths": paths, "conflict": True},
        )
    merged = _checked(worktree, ["rev-parse", "HEAD"], "Could not read the merged head:", timeout=15)
    if stage_head is not None:
        state["refresh"]["head"] = merged
        _write_state(main_workspace(worktree), os.path.basename(worktree), state)
    return {
        "refreshed": True,
        "old_base": old_base,
        "new_base": new_base,
        "head_sha": merged,
    }


def untracked_collisions(root: str, base_sha: str, target_sha: str) -> list[str]:
    """Paths a move from ``base_sha`` to ``target_sha`` would write over a file the main checkout keeps untracked
    or ignored. Checked before anything moves, so nothing is touched when the answer is "do not"."""
    names = _git(
        root, ["diff", "--name-only", "--no-renames", "--diff-filter=AM", "-z", base_sha, target_sha], timeout=60
    )
    if names.returncode != 0:
        raise WorktreeError(f"Could not compare {base_sha[:12]} with {target_sha[:12]}: {_err(names)}")
    present = set()
    for name in _names(names)[:20_000]:
        parts = name.split("/")
        for end in range(1, len(parts) + 1):
            candidate = os.path.join(root, *parts[:end])
            if os.path.lexists(candidate) and (end == len(parts) or not os.path.isdir(candidate) or is_link(candidate)):
                present.add("/".join(parts[:end]))
    present = sorted(present)
    collisions: list[str] = []
    for start in range(0, len(present), 200):
        chunk = present[start : start + 200]
        listed = _git(root, ["ls-files", "-z", "--", *chunk], timeout=60)
        if listed.returncode != 0:
            raise WorktreeError(f"Could not read the tracked file list: {_err(listed)}")
        tracked = set(_names(listed))
        collisions.extend(name for name in chunk if name not in tracked)
    return sorted(collisions)


def staged_paths(root: str) -> list[str]:
    if _git(root, ["diff", "--cached", "--quiet"], timeout=60).returncode == 0:
        return []
    names = _git(root, ["diff", "--cached", "--name-only", "-z"], timeout=60)
    if names.returncode != 0:
        raise WorktreeError(f"Could not read the staged file list: {_err(names)}")
    return _names(names)


def modified_paths(root: str, paths: Sequence[str]) -> list[str]:
    """Which of ``paths`` are tracked and carry uncommitted edits in the main checkout."""
    modified: list[str] = []
    for start in range(0, len(paths), 200):
        chunk = list(paths[start : start + 200])
        result = _git(root, ["status", "--porcelain", "-z", "--untracked-files=no", "--", *chunk], timeout=60)
        if result.returncode != 0:
            raise WorktreeError(f"Could not read the status of the landing's paths: {_err(result)}")
        records = [os.fsdecode(record) for record in (result.stdout or b"").split(b"\0") if record]
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if record[:1] in "RC":
                index += 1  # the rename source follows as its own record
            modified.append(record[3:].replace("\\", "/"))
    return sorted(set(modified))


def hidden_index_paths(path: str) -> list[str]:
    """Tracked paths whose index flags suppress content inspection. Status alone cannot certify these files."""
    result = _git(path, ["ls-files", "-v", "-z"], timeout=60)
    if result.returncode != 0:
        raise WorktreeError(f"Could not read the index flags: {_err(result)}")
    return [
        os.fsdecode(record[2:])
        for record in (result.stdout or b"").split(b"\0")
        if record and (record[:1].islower() or record[:1] == b"S")
    ]


def is_clean(path: str) -> bool:
    """True when status is empty and no index flag hides contents. Ignored caches and build output do not count."""
    result = _git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], timeout=120)
    if result.returncode != 0:
        raise WorktreeError(f"Could not read the worktree status: {_err(result)}")
    return not result.stdout and not hidden_index_paths(path)


def head(path: str) -> str:
    return _checked(path, ["rev-parse", "HEAD"], "Could not read HEAD:", timeout=15)


def remove(path: str) -> dict:
    """Unlink every link, then remove the worktree. Its branch is kept.

    Only a directory directly under a git repository's ``.conductor/worktrees/`` may be removed, and only when its
    real path (``realpath``) is exactly that place: no link anywhere on the way, the worktree itself, ``worktrees``
    or ``.conductor``. Without those rules a stale or linked path could point anywhere, and the recursive fallback
    below would delete it.
    """
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise WorktreeError("A bounded worktree path is required.")
    target = os.path.abspath(path.strip())
    parent = os.path.normcase(os.path.dirname(target)).replace(os.sep, "/")
    if not parent.endswith("/" + WORKTREE_PARENT):
        raise WorktreeError(f"Only a directory under {WORKTREE_PARENT}/ can be removed as a worktree: {target}")
    if not os.path.lexists(target):
        return {"removed": False, "unlinked": [], "path": target}
    owner = os.path.dirname(os.path.dirname(os.path.dirname(target)))
    if not is_git_workspace(owner):
        raise WorktreeError(
            f"{target} is not in a git repository's {WORKTREE_PARENT}/; it was left in place.",
            {"foreign": True, "path": target},
        )
    worktree_parent(owner)
    canonical = os.path.realpath(target)
    expected = os.path.join(os.path.realpath(owner), *WORKTREE_PARENT.split("/"), os.path.basename(target))
    if is_link(target) or os.path.normcase(canonical) != os.path.normcase(expected):
        raise WorktreeError(
            f"{target} does not resolve to itself inside {owner}; it was left in place.",
            {"foreign": True, "path": target},
        )
    if not os.path.isdir(canonical):
        return {"removed": False, "unlinked": [], "path": target}
    unlinked = unlink_links(canonical)
    if os.path.normcase(canonical) in registered_worktrees(owner):
        _checked(owner, ["worktree", "remove", "--force", canonical], "Could not remove the worktree:", timeout=180)
    _git(owner, ["worktree", "prune"], timeout=60)
    if os.path.isdir(canonical):
        # Only reachable for a directory git no longer lists as a worktree, already proved to be inside the repository.
        _assert_no_links(canonical)
        shutil.rmtree(canonical, ignore_errors=False)
    _delete_state(owner, os.path.basename(target), target)
    return {"removed": True, "unlinked": unlinked, "path": target}


def delete_branch_if_kept(root: str, name: str, keeper_ref: str) -> bool:
    """Delete a package branch only when ``keeper_ref`` points at its tip, so no commit becomes unreachable."""
    tip = _git(root, ["rev-parse", "-q", "--verify", f"refs/heads/{branch(name)}"], timeout=15)
    kept = _git(root, ["rev-parse", "-q", "--verify", keeper_ref], timeout=15)
    if tip.returncode != 0 or kept.returncode != 0 or _out(tip) != _out(kept):
        return False
    return _git(root, ["update-ref", "-d", f"refs/heads/{name}", _out(tip)], timeout=30).returncode == 0


# ── leases ───────────────────────────────────────────────────────────────


class LeaseTimeout(TimeoutError):
    """Another holder kept the tree for longer than the caller would wait."""


class LeaseManager:
    """Writer leases on directory trees, one holder each.

    Two keys conflict when one contains the other. Sibling package worktrees stay parallel; the main checkout's
    git directory, held by a landing, excludes nothing but itself, and the repository root, if anything held it,
    would exclude every worktree inside it.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._owners: dict[str, str] = {}

    @staticmethod
    def key(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    @staticmethod
    def _conflict(first: str, second: str) -> bool:
        if first == second:
            return True
        try:
            common = os.path.commonpath([first, second])
        except ValueError:
            return False
        return common in (first, second)

    def acquire(self, path: str, holder: str, timeout: float | None = None) -> bool:
        key = self.key(path)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while any(owner != holder and self._conflict(key, held) for held, owner in self._owners.items()):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=0.25 if remaining is None else min(0.25, remaining))
            self._owners[key] = holder
            return True

    def release(self, holder: str) -> None:
        with self._condition:
            for key in [key for key, owner in self._owners.items() if owner == holder]:
                del self._owners[key]
            self._condition.notify_all()

    def held(self) -> dict[str, str]:
        with self._condition:
            return dict(self._owners)

    @contextmanager
    def lease(self, path: str, holder: str, timeout: float | None = 120.0) -> Iterator[str]:
        if not self.acquire(path, holder, timeout):
            raise LeaseTimeout(f"Another holder keeps {path}; {holder} did not start.")
        try:
            yield holder
        finally:
            self.release(holder)
