"""The workflow contract: the packages, what each may change, how each is checked, and what lands before what.

A workflow file is TOML (or JSON with the same shape). It is validated whole before anything runs; a field the
contract does not know is refused rather than ignored, so a typo cannot silently drop a check. The validated plan
has a fingerprint, and an approval binds to it: a workflow that changes after it was approved does not resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Sequence
from typing import Any

from . import agents
from . import worktrees as wt

VERSION = 1
MAX_PACKAGES = 100
MAX_CHECKS = 10
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
TIERS = ("T0", "T1", "T2", "T3")
TOP_FIELDS = {"version", "name", "integration_branch", "max_parallel", "git", "environment", "packages"}
PACKAGE_FIELDS = {
    "id",
    "title",
    "instruction",
    "depends_on",
    "verification_tier",
    "max_attempts",
    "timeout_seconds",
    "files",
    "agent",
    "reviewer",
    "acceptance",
}


class ContractError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, label: str, limit: int, *, default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or not value.strip():
        raise ContractError(f"{label} must be a non-empty string of at most {limit} characters.")
    return value.strip()


def _integer(value: Any, label: str, default: int, minimum: int, maximum: int) -> int:
    value = default if value is None else value
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _argv_list(raw: Any, label: str, maximum: int, *, minimum: int = 0) -> list[dict]:
    if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
        raise ContractError(f"{label} must be a list of {minimum} to {maximum} commands.")
    commands = []
    for command in raw:
        if not isinstance(command, dict) or set(command) != {"argv", "timeout_seconds"}:
            raise ContractError(f"Each {label} entry needs exactly argv and timeout_seconds.")
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 40
            or not argv[0]
            or any(not isinstance(part, str) or len(part) > 2000 or "\x00" in part for part in argv)
        ):
            raise ContractError(f"{label}: argv must be a list of 1 to 40 strings. No shell runs it.")
        commands.append(
            {
                "argv": list(argv),
                "timeout_seconds": _integer(command["timeout_seconds"], f"{label} timeout_seconds", 0, 1, 3600),
            }
        )
    return commands


def relative_path(raw: Any, label: str) -> str:
    """A repository-relative path: forward slashes, no traversal, no git or conductor internals, no globs."""
    if not isinstance(raw, str) or not raw or len(raw) > 500 or "\x00" in raw:
        raise ContractError(f"{label}: a repository-relative path is required.")
    path = raw.replace("\\", "/")
    stem = path[:-1] if path.endswith("/") else path
    parts = stem.split("/")
    if (
        path.startswith("/")
        or ":" in path
        or any(char in path for char in "*?[")
        or any(part in {"", ".", ".."} or part != part.strip() for part in parts)
        or parts[0].lower() in {".git", ".conductor"}
    ):
        raise ContractError(f"{label}: {raw!r} is not a plain repository-relative path (no globs, no '..').")
    return path


def in_scope(path: str, files: Sequence[str]) -> bool:
    """Whether ``path`` is one of ``files``: an exact file, or under a directory entry ending in '/'."""
    return any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in files)


def _environment(raw: Any) -> dict:
    if raw is None:
        return {"link": [], "setup": [], "env": []}
    if not isinstance(raw, dict) or set(raw) - {"link", "setup", "env"}:
        raise ContractError("environment takes link, setup and env.")
    links = raw.get("link", [])
    if not isinstance(links, list) or len(links) > 20:
        raise ContractError("environment.link must be a list of at most 20 directories.")
    names = raw.get("env")
    if names is not None and (
        not isinstance(names, list)
        or len(names) > 80
        or any(not isinstance(n, str) or not ENV_NAME.fullmatch(n) for n in names)
    ):
        raise ContractError("environment.env must be a list of at most 80 variable names.")
    return {
        "link": [relative_path(entry, "environment.link").rstrip("/") for entry in links],
        "setup": _argv_list(raw.get("setup", []), "environment.setup", 10),
        "env": list(names or []),
    }


def _package(raw: Any, ids: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("Each package must be a table.")
    unknown = set(raw) - PACKAGE_FIELDS
    key = raw.get("id")
    if not isinstance(key, str) or not KEY.fullmatch(key) or key.endswith("."):
        raise ContractError(f"Package id {key!r} must be 1-80 letters, digits, '_', '.' or '-'.")
    try:
        # The same rules the runtime applies: the id names a worktree directory and a branch on every platform.
        wt.branch(wt.branch_name(key))
    except wt.WorktreeError as exc:
        raise ContractError(f"{exc} No '..', no '.lock' ending, and no Windows device name such as CON.") from None
    if unknown:
        raise ContractError(f"{key}: unknown fields {', '.join(sorted(unknown))}.")
    if key.casefold() in ids:
        # Compared without case: A and a would share a worktree directory and a branch name on a case-insensitive
        # filesystem.
        raise ContractError(f"Package id {key} is used twice (ids are compared without regard to case).")
    title = _text(raw.get("title"), f"{key}: title", 160)
    package = {
        "id": key,
        "title": title,
        "instruction": _text(raw.get("instruction"), f"{key}: instruction", 20000, default=title),
        "verification_tier": raw.get("verification_tier", "T0"),
        "max_attempts": _integer(raw.get("max_attempts"), f"{key}: max_attempts", 2, 1, 5),
        "timeout_seconds": _integer(raw.get("timeout_seconds"), f"{key}: timeout_seconds", 1800, 5, 86400),
    }
    if package["verification_tier"] not in TIERS:
        raise ContractError(f"{key}: verification_tier must be one of {', '.join(TIERS)}.")
    deps = raw.get("depends_on", [])
    if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(deps) != len(set(deps)):
        raise ContractError(f"{key}: depends_on must be a list of distinct package ids.")
    if key in deps:
        raise ContractError(f"{key} cannot depend on itself.")
    package["depends_on"] = list(deps)
    files = raw.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 300:
        raise ContractError(f"{key}: files must list 1 to 300 paths this package may change (a directory ends in /).")
    package["files"] = [relative_path(entry, f"{key}: files") for entry in files]
    try:
        package["agent"] = agents.validate_spec(raw.get("agent"))
    except ValueError as exc:
        raise ContractError(f"{key}: agent: {exc}") from exc
    reviewer = raw.get("reviewer")
    tier = package["verification_tier"]
    if tier == "T0":
        if reviewer is not None:
            raise ContractError(f"{key}: a T0 package is checked, not reviewed; drop reviewer or raise the tier.")
        package["reviewer"] = None
    else:
        try:
            package["reviewer"] = agents.validate_spec(reviewer)
        except ValueError as exc:
            raise ContractError(f"{key}: a {tier} package needs a reviewer: {exc}") from exc
        if package["reviewer"] == package["agent"]:
            raise ContractError(f"{key}: the reviewer must not be the implementer's exact configuration.")
        if tier in {"T2", "T3"} and agents.family_of(package["reviewer"]) == agents.family_of(package["agent"]):
            raise ContractError(f"{key}: {tier} review needs a reviewer from another model family.")
    acceptance = raw.get("acceptance")
    if not isinstance(acceptance, dict) or set(acceptance) != {"checks"}:
        raise ContractError(f"{key}: acceptance needs checks, the commands that decide whether it lands.")
    package["acceptance"] = {
        "checks": _argv_list(acceptance["checks"], f"{key}: acceptance.checks", MAX_CHECKS, minimum=1)
    }
    return package


def validate(raw: Any) -> dict:
    """The plan, normalized, or ContractError naming the first problem."""
    if not isinstance(raw, dict) or raw.get("version") != VERSION or type(raw.get("version")) is not int:
        raise ContractError(f"A version {VERSION} workflow is required (version = {VERSION}).")
    unknown = set(raw) - TOP_FIELDS
    if unknown:
        raise ContractError(f"Unknown workflow fields: {', '.join(sorted(unknown))}.")
    integration = raw.get("integration_branch", "main")
    try:
        wt.check_integration_branch(integration)
    except wt.WorktreeError as exc:
        raise ContractError(f"integration_branch: {exc}") from None
    git = raw.get("git") or {}
    if not isinstance(git, dict) or set(git) - {"author_name", "author_email"}:
        raise ContractError("git takes author_name and author_email.")
    identity = None
    if git:
        identity = [
            _text(git.get("author_name"), "git.author_name", 120),
            _text(git.get("author_email"), "git.author_email", 200),
        ]
    packages_raw = raw.get("packages")
    if not isinstance(packages_raw, list) or not 1 <= len(packages_raw) <= MAX_PACKAGES:
        raise ContractError(f"A workflow has 1 to {MAX_PACKAGES} packages.")
    packages: list[dict] = []
    ids: set[str] = set()
    for entry in packages_raw:
        package = _package(entry, ids)
        ids.add(package["id"].casefold())
        packages.append(package)
    known = {package["id"] for package in packages}
    for package in packages:
        missing = [dep for dep in package["depends_on"] if dep not in known]
        if missing:
            raise ContractError(f"{package['id']} depends on unknown packages: {', '.join(missing)}.")
    plan = {
        "version": VERSION,
        "name": _text(raw.get("name"), "name", 120),
        "integration_branch": integration,
        "max_parallel": _integer(raw.get("max_parallel"), "max_parallel", 2, 1, 8),
        "identity": identity,
        "environment": _environment(raw.get("environment")),
        "packages": packages,
    }
    dependency_order(plan)  # refuses a cycle
    return plan


def load(path: str) -> dict:
    """Read and validate a workflow file. The plan carries the file's SHA-256."""
    with open(path, "rb") as handle:
        data = handle.read(1_000_001)
    if len(data) > 1_000_000:
        raise ContractError("A workflow file is at most 1 MB.")
    try:
        raw = json.loads(data) if path.lower().endswith(".json") else tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContractError(
            f"{os.path.basename(path)} is not valid {'JSON' if path.endswith('.json') else 'TOML'}: {exc}"
        ) from exc
    plan = validate(raw)
    plan["workflow_sha256"] = hashlib.sha256(data).hexdigest()
    return plan


