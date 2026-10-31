"""Who may run where, and who says yes.

**Dispatch roots.** Conductor works only inside directories the operator granted. Static roots come from
``CONDUCTOR_DISPATCH_ROOTS`` (``os.pathsep``-separated). Grants come from ``conductor grant <repo>``, which an
approver must approve. Every directory conductor runs anything in (the repository, each worktree, the integration
worktree) is canonicalized and must lie inside a root; a path that reaches its target through a symlink is refused
outright, so a link cannot smuggle work outside the roots.

**Grants survive a restart, for as long as a project lives in them.** A grant is recorded in ``grants.json`` under
the conductor home (``CONDUCTOR_HOME``, default ``~/.conductor``). Each process start restores the record: every
root is canonicalized again, and kept only while a registered project (a repository conductor has run in, not
archived) lies inside it. A root that vanished, now resolves through a link, or holds no project is dropped from the
record. At most 64 roots; a malformed or oversized record grants nothing. Only an approved grant is recorded:
running in a repository under a static root registers the project but never records a grant.

**Approvers.** An approval is bound to the SHA-256 fingerprint of exactly what it approves (a plan, a grant, one
package's landing head); a changed plan needs a new approval. Two approvers ship: :class:`CLIApprover` asks the
person at the terminal and refuses when there is no terminal (an agent's stdin ends after its brief), and
:class:`PolicyApprover` approves from an allow-list file kept outside the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import IO, Any, Protocol

MAX_GRANTED_ROOTS = 64
MAX_RECORD_BYTES = 8 * 1024 * 1024
GRANTS_FILE = "grants.json"
PROJECTS_FILE = "projects.json"
TIER_ORDER = ("T0", "T1", "T2", "T3")


class PermissionRefused(ValueError):
    """A path outside the dispatch roots, or an approval that was not given."""


def conductor_home() -> str:
    return os.path.abspath(os.environ.get("CONDUCTOR_HOME") or os.path.join(os.path.expanduser("~"), ".conductor"))


def canonical_directory(raw: object) -> str:
    """An absolute, existing directory that is already canonical. Fail closed.

    The supplied path must not traverse a symlink or junction: ``realpath`` must change nothing but case.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PermissionRefused("A directory path is required.")
    candidate = raw.strip()
    if "\x00" in candidate:
        raise PermissionRefused("The path contains a NUL byte.")
    if not os.path.isabs(candidate):
        raise PermissionRefused("The directory must be an absolute path.")
    absolute = os.path.abspath(candidate)
    canonical = os.path.realpath(absolute)
    if os.path.normcase(canonical) != os.path.normcase(absolute):
        raise PermissionRefused(f"{absolute} resolves through a link; pass the canonical path {canonical}.")
    if not os.path.isdir(canonical):
        raise PermissionRefused(f"{canonical} is not an existing directory.")
    return canonical


def within_root(child: str, root: str) -> bool:
    child, root = os.path.normcase(child), os.path.normcase(root)
    if child == root:
        return True
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def _read_record(path: str, field_name: str, limit: int) -> list[str]:
    """The list a version 1 record holds; none when it is missing, oversized or not that shape."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return []
    try:
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("over its size limit")
        record = json.loads(raw.decode("utf-8"))
        version = record.get("version") if isinstance(record, dict) else None
        items = record.get(field_name) if type(version) is int and version == 1 else None
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError("not a version 1 list")
    except ValueError:
        return []
    return items[:limit]


def _write_record(path: str, field_name: str, items: Sequence[str]) -> bool:
    """Rewrite a record whole and rename it over the last one. Never replaces a readable record with one the
    reader would refuse."""
    data = json.dumps({"version": 1, field_name: list(items)}, ensure_ascii=False, indent=2).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f"{path}.{os.getpid()}.{threading.get_ident()}.partial"
    with open(partial, "wb") as handle:
        handle.write(data)
    os.replace(partial, path)
    return True


@contextmanager
def _records_locked(home: str, timeout: float = 10.0) -> Iterator[None]:
    """One process at a time reads, changes and writes the grant and project records, so two conductor processes
    (in different repositories) never write over each other's change."""
    from .store import FileLock

    lock = FileLock(os.path.join(home, "records.lock"))
    deadline = time.monotonic() + timeout
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise PermissionRefused("Another conductor process kept the grant records locked; try again.")
        time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


