"""Dispatch roots, grants that survive a restart while a project lives in them, and the approvers."""

from __future__ import annotations

import io
import json
import os

import pytest

from conductor import permissions
from conductor.permissions import (
    MAX_GRANTED_ROOTS,
    Approval,
    ApprovalRequest,
    CLIApprover,
    GrantStore,
    PermissionRefused,
    PolicyApprover,
    ProjectRegistry,
)

from .conftest import symlinks_available


def real(path) -> str:
    return os.path.realpath(str(path))


def test_canonical_directory_fails_closed(tmp_path):
    with pytest.raises(PermissionRefused, match="required"):
        permissions.canonical_directory("")
    with pytest.raises(PermissionRefused, match="NUL"):
        permissions.canonical_directory("a\x00b")
    with pytest.raises(PermissionRefused, match="absolute"):
        permissions.canonical_directory("relative/dir")
    with pytest.raises(PermissionRefused, match="not an existing directory"):
        permissions.canonical_directory(real(tmp_path / "missing"))
    assert permissions.canonical_directory(real(tmp_path)) == real(tmp_path)


def test_a_path_through_a_link_is_refused(tmp_path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks need a privilege this machine does not grant")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link, target_is_directory=True)
    with pytest.raises(PermissionRefused, match="resolves through a link"):
        permissions.canonical_directory(os.path.abspath(link))


def test_require_accepts_only_paths_inside_a_root(tmp_path, conductor_home):
    inside = tmp_path / "root" / "repo"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    grants = GrantStore(conductor_home, static_roots=[str(tmp_path / "root")])
    assert grants.require(real(inside)) == real(inside)
    with pytest.raises(PermissionRefused, match="outside conductor's dispatch roots"):
        grants.require(real(outside))
    grants.grant(real(outside), record=False)
    assert grants.require(real(outside)) == real(outside)


def test_static_roots_come_from_the_environment(tmp_path, monkeypatch, conductor_home):
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ROOTS", os.pathsep.join([str(tmp_path), "", str(tmp_path / "missing")]))
    assert GrantStore(conductor_home).static_roots == [real(tmp_path)]


def test_a_recorded_grant_survives_a_restart_while_a_project_lives_in_it(tmp_path, conductor_home):
    project = tmp_path / "work" / "repo"
    project.mkdir(parents=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    first = GrantStore(conductor_home, static_roots=[])
    first.grant(real(tmp_path / "work"), record=True)
    first.grant(real(empty), record=True)
    first.grant(real(project), record=False)  # a re-grant inside a run is never recorded
    assert first.recorded() == sorted([real(tmp_path / "work"), real(empty)], key=os.path.normcase)

    registry = ProjectRegistry(conductor_home)
    registry.add(real(project))
    restarted = GrantStore(conductor_home, static_roots=[])
    assert restarted.restore(registry.projects) == [real(tmp_path / "work")]
    assert restarted.require(real(project)) == real(project)
    with pytest.raises(PermissionRefused):
        restarted.require(real(empty))
    with open(restarted.path, encoding="utf-8") as handle:
        record = json.load(handle)
    assert record == {"version": 1, "roots": [real(tmp_path / "work")]}

    registry.archive(real(project))
    after_archive = GrantStore(conductor_home, static_roots=[])
    assert after_archive.restore(registry.projects) == []
    with pytest.raises(PermissionRefused):
        after_archive.require(real(project))


def test_a_vanished_root_is_dropped(tmp_path, conductor_home):
    gone = tmp_path / "gone"
    gone.mkdir()
    GrantStore(conductor_home, static_roots=[]).grant(real(gone), record=True)
    gone.rmdir()
    assert GrantStore(conductor_home, static_roots=[]).restore(lambda: [real(tmp_path)]) == []


@pytest.mark.parametrize(
    "record",
    [
        b"not json",
        json.dumps({"version": True, "roots": []}).encode(),
        json.dumps({"version": 2, "roots": []}).encode(),
        json.dumps({"version": 1, "roots": "x"}).encode(),
        json.dumps({"version": 1, "roots": [1]}).encode(),
        b" " * (permissions.MAX_RECORD_BYTES + 1),
    ],
)
def test_a_malformed_or_oversized_record_grants_nothing(tmp_path, conductor_home, record):
    os.makedirs(conductor_home, exist_ok=True)
    with open(os.path.join(conductor_home, permissions.GRANTS_FILE), "wb") as handle:
        handle.write(record)
    assert GrantStore(conductor_home, static_roots=[]).restore(lambda: [real(tmp_path)]) == []


def test_the_grant_list_is_capped(tmp_path, conductor_home):
    grants = GrantStore(conductor_home, static_roots=[])
    for index in range(MAX_GRANTED_ROOTS):
        folder = tmp_path / f"g{index}"
        folder.mkdir()
        grants.grant(real(folder), record=False)
    extra = tmp_path / "extra"
    extra.mkdir()
    with pytest.raises(PermissionRefused, match="full"):
        grants.grant(real(extra), record=False)


def test_revoke_forgets_a_grant(tmp_path, conductor_home):
    grants = GrantStore(conductor_home, static_roots=[])
    grants.grant(real(tmp_path), record=True)
    assert grants.revoke(real(tmp_path)) and not grants.recorded()
    assert not grants.revoke(real(tmp_path))


def request(kind="run", facts=None) -> ApprovalRequest:
    return ApprovalRequest(kind, "subject", "f" * 64, ["line"], facts or {})


def test_an_approval_binds_to_its_fingerprint():
    approval = Approval(True, "x", "f" * 64)
    assert approval.binds(request())
    assert not Approval(True, "x", "e" * 64).binds(request())
    assert not Approval(False, "x", "f" * 64).binds(request())


def test_the_cli_approver_refuses_without_a_terminal():
    decision = CLIApprover(io.StringIO("y\n"), io.StringIO()).approve(request())
    assert not decision.approved and decision.reason == "no interactive terminal"


@pytest.mark.parametrize("answer,approved", [("y\n", True), ("yes\n", True), ("n\n", False), ("\n", False)])
def test_the_cli_approver_asks(answer, approved):
    out = io.StringIO()
    decision = CLIApprover(io.StringIO(answer), out, interactive=lambda: True).approve(request())
    assert decision.approved is approved and decision.binds(request()) is approved
    assert "Approve run: subject" in out.getvalue() and "fingerprint ffffffffffffffff" in out.getvalue()


@pytest.mark.parametrize(
    "facts,reason",
    [
        ({"adapters": ["scripted"], "commands": ["{python}"], "tiers": ["T0"], "max_parallel": 2}, None),
        ({"adapters": ["codex_cli"], "commands": ["{python}"], "tiers": ["T0"]}, "adapter codex_cli"),
        ({"adapters": ["scripted"], "commands": ["bash"], "tiers": ["T0"]}, "check command bash"),
        ({"adapters": ["scripted"], "commands": [], "tiers": ["T3"]}, "tier T3 is above"),
        ({"adapters": ["scripted"], "commands": [], "tiers": ["T0"], "max_parallel": 5}, "max_parallel 5"),
    ],
)
def test_the_policy_approver_runs_its_allow_list(facts, reason):
    policy = PolicyApprover(
        {"kinds": ["run"], "adapters": ["scripted"], "commands": ["{python}"], "max_tier": "T2", "max_parallel": 4}
    )
    decision = policy.approve(request("run", facts))
    assert decision.approved is (reason is None)
    if reason:
        assert reason in decision.reason


def test_the_policy_approver_scopes_kinds_and_grants(tmp_path):
    policy = PolicyApprover({"kinds": ["grant"], "roots": [str(tmp_path)]})
    assert not policy.approve(request("run")).approved
    inside = permissions.grant_request(real(tmp_path / "repo"))
    outside = permissions.grant_request(real(tmp_path.parent))
    assert policy.approve(inside).approved and not policy.approve(outside).approved
    with pytest.raises(ValueError, match="Unknown policy fields"):
        PolicyApprover({"kinds": [], "everything": True})


def test_a_policy_file_inside_a_repository_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "policy.toml").write_text('[policy]\nkinds = ["run"]\n', encoding="utf-8")
    (tmp_path / "policy.toml").write_text('[policy]\nkinds = ["run"]\n', encoding="utf-8")
    with pytest.raises(PermissionRefused, match="where agents work"):
        PolicyApprover.from_file(str(repo / "policy.toml"), forbidden_roots=[str(repo)])
    loaded = PolicyApprover.from_file(str(tmp_path / "policy.toml"), forbidden_roots=[str(repo)])
    assert loaded.policy == {"kinds": ["run"]}
    (tmp_path / "bad.toml").write_text('kinds = ["run"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        PolicyApprover.from_file(str(tmp_path / "bad.toml"))
