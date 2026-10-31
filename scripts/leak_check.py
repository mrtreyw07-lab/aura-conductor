"""Fail when the repository carries something that belongs on one machine: a local path, a session id, a private
name, or a personal email address.

Usage: ``python scripts/leak_check.py [--history]``

* Files: every tracked or untracked-but-not-ignored text file.
* ``--history``: every commit on every ref too: its message, and its author's and committer's names and emails.

Three kinds of finding:

1. **Paths and ids** (plain patterns below): drive-letter paths, home directories, desktop-session and workflow ids.
2. **Private names.** Stored as salted SHA-256 digests of lowercase words, so this file does not itself publish the
   names it hunts for. Every word of every file is hashed and compared.
3. **Extra names**, in plain text, from the ``LEAK_TERMS`` environment variable (CI reads it from a secret) or a
   git-ignored ``.leakcheck.local`` file, one per line.

Allowed: the repository's own GitHub URL (``github.com/<owner>/aura-conductor``).

Commit identities are held to a stricter rule than text: every author and committer must be a GitHub noreply address
(``<id>+<login>@users.noreply.github.com``, bots' ``[bot]`` logins included) whose name is that login or empty, or
GitHub's own web-flow committer (``GitHub <noreply@github.com>``). Anything else is a finding, whether or not it holds
a known private name.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

SALT = "conductor-leak-check:"
PRIVATE = frozenset(
    {
        "93728706f1dd307ca848512eb59c767d20c0d91f9fa1ad35a98f9f50f4d1d767",
        "250baf0743f1fd4d5205e2beb76f96bf2a7c39faab2574db56c6a1ffa0fdbec8",
        "1a014d74b71aeb18e3335c729421d54c4a045552192e3c99b8ea1f7d56753877",
        "57ca26584458d3a614da4c3c13b3e0b08fdbbee9970e846343b8201908e1aff9",
        "20fa608b3e9f4cdacac6a3e6c9c57314a79e983eeb5c5d56f482b6e703bdc7ad",
        "7326cf96c4e49ce364d4d03467fa4363cc044dd99e81b4896dba6adf47380212",
        "0d34f1f2508b133fbd182d3ffd4fc8f3518d78de9758d8f3a262b46304b4d31e",
        "3692379c6b8f207646fd07bc80e105926d24f4eebca648651dffbe848aa554b5",
        "fa27c2b22a4cf2ac53bdde679c762e8c84c58e167190bca0e43d7c24692f66a8",
        "c30d719fb3f3c1905bf2a59a8d9bd989c41773fba9b9b1ccc21e8f42ce2e559e",
        "025a423389cd791da66dea72cc8711cee96aa9307fc1a2a4b33f8be0c032c3dd",
        "7093e4148de87578a9e55f1bf8bc35ef4f317a0487ff691752e56a9c80449ca5",
    }
)
PATTERNS = {
    "a drive-letter path": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][A-Za-z_.~$]"),
    "a home directory": re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/(?!runner/|user/|you/|me/|<)[A-Za-z0-9_.-]+"),
    "a desktop session id": re.compile(r"local_[0-9a-f]{8}-[0-9a-f]{4}-"),
    "a private workflow id": re.compile(r"\bwf_[0-9a-f]{24}\b"),
    "a private package number": re.compile(r"\bWP-\d{1,3}\b"),
}
ALLOWED = re.compile(r"github\.com/[A-Za-z0-9-]+/aura-conductor")
NOREPLY = re.compile(r"^(?:\d+\+)?(?P<login>[A-Za-z0-9-]+(?:\[bot\])?)@users\.noreply\.github\.com$")
WEB_FLOW = ("GitHub", "noreply@github.com")
WORD = re.compile(r"[a-z0-9]+")
SKIP = {"scripts/leak_check.py", "tests/test_leak_check.py"}
SEPARATOR = "\x1f"


def digest(word: str) -> str:
    return hashlib.sha256((SALT + word.lower()).encode("utf-8")).hexdigest()


def extra_terms(root: str) -> set[str]:
    terms = {line.strip().lower() for line in os.environ.get("LEAK_TERMS", "").splitlines() if line.strip()}
    try:
        with open(os.path.join(root, ".leakcheck.local"), encoding="utf-8") as handle:
            terms |= {line.strip().lower() for line in handle if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        pass
    return terms


def findings(text: str, extra: set[str]) -> list[str]:
    found = []
    text = ALLOWED.sub("", text)
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            found.append(label)
    lowered = text.lower()
    if any(digest(word) in PRIVATE for word in set(WORD.findall(lowered))):
        found.append("a private name")
    if any(term in lowered for term in extra):
        found.append("an extra term")
    return found


def identity_findings(name: str, email: str, extra: set[str]) -> list[str]:
    """A commit identity passes only as a GitHub noreply address named by its own login (or not named), or as
    GitHub's web-flow committer. Anything else is a finding by itself, and is also scanned like text."""
    name, email = name.strip(), email.strip()
    if (name, email) == WEB_FLOW:
        return []
    noreply = NOREPLY.fullmatch(email)
    if noreply and name in {"", noreply.group("login")}:
        return []
    labels = [f"{label} in a commit identity" for label in findings(f"{name} {email}", extra)]
    if not noreply:
        return ["an email that is not a GitHub noreply address", *labels]
    return ["a name that is not the noreply address's login", *labels]


def git(root: str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, check=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ).stdout


def history(root: str, extra: set[str]) -> list[str]:
    problems = []
    if not git(root, "rev-parse", "--verify", "-q", "HEAD"):
        return problems
    fields = SEPARATOR.join(["%H", "%an", "%ae", "%cn", "%ce", "%B"])
    for record in git(root, "log", "--all", f"--format={fields}%x1e").decode("utf-8", errors="replace").split("\x1e"):
        parts = record.strip("\n").split(SEPARATOR, 5)
        if len(parts) != 6:
            continue
        commit, author, author_email, committer, committer_email, message = parts
        labels = (
            findings(message, extra)
            + identity_findings(author, author_email, extra)
            + identity_findings(committer, committer_email, extra)
        )
        problems.extend(f"commit {commit[:12]}: {label}" for label in labels)
    return problems


def main(argv: list[str], root: str | None = None) -> int:
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    extra = extra_terms(root)
    problems = []
    try:
        listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    except subprocess.CalledProcessError as exc:
        print(f"leak check: git could not list the files: {exc.stderr.decode(errors='replace').strip()}")
        return 2
    for raw in sorted({name for name in listed if name}):
        name = os.fsdecode(raw)
        if name in SKIP or not os.path.isfile(os.path.join(root, name)):
            continue
        with open(os.path.join(root, name), "rb") as handle:
            data = handle.read()
        if b"\0" in data[:8192]:
            continue  # binary
        for label in findings(data.decode("utf-8", errors="replace"), extra):
            problems.append(f"{name}: {label}")
    if "--history" in argv:
        problems.extend(history(root, extra))
    for problem in problems:
        print(problem)
    print(f"leak check: {len(problems)} finding(s)" + (" (history included)" if "--history" in argv else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