class ProjectRegistry:
    """Repositories conductor has run in. A project keeps its folder's grant alive across restarts."""

    def __init__(self, home: str | None = None):
        self.home = home or conductor_home()
        self.path = os.path.join(self.home, PROJECTS_FILE)

    def projects(self) -> list[str]:
        return _read_record(self.path, "projects", 10_000)

    def add(self, root: str) -> None:
        canonical = canonical_directory(root)
        with _records_locked(self.home):
            current = self.projects()
            if not any(os.path.normcase(p) == os.path.normcase(canonical) for p in current):
                _write_record(self.path, "projects", sorted([*current, canonical], key=os.path.normcase))

    def archive(self, root: str) -> bool:
        key = os.path.normcase(os.path.abspath(root))
        with _records_locked(self.home):
            current = self.projects()
            kept = [p for p in current if os.path.normcase(p) != key]
            if len(kept) == len(current):
                return False
            _write_record(self.path, "projects", kept)
            return True


class GrantStore:
    """Static roots, granted roots, and the record that lets a grant outlive the process that made it."""

    def __init__(self, home: str | None = None, static_roots: Iterable[str] | None = None):
        self.home = home or conductor_home()
        self.path = os.path.join(self.home, GRANTS_FILE)
        self._lock = threading.Lock()
        self._granted: dict[str, str] = {}  # normcased → canonical
        self._recorded: dict[str, str] = {}
        raw = (
            static_roots
            if static_roots is not None
            else os.environ.get("CONDUCTOR_DISPATCH_ROOTS", "").split(os.pathsep)
        )
        self.static_roots: list[str] = []
        for entry in raw:
            try:
                self.static_roots.append(os.path.realpath(canonical_directory(os.path.expanduser(entry))))
            except PermissionRefused:
                continue

    def roots(self) -> list[str]:
        with self._lock:
            return [*self.static_roots, *sorted(self._granted.values(), key=os.path.normcase)]

    def recorded(self) -> list[str]:
        with self._lock:
            return sorted(self._recorded.values(), key=os.path.normcase)

    def grant(self, raw: str, *, record: bool) -> str:
        """Grant a root. ``record`` is true only for an approved, explicit grant; it survives restarts."""
        canonical = canonical_directory(raw)
        key = os.path.normcase(canonical)
        with self._lock:
            if key not in self._granted and len(self._granted) >= MAX_GRANTED_ROOTS:
                raise PermissionRefused(f"The grant list is full ({MAX_GRANTED_ROOTS} roots).")
            self._granted[key] = canonical
            if record:
                self._recorded[key] = canonical
        if record:
            # Read again under the records lock and add to what is there: another process may have changed it.
            self._update(lambda roots: {**roots, key: canonical})
        return canonical

    def revoke(self, raw: str) -> bool:
        key = os.path.normcase(os.path.abspath(raw))
        with self._lock:
            found = self._granted.pop(key, None) is not None
            self._recorded.pop(key, None)
        self._update(lambda roots: {k: v for k, v in roots.items() if k != key})
        return found

    def _update(self, change: Callable[[dict[str, str]], dict[str, str]]) -> None:
        """Apply ``change`` to the record as it is on disk now, under the records lock."""
        try:
            with _records_locked(self.home):
                current = {os.path.normcase(root): root for root in _read_record(self.path, "roots", MAX_GRANTED_ROOTS)}
                _write_record(self.path, "roots", sorted(change(current).values(), key=os.path.normcase))
        except (OSError, PermissionRefused):
            pass  # the grant stands for this process; it is not remembered

    def restore(self, projects: Callable[[], Iterable[str]]) -> list[str]:
        """Read projects and grants in one records transaction, so a concurrent registration keeps its grant."""
        try:
            with _records_locked(self.home):
                return self._restore(projects())
        except (OSError, PermissionRefused):
            return []

    def _restore(self, projects: Iterable[str]) -> list[str]:
        recorded = _read_record(self.path, "roots", MAX_GRANTED_ROOTS)
        homes = []
        for project in projects:
            try:
                homes.append(canonical_directory(project))
            except PermissionRefused:
                continue
        restored = []
        for root in recorded:
            try:
                canonical = canonical_directory(root)
            except PermissionRefused:
                continue
            if not any(within_root(home, canonical) for home in homes):
                continue
            try:
                self.grant(canonical, record=False)
            except PermissionRefused:
                continue
            with self._lock:
                self._recorded[os.path.normcase(canonical)] = canonical
            restored.append(canonical)
        _write_record(self.path, "roots", sorted(restored, key=os.path.normcase))
        return restored

    def require(self, raw: str) -> str:
        """The canonical directory when it lies inside a root; PermissionRefused otherwise."""
        canonical = canonical_directory(raw)
        for root in self.roots():
            if within_root(canonical, root):
                return canonical
        raise PermissionRefused(
            f"{canonical} is outside conductor's dispatch roots. Grant it with "
            "`conductor grant <repository>` or add a parent to CONDUCTOR_DISPATCH_ROOTS."
        )


