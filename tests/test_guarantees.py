"""The README's guarantees that can be read off the source itself.

Behaviour is tested elsewhere (the reflog checks in ``test_scheduler`` and ``test_demo``). These tests make sure no
code path can do what the guarantees rule out: every git argv conductor builds is a list literal, so the literals
are the whole vocabulary.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "conductor"


def git_argv_lists() -> list[tuple[str, int, list[str]]]:
    """The string constants of every list literal that holds any: every git argv in the source has that shape,
    including those that start with ``*identity_arguments(...)``."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                words = [
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
                if words:
                    found.append((path.name, node.lineno, words))
    return found


def test_conductor_never_rebases_pushes_or_rewrites_history():
    forbidden = {
        "rebase",
        "push",
        "filter-branch",
        "filter-repo",
        "--force-with-lease",
        "--amend",
        "fetch",
        "pull",
        "remote",
    }
    offenders = [(name, line, words) for name, line, words in git_argv_lists() if forbidden & set(words)]
    assert offenders == []


def test_force_appears_only_where_it_touches_conductors_own_worktrees():
    forced = sorted(
        (name, words[0])
        for name, _line, words in git_argv_lists()
        if any(word in {"--force", "-f", "-fd", "-D"} for word in words)
    )
    # Removing a package or integration worktree conductor made; re-detaching and clearing the integration worktree.
    assert forced == [
        ("merge_queue.py", "checkout"),
        ("merge_queue.py", "clean"),
        ("worktrees.py", "clean"),
        ("worktrees.py", "worktree"),
    ]


def argv_nodes(name: str) -> list[ast.List]:
    """The list literals in one module that start with a string: its git argv lists, as syntax."""
    tree = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant)
    ]


def names(nodes) -> list[str | None]:
    return [node.id if isinstance(node, ast.Name) else getattr(node, "value", None) for node in nodes]


def test_the_integration_branch_moves_only_by_fast_forward_or_compare_and_swap():
    lists = argv_nodes("merge_queue.py")
    merges = [names(node.elts) for node in lists if names(node.elts)[:2] == ["merge", "--ff-only"]]
    assert merges == [["merge", "--ff-only", "--no-overwrite-ignore", "landing"]]
    swaps = [names(node.elts) for node in lists if names(node.elts)[0] == "update-ref"]
    # update-ref <branch> <new> <old>: git refuses unless the branch still points at the old value, the parent.
    assert len(swaps) == 1 and swaps[0][-2:] == ["landing", "parent"]
    deletes = [names(node.elts) for node in argv_nodes("worktrees.py") if names(node.elts)[:2] == ["update-ref", "-d"]]
    assert len(deletes) == 1 and len(deletes[0]) == 4  # a pruned package branch is deleted only from its known tip
    branch_moves = [words for name, _line, words in git_argv_lists() if words[:1] == ["branch"]]
    assert branch_moves == [["branch", "-m"]]  # an old attempt's branch is renamed, never reset


def test_no_shell_anywhere():
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, path.name
