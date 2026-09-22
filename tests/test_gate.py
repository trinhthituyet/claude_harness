"""Gate behaviour, especially the fail-closed properties.

The first test here is the most important in the suite: an SDK PreToolUse hook that
raises is fail-open (the tool runs anyway), so the hook must convert every internal
error into an explicit deny. See docs/DESIGN.md 4.1 probe P5 and 4.4.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.security import path_guard
from app.security.gate import ApprovalAnswer, Gate
from app.security.policy import RunPolicy


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.audits: list[dict] = []

    async def emit(self, type_: str, payload: dict) -> None:
        self.events.append((type_, payload))

    async def audit(self, **kwargs) -> None:
        self.audits.append(kwargs)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return Path(os.path.realpath(project))


@pytest.fixture
def gate(root: Path) -> tuple[Gate, Recorder]:
    recorder = Recorder()
    return (
        Gate(RunPolicy(root=root), emit=recorder.emit, audit=recorder.audit,
             approval_timeout_s=1),
        recorder,
    )


def hook_payload(tool: str, tool_input: dict) -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "hook_event_name": "PreToolUse"}


def decision_of(output: dict) -> str | None:
    return (output.get("hookSpecificOutput") or {}).get("permissionDecision")


# --------------------------------------------------- property 1: fail closed


async def test_hook_denies_when_the_guard_raises(gate, monkeypatch):
    """A raising guard must produce a deny, never a pass-through."""
    instance, recorder = gate

    def explode(*args, **kwargs):
        raise RuntimeError("guard blew up")

    monkeypatch.setattr(path_guard, "check", explode)
    output = await instance.pre_tool_use(hook_payload("Write", {"file_path": "/tmp/x"}), "t1", {})
    assert decision_of(output) == "deny"
    assert "internal error" in output["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hook_denies_even_when_auditing_also_fails(gate, monkeypatch):
    instance, _ = gate

    def explode(*args, **kwargs):
        raise RuntimeError("guard blew up")

    async def bad_audit(**kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(path_guard, "check", explode)
    instance._audit = bad_audit
    output = await instance.pre_tool_use(hook_payload("Write", {"file_path": "/tmp/x"}), "t1", {})
    assert decision_of(output) == "deny"


# ------------------------------------------- property 2: allow means no decision


async def test_hook_allow_path_returns_no_decision(gate, root):
    instance, _ = gate
    output = await instance.pre_tool_use(
        hook_payload("Write", {"file_path": str(root / "a.txt")}), "t1", {}
    )
    assert output == {}


async def test_hook_denies_unknown_tool(gate):
    instance, _ = gate
    output = await instance.pre_tool_use(hook_payload("MysteryTool", {}), "t1", {})
    assert decision_of(output) == "deny"


async def test_hook_asks_for_out_of_root_write(gate, tmp_path):
    instance, _ = gate
    output = await instance.pre_tool_use(
        hook_payload("Write", {"file_path": str(tmp_path / "escape.txt")}), "t1", {}
    )
    assert decision_of(output) == "ask"


# ----------------------------------------------------------- approval flow


class FakeContext:
    def __init__(self) -> None:
        self.tool_use_id = "t1"
        self.agent_id = None
        self.blocked_path = None
        self.suggestions: list = []


async def test_callback_allows_inside_root(gate, root):
    instance, _ = gate
    result = await instance.can_use_tool("Write", {"file_path": str(root / "a.txt")}, FakeContext())
    assert result.behavior == "allow"


async def test_callback_denies_untrusted_mcp_without_asking(gate):
    instance, recorder = gate
    result = await instance.can_use_tool("mcp__files__write", {}, FakeContext())
    assert result.behavior == "deny"
    assert not instance.pending


async def test_out_of_root_write_asks_and_user_can_approve(gate, tmp_path):
    instance, recorder = gate
    target = str(tmp_path / "outside" / "x.txt")
    task = asyncio.create_task(
        instance.can_use_tool("Write", {"file_path": target}, FakeContext())
    )
    await asyncio.sleep(0.05)
    assert len(instance.pending) == 1
    request_id = next(iter(instance.pending))
    assert instance.resolve(request_id, ApprovalAnswer(approved=True))
    result = await task
    assert result.behavior == "allow"
    assert any(type_ == "permission_request" for type_, _ in recorder.events)


async def test_user_denial_is_honoured(gate, tmp_path):
    instance, _ = gate
    task = asyncio.create_task(
        instance.can_use_tool("Write", {"file_path": str(tmp_path / "x.txt")}, FakeContext())
    )
    await asyncio.sleep(0.05)
    request_id = next(iter(instance.pending))
    instance.resolve(request_id, ApprovalAnswer(approved=False, reason="nope"))
    result = await task
    assert result.behavior == "deny"
    assert result.message == "nope"


async def test_timeout_denies(gate, tmp_path):
    instance, _ = gate  # approval_timeout_s = 1
    result = await instance.can_use_tool(
        "Write", {"file_path": str(tmp_path / "x.txt")}, FakeContext()
    )
    assert result.behavior == "deny"
    assert "timed out" in result.message
    assert not instance.pending


async def test_remember_widens_the_boundary_for_the_run(gate, tmp_path):
    instance, _ = gate
    outside = tmp_path / "outside"
    outside.mkdir()
    first = asyncio.create_task(
        instance.can_use_tool("Write", {"file_path": str(outside / "a.txt")}, FakeContext())
    )
    await asyncio.sleep(0.05)
    instance.resolve(next(iter(instance.pending)), ApprovalAnswer(approved=True, remember=True))
    assert (await first).behavior == "allow"

    # A second write in the same directory must not ask again.
    second = await instance.can_use_tool(
        "Write", {"file_path": str(outside / "b.txt")}, FakeContext()
    )
    assert second.behavior == "allow"
    assert not instance.pending


async def test_fail_all_pending_denies_waiters(gate, tmp_path):
    instance, _ = gate
    task = asyncio.create_task(
        instance.can_use_tool("Write", {"file_path": str(tmp_path / "x.txt")}, FakeContext())
    )
    await asyncio.sleep(0.05)
    instance.fail_all_pending("run cancelled")
    result = await task
    assert result.behavior == "deny"
    assert result.message == "run cancelled"


async def test_blocked_path_from_the_cli_is_escalated(gate, tmp_path):
    """Bash inputs carry no path field, so ctx.blocked_path is the only signal."""
    instance, _ = gate
    context = FakeContext()
    context.blocked_path = str(tmp_path / "outside" / "x.txt")
    task = asyncio.create_task(
        instance.can_use_tool("Bash", {"command": "printf hi > x"}, context)
    )
    await asyncio.sleep(0.05)
    assert len(instance.pending) == 1
    instance.resolve(next(iter(instance.pending)), ApprovalAnswer(approved=False))
    assert (await task).behavior == "deny"
