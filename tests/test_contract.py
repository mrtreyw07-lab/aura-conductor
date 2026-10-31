"""The workflow contract: what is accepted, what is refused, and what an approval binds to."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from conductor import contract
from conductor.contract import ContractError

from .conftest import PASS, package, workflow

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two-dependent-packages" / "workflow.toml"


def valid() -> dict:
    return workflow(
        package("A", [{"write": "a.txt", "content": "a"}]),
        package("B", [{"write": "b.txt", "content": "b"}], depends_on=("A",)),
    )


def test_the_example_workflow_is_valid():
    plan = contract.load(str(EXAMPLE))
    assert [p["id"] for p in plan["packages"]] == ["A", "B", "C"]
    assert plan["packages"][1]["depends_on"] == ["A"]
    assert plan["packages"][2]["verification_tier"] == "T1"
    assert len(plan["workflow_sha256"]) == 64
    assert contract.overlaps(plan) == [
        "A and C may run in parallel and both declare README.md; the later landing may conflict and be reworked."
    ]


@pytest.mark.parametrize("path", sorted((EXAMPLE.parents[1]).glob("*/workflow.toml")), ids=lambda p: p.parent.name)
def test_every_example_workflow_is_valid(path):
    plan = contract.load(str(path))
    assert plan["packages"] and contract.fingerprint(plan)


def test_defaults_are_filled_in():
    plan = contract.validate(
        {
            "version": 1,
            "name": "x",
            "packages": [
                {
                    "id": "A",
                    "title": "t",
                    "files": ["a/"],
                    "agent": {"adapter": "scripted"},
                    "acceptance": {"checks": [PASS]},
                }
            ],
        }
    )
    entry = plan["packages"][0]
    assert plan["integration_branch"] == "main" and plan["max_parallel"] == 2
    assert entry["instruction"] == "t" and entry["verification_tier"] == "T0" and entry["max_attempts"] == 2
    assert entry["reviewer"] is None and entry["depends_on"] == []


def test_json_and_toml_load_the_same_plan(tmp_path):
    data = valid()
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    plan = contract.load(str(path))
    assert contract.fingerprint(plan) == contract.fingerprint(contract.validate(data))


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda d: d.update(version=2), "version 1"),
        (lambda d: d.update(version=True), "version 1"),
        (lambda d: d.update(colour="blue"), "Unknown workflow fields: colour"),
        (lambda d: d.update(max_parallel=9), "max_parallel"),
        (lambda d: d.update(integration_branch="../main"), "integration_branch"),
        (lambda d: d.update(packages=[]), "1 to 100 packages"),
        (lambda d: d["packages"][0].update(typo=1), "unknown fields typo"),
        (lambda d: d["packages"][0].update(id="bad id"), "Package id"),
        (lambda d: d["packages"][0].update(id="A.lock"), "cannot name a worktree and a branch"),
        (lambda d: d["packages"][0].update(id="A..B"), "cannot name a worktree and a branch"),
        (lambda d: d["packages"][0].update(id="con"), "cannot name a worktree and a branch"),
        (lambda d: d["packages"][0].update(id="Com1.txt"), "cannot name a worktree and a branch"),
        (lambda d: d.update(integration_branch="conductor/A"), "conductor's own conductor/ namespace"),
        (lambda d: d.update(integration_branch="Conductor/x"), "conductor's own conductor/ namespace"),
        (lambda d: d.update(integration_branch="refs/heads/main"), "cannot be the integration branch"),
        (lambda d: d.update(integration_branch="a/.hidden"), "integration_branch"),
        (lambda d: d["packages"][1].update(id="A"), "used twice"),
        (lambda d: d["packages"][1].update(id="a"), "compared without regard to case"),
        (lambda d: d["packages"][1].update(depends_on=["a"]), "unknown packages: a"),
        (lambda d: d["packages"][1].update(depends_on=["Z"]), "unknown packages: Z"),
        (lambda d: d["packages"][0].update(depends_on=["A"]), "cannot depend on itself"),
        (lambda d: d["packages"][0].update(depends_on=["B"]), "cycle: A -> B -> A"),
        (lambda d: d["packages"][0].update(files=[]), "files must list"),
        (lambda d: d["packages"][0].update(files=["../escape.txt"]), "plain repository-relative"),
        (lambda d: d["packages"][0].update(files=["src/*.py"]), "no globs"),
        (lambda d: d["packages"][0].update(files=[".git/config"]), "plain repository-relative"),
        (lambda d: d["packages"][0].update(files=["/abs/path"]), "plain repository-relative"),
        (lambda d: d["packages"][0].update(acceptance={"checks": []}), "1 to 10 commands"),
        (lambda d: d["packages"][0].update(acceptance={}), "acceptance needs checks"),
        (lambda d: d["packages"][0]["acceptance"]["checks"][0].update(argv="python -c pass"), "argv must be a list"),
        (lambda d: d["packages"][0]["acceptance"]["checks"][0].update(timeout_seconds=0), "timeout_seconds"),
        (lambda d: d["packages"][0].update(verification_tier="T9"), "verification_tier"),
        (lambda d: d["packages"][0].update(agent={"adapter": "nope"}), "adapter must be one of"),
        (lambda d: d["packages"][0].update(reviewer={"adapter": "scripted", "name": "r"}), "T0 package is checked"),
        (lambda d: d["packages"][0].update(verification_tier="T1"), "needs a reviewer"),
        (
            lambda d: d["packages"][0].update(verification_tier="T1", reviewer=d["packages"][0]["agent"]),
            "exact configuration",
        ),
        (
            lambda d: d["packages"][0].update(verification_tier="T2", reviewer={"adapter": "scripted", "name": "r"}),
            "another model family",
        ),
    ],
)
def test_refusals_name_the_problem(change, message):
    data = copy.deepcopy(valid())
    change(data)
    with pytest.raises(ContractError, match=message.replace("(", r"\(").replace("*", r"\*")):
        contract.validate(data)


def test_tier_two_accepts_a_reviewer_from_another_family():
    data = valid()
    data["packages"][0].update(verification_tier="T2", reviewer={"adapter": "scripted", "family": "other"})
    assert contract.validate(data)["packages"][0]["reviewer"]["family"] == "other"


def test_fingerprint_binds_every_field():
    first = contract.validate(valid())
    changed = valid()
    changed["packages"][0]["acceptance"]["checks"][0]["argv"] = ["{python}", "-c", "print(1)"]
    assert contract.fingerprint(first) == contract.fingerprint(contract.validate(valid()))
    assert contract.fingerprint(first) != contract.fingerprint(contract.validate(changed))


def test_scope_and_ordering_helpers():
    assert contract.in_scope("src/a.py", ["src/"]) and contract.in_scope("a.txt", ["a.txt"])
    assert not contract.in_scope("src2/a.py", ["src/"]) and not contract.in_scope("a.txt.bak", ["a.txt"])
    plan = contract.validate(
        workflow(package("C", [], depends_on=("B",)), package("B", [], depends_on=("A",)), package("A", []))
    )
    assert contract.dependency_order(plan) == ["A", "B", "C"]
    assert contract.ancestors(plan, "C") == {"A", "B"}
    assert contract.overlaps(plan) == []
    assert contract.summary(plan)[1].startswith("  C [T0] scripted:scripted after B")


def test_oversized_and_malformed_files_are_refused(tmp_path):
    big = tmp_path / "big.toml"
    big.write_bytes(b"#" * 1_000_001)
    with pytest.raises(ContractError, match="at most 1 MB"):
        contract.load(str(big))
    bad = tmp_path / "bad.toml"
    bad.write_text("version = [", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid TOML"):
        contract.load(str(bad))
    assert os.path.exists(bad)