# ── approvals ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str  # "run", "grant" or "land"
    subject: str
    fingerprint: str
    summary: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Approval:
    approved: bool
    approver: str
    fingerprint: str
    reason: str = ""
    at: float = 0.0

    def binds(self, request: ApprovalRequest) -> bool:
        return self.approved and self.fingerprint == request.fingerprint

    def record(self) -> dict:
        return asdict(self)


class Approver(Protocol):
    name: str

    def approve(self, request: ApprovalRequest) -> Approval: ...


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class CLIApprover:
    """Asks the person at the terminal. No terminal, no approval: an agent's child process has none."""

    name = "cli"

    def __init__(
        self,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
        *,
        interactive: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._interactive = interactive or (lambda: bool(self._stdin and self._stdin.isatty()))
        self._clock = clock
        self._lock = threading.Lock()

    def approve(self, request: ApprovalRequest) -> Approval:
        with self._lock:
            if not self._interactive():
                return Approval(False, self.name, request.fingerprint, "no interactive terminal", self._clock())
            out = self._stdout
            out.write(f"\nApprove {request.kind}: {request.subject}\n")
            for line in request.summary:
                out.write(f"  {line}\n")
            out.write(f"  fingerprint {request.fingerprint[:16]}\n  Approve? [y/N] ")
            out.flush()
            answer = (self._stdin.readline() or "").strip().lower()
        approved = answer in {"y", "yes"}
        return Approval(
            approved,
            self.name,
            request.fingerprint,
            "approved at the terminal" if approved else "declined at the terminal",
            self._clock(),
        )


class PolicyApprover:
    """Approves from an allow-list. Anything the policy does not allow is refused, with the reason.

    A policy file is TOML with one ``[policy]`` table::

        [policy]
        kinds = ["run", "land"]            # what this policy may approve at all
        adapters = ["scripted"]            # run: every agent and reviewer adapter is listed
        commands = ["{python}", "pytest"]  # run: every check's argv[0] (basename) is listed
        max_tier = "T2"                    # run: no package above this tier; land: same
        max_parallel = 4                   # run
        roots = ["~/code"]                 # grant: only inside these directories
    """

    name = "policy"
    FIELDS = {"kinds", "adapters", "commands", "max_tier", "max_parallel", "roots"}

    def __init__(self, policy: dict, *, source: str = "inline", clock: Callable[[], float] = time.time):
        unknown = set(policy) - self.FIELDS
        if unknown:
            raise ValueError(f"Unknown policy fields: {', '.join(sorted(unknown))}.")
        self.policy = policy
        self.source = source
        self._clock = clock

    @classmethod
    def from_file(cls, path: str, *, forbidden_roots: Sequence[str] = ()) -> PolicyApprover:
        """Load a policy. A policy inside a repository conductor runs in is refused: an agent could edit it."""
        absolute = os.path.realpath(os.path.abspath(path))
        for root in forbidden_roots:
            if within_root(absolute, os.path.realpath(root)):
                raise PermissionRefused(
                    f"The policy {absolute} lies inside {root}, where agents work; keep it "
                    "outside every repository conductor runs in."
                )
        with open(absolute, "rb") as handle:
            data = tomllib.load(handle)
        if set(data) != {"policy"} or not isinstance(data["policy"], dict):
            raise ValueError("A policy file holds exactly one [policy] table.")
        return cls(data["policy"], source=absolute)

    def approve(self, request: ApprovalRequest) -> Approval:
        reason = self._refusal(request)
        return Approval(
            reason is None,
            f"{self.name}:{os.path.basename(self.source)}",
            request.fingerprint,
            reason or "allowed by policy",
            self._clock(),
        )

    def _refusal(self, request: ApprovalRequest) -> str | None:
        policy, facts = self.policy, request.facts
        if request.kind not in (policy.get("kinds") or []):
            return f"the policy does not approve {request.kind} requests"
        tier_limit = policy.get("max_tier")
        if request.kind in {"run", "land"} and tier_limit:
            if tier_limit not in TIER_ORDER:
                return f"the policy's max_tier {tier_limit!r} is not a tier"
            over = [t for t in facts.get("tiers", []) if TIER_ORDER.index(t) > TIER_ORDER.index(tier_limit)]
            if over:
                return f"tier {over[0]} is above the policy's {tier_limit}"
        if request.kind == "run":
            adapters = set(policy.get("adapters") or [])
            outside = sorted(set(facts.get("adapters", [])) - adapters)
            if outside:
                return f"adapter {outside[0]} is not allowed by the policy"
            commands = set(policy.get("commands") or [])
            outside = sorted(set(facts.get("commands", [])) - commands)
            if outside:
                return f"check command {outside[0]} is not allowed by the policy"
            limit = policy.get("max_parallel")
            if limit is not None and facts.get("max_parallel", 1) > limit:
                return f"max_parallel {facts.get('max_parallel')} is above the policy's {limit}"
        if request.kind == "grant":
            roots = [os.path.realpath(os.path.expanduser(root)) for root in policy.get("roots") or []]
            if not any(within_root(os.path.realpath(facts.get("path", "")), root) for root in roots):
                return "the directory is outside the policy's roots"
        return None


def run_request(plan: dict, fingerprint_value: str, summary: list[str]) -> ApprovalRequest:
    packages = plan["packages"]
    adapters = sorted({spec["adapter"] for p in packages for spec in (p["agent"], p.get("reviewer")) if spec})
    commands = sorted(
        {
            os.path.basename(check["argv"][0]) if check["argv"][0] != "{python}" else "{python}"
            for p in packages
            for check in p["acceptance"]["checks"]
        }
        | {
            os.path.basename(c["argv"][0]) if c["argv"][0] != "{python}" else "{python}"
            for c in plan["environment"]["setup"]
        }
    )
    return ApprovalRequest(
        "run",
        plan["name"],
        fingerprint_value,
        summary,
        {
            "adapters": adapters,
            "commands": commands,
            "max_parallel": plan["max_parallel"],
            "tiers": sorted({p["verification_tier"] for p in packages}),
        },
    )


def grant_request(path: str) -> ApprovalRequest:
    return ApprovalRequest(
        "grant",
        path,
        fingerprint({"grant": os.path.normcase(path)}),
        [f"conductor may create worktrees, run agents and checks, and land commits in {path}"],
        {"path": path},
    )
