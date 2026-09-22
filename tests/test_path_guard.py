"""Containment tests. This is the crux of the security boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.security import path_guard
from app.security.policy import PolicyError, RunPolicy, resolve_project_root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return Path(os.path.realpath(project))


@pytest.fixture
def policy(root: Path) -> RunPolicy:
    return RunPolicy(root=root)


def write(tool_input: dict, policy: RunPolicy, tool: str = "Write"):
    return path_guard.check(tool, tool_input, policy)


# ------------------------------------------------------------------ resolution


def test_resolve_project_root_resolves_symlinks(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert resolve_project_root(str(link)) == Path(os.path.realpath(real))


def test_resolve_project_root_rejects_relative():
    with pytest.raises(PolicyError, match="absolute"):
        resolve_project_root("relative/path")


def test_resolve_project_root_rejects_missing(tmp_path: Path):
    with pytest.raises(PolicyError, match="does not exist"):
        resolve_project_root(str(tmp_path / "nope"))


def test_resolve_project_root_rejects_file(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(PolicyError, match="not a directory"):
        resolve_project_root(str(target))


def test_resolve_project_root_rejects_home():
    with pytest.raises(PolicyError, match="home directory"):
        resolve_project_root(str(Path.home()))


def test_resolve_project_root_rejects_rule_unsafe_chars(tmp_path: Path):
    awkward = tmp_path / "pro(ject)"
    awkward.mkdir()
    with pytest.raises(PolicyError, match="permission rule"):
        resolve_project_root(str(awkward))


def test_resolve_candidate_handles_missing_tail(root: Path):
    resolved = path_guard.resolve_candidate("a/b/c.txt", root)
    assert resolved == root / "a" / "b" / "c.txt"


def test_resolve_candidate_follows_symlinked_parent(root: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    resolved = path_guard.resolve_candidate(str(root / "link" / "x.txt"), root)
    assert resolved == Path(os.path.realpath(outside)) / "x.txt"


# ----------------------------------------------------------------- containment


def test_contained_rejects_string_prefix_sibling(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    sibling = tmp_path / "proj-evil"
    sibling.mkdir()
    assert not path_guard.contained(sibling / "f.txt", root)
    assert path_guard.contained(root / "f.txt", root)


def test_contained_accepts_root_itself(root: Path):
    assert path_guard.contained(root, root)


# ------------------------------------------------------------------ write gate


def test_write_inside_root_allowed(root: Path, policy: RunPolicy):
    assert write({"file_path": str(root / "a.txt")}, policy).decision == "allow"


def test_write_outside_root_asks(root: Path, tmp_path: Path, policy: RunPolicy):
    verdict = write({"file_path": str(tmp_path / "escape.txt")}, policy)
    assert verdict.decision == "ask"
    assert "outside the project boundary" in verdict.reason


def test_write_traversal_asks(root: Path, policy: RunPolicy):
    assert write({"file_path": "../escape.txt"}, policy).decision == "ask"


def test_write_through_symlinked_parent_asks(root: Path, tmp_path: Path, policy: RunPolicy):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    verdict = write({"file_path": str(root / "link" / "x.txt")}, policy)
    assert verdict.decision == "ask"


def test_write_to_home_tilde_asks(root: Path, policy: RunPolicy):
    assert write({"file_path": "~/escape.txt"}, policy).decision == "ask"


def test_write_without_path_denied(policy: RunPolicy):
    assert write({}, policy).decision == "deny"


def test_edit_and_notebook_edit_are_write_gated(root: Path, tmp_path: Path, policy: RunPolicy):
    assert write({"file_path": str(tmp_path / "x")}, policy, "Edit").decision == "ask"
    assert write({"notebook_path": str(tmp_path / "n.ipynb")}, policy, "NotebookEdit").decision == "ask"


def test_session_root_widens_write_boundary(root: Path, tmp_path: Path, policy: RunPolicy):
    extra = tmp_path / "extra"
    extra.mkdir()
    target = {"file_path": str(extra / "x.txt")}
    assert write(target, policy).decision == "ask"
    policy.add_session_root(extra)
    assert write(target, policy).decision == "allow"


# ------------------------------------------------------------------ read gate


def test_reads_are_not_path_confined(tmp_path: Path, policy: RunPolicy):
    verdict = path_guard.check("Read", {"file_path": str(tmp_path / "anything")}, policy)
    assert verdict.decision == "allow"


# ------------------------------------------------------- other tool classes


def test_unknown_tool_denied(policy: RunPolicy):
    assert path_guard.check("SomeNewTool", {}, policy).decision == "deny"


def test_untrusted_mcp_tool_denied(policy: RunPolicy):
    verdict = path_guard.check("mcp__files__write", {"path": "/etc/passwd"}, policy)
    assert verdict.decision == "deny"
    assert "not marked trusted" in verdict.reason


def test_trusted_mcp_tool_allowed(root: Path):
    policy = RunPolicy(root=root, trusted_mcp_servers=frozenset({"files"}))
    assert path_guard.check("mcp__files__write", {}, policy).decision == "allow"


def test_network_tools_follow_the_task_switch(root: Path):
    off = RunPolicy(root=root, network_enabled=False)
    on = RunPolicy(root=root, network_enabled=True)
    assert path_guard.check("WebFetch", {"url": "https://x"}, off).decision == "deny"
    assert path_guard.check("WebFetch", {"url": "https://x"}, on).decision == "allow"


def test_bash_red_flags_denied(policy: RunPolicy):
    assert path_guard.check("Bash", {"command": "sudo rm -rf /"}, policy).decision == "deny"
    assert path_guard.check("Bash", {"command": "cat ~/.ssh/id_rsa"}, policy).decision == "deny"
    assert path_guard.check("Bash", {"command": "ls -la"}, policy).decision == "allow"


def test_nul_byte_path_does_not_crash(policy: RunPolicy):
    verdict = write({"file_path": "bad\x00name.txt"}, policy)
    assert verdict.decision in {"ask", "deny"}