def fingerprint(plan: dict) -> str:
    """What an approval binds to: the whole validated plan."""
    return digest({key: value for key, value in plan.items() if key != "workflow_sha256"})


def dependency_order(plan: dict) -> list[str]:
    """Package ids with every dependency first; ContractError on a cycle."""
    graph = {package["id"]: package["depends_on"] for package in plan["packages"]}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(key: str, trail: tuple[str, ...]) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            cycle = " -> ".join((*trail[trail.index(key) :], key))
            raise ContractError(f"Package dependencies contain a cycle: {cycle}.")
        state[key] = 1
        for dep in graph[key]:
            visit(dep, (*trail, key))
        state[key] = 2
        order.append(key)

    for key in graph:
        visit(key, ())
    return order


def ancestors(plan: dict, package_id: str) -> set[str]:
    graph = {package["id"]: package["depends_on"] for package in plan["packages"]}
    seen: set[str] = set()
    stack = list(graph[package_id])
    while stack:
        key = stack.pop()
        if key not in seen:
            seen.add(key)
            stack.extend(graph[key])
    return seen


def overlaps(plan: dict) -> list[str]:
    """Warnings for packages that may run at the same time and declare overlapping files.

    Not an error: the merge queue catches the conflict and returns the later package as rework. It is worth
    knowing before the run, because that rework costs an attempt.
    """
    warnings = []
    packages = plan["packages"]
    for index, first in enumerate(packages):
        for second in packages[index + 1 :]:
            if first["id"] in ancestors(plan, second["id"]) or second["id"] in ancestors(plan, first["id"]):
                continue
            shared = sorted(
                {a for a in first["files"] for b in second["files"] if in_scope(a, [b]) or in_scope(b, [a])}
            )
            if shared:
                warnings.append(
                    f"{first['id']} and {second['id']} may run in parallel and both declare "
                    f"{', '.join(shared[:5])}; the later landing may conflict and be reworked."
                )
    return warnings


def summary(plan: dict) -> list[str]:
    """One line per package, for an approval prompt."""
    lines = [
        f"{plan['name']}: {len(plan['packages'])} packages onto {plan['integration_branch']}, "
        f"at most {plan['max_parallel']} at a time"
    ]
    for package in plan["packages"]:
        after = f" after {', '.join(package['depends_on'])}" if package["depends_on"] else ""
        checks = "; ".join(" ".join(check["argv"]) for check in package["acceptance"]["checks"])
        lines.append(
            f"  {package['id']} [{package['verification_tier']}] {agents.describe(package['agent'])}"
            f"{after}: may change {', '.join(package['files'][:4])}; checks: {checks}"
        )
    return lines
