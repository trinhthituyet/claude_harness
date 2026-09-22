"""The gate: the PreToolUse hook and the permission callback.

Three properties here are load-bearing, and each is covered by a named test in
tests/test_gate.py:

1. ``pre_tool_use`` catches *every* exception and turns it into a **deny**. A
   raising hook callback is fail-open in the SDK (verified; see docs/DESIGN.md
   section 4.1 probe P5), so an ``except`` that re-raises or returns ``{}`` would
   be a security hole.
2. The allow path returns ``{}`` — "no decision" — never an ``allow`` decision.
   An allow decision would skip ``can_use_tool`` and the deny rules underneath.
3. ``can_use_tool`` has no ``try``/``except``. An exception there is already a
   denial (probe P6), and swallowing it would convert a fail-closed path into
   whatever the handler decided.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from app.security import path_guard
from app.security.policy import RunPolicy

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
Audit = Callable[..., Awaitable[None]]


@dataclass
class ApprovalAnswer:
    approved: bool
    remember: bool = False
    reason: str = ""


@dataclass
class PendingApproval:
    id: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    resolved_paths: list[str]
    deadline: datetime
    future: asyncio.Future[ApprovalAnswer]
    agent_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def public(self) -> dict[str, Any]:
        """The shape sent to the browser."""
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "reason": self.reason,
            "resolved_paths": self.resolved_paths,
            "agent_id": self.agent_id,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat(),
        }


def _deny_hook(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _ask_hook(reason: str) -> dict[str, Any]:
    """Force the call onto the prompt path so ``can_use_tool`` decides it."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


class Gate:
    """Per-run permission enforcement, shared by the hook and the callback."""

    def __init__(
        self,
        policy: RunPolicy,
        *,
        emit: Emit,
        audit: Audit,
        approval_timeout_s: int = 300,
    ) -> None:
        self.policy = policy
        self._emit = emit
        self._audit = audit
        self.approval_timeout_s = approval_timeout_s
        self.pending: dict[str, PendingApproval] = {}

    # ------------------------------------------------------------------ hook

    async def pre_tool_use(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Universal chokepoint: fires for every tool call, subagents included."""
        try:
            tool_name = payload.get("tool_name", "")
            tool_input = payload.get("tool_input") or {}
            agent_id = payload.get("agent_id")
            verdict = path_guard.check(tool_name, tool_input, self.policy)
            await self._audit(
                layer="hook",
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                agent_id=agent_id,
                verdict=verdict,
                tool_input=tool_input,
            )
            if verdict.decision == "deny":
                await self._emit(
                    "permission",
                    {
                        "layer": "hook",
                        "decision": "deny",
                        "tool_name": tool_name,
                        "reason": verdict.reason,
                    },
                )
                return _deny_hook(verdict.reason)
            if verdict.decision == "ask":
                # Force the prompt path even if a rule would have auto-allowed,
                # so can_use_tool (and therefore the user) always gets a say.
                return _ask_hook(verdict.reason)
            return {}
        except BaseException as exc:  # noqa: BLE001 - see module docstring, property 1
            reason = f"harness guard internal error: {exc!r}"
            try:
                await self._audit(
                    layer="hook",
                    tool_name=payload.get("tool_name", "<unknown>"),
                    tool_use_id=tool_use_id,
                    agent_id=None,
                    verdict=path_guard.Verdict("deny", reason),
                    tool_input=payload.get("tool_input") or {},
                )
            except BaseException:  # noqa: BLE001 - auditing must never unblock a denial
                pass
            return _deny_hook(reason)

    # -------------------------------------------------------------- callback

    async def can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """The SDK's approval surface. Intentionally not wrapped in try/except."""
        verdict = path_guard.check(tool_name, tool_input, self.policy)
        if context.blocked_path and verdict.decision == "allow":
            # The CLI's own shell analysis spotted a path we could not extract
            # (typically a Bash redirection target). Treat it as an escape.
            resolved = path_guard.resolve_candidate(context.blocked_path, self.policy.root)
            if not path_guard.contained_in_any(resolved, self.policy.write_roots):
                verdict = path_guard.Verdict(
                    "ask",
                    f"{tool_name} targets {resolved}, outside the project boundary "
                    "(reported by the CLI as a blocked path)",
                    [context.blocked_path],
                    [str(resolved)],
                )

        await self._audit(
            layer="callback",
            tool_name=tool_name,
            tool_use_id=context.tool_use_id,
            agent_id=context.agent_id,
            verdict=verdict,
            tool_input=tool_input,
        )

        if verdict.decision == "allow":
            return PermissionResultAllow()
        if verdict.decision == "deny":
            await self._emit(
                "permission",
                {
                    "layer": "callback",
                    "decision": "deny",
                    "tool_name": tool_name,
                    "reason": verdict.reason,
                },
            )
            return PermissionResultDeny(message=verdict.reason)
        return await self._request_approval(tool_name, tool_input, context, verdict)

    # -------------------------------------------------------------- approval

    async def _request_approval(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
        verdict: path_guard.Verdict,
    ) -> PermissionResultAllow | PermissionResultDeny:
        loop = asyncio.get_running_loop()
        request = PendingApproval(
            id=str(uuid.uuid4()),
            tool_name=tool_name,
            tool_input=tool_input,
            reason=verdict.reason,
            resolved_paths=verdict.resolved,
            agent_id=context.agent_id,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.approval_timeout_s),
            future=loop.create_future(),
        )
        self.pending[request.id] = request
        await self._emit("permission_request", request.public())
        try:
            answer = await asyncio.wait_for(request.future, self.approval_timeout_s)
        except asyncio.TimeoutError:
            await self._emit("permission_resolved", {"id": request.id, "outcome": "timeout"})
            await self._audit(
                layer="ui",
                tool_name=tool_name,
                tool_use_id=context.tool_use_id,
                agent_id=context.agent_id,
                verdict=path_guard.Verdict("deny", "approval timed out", verdict.candidates,
                                           verdict.resolved),
                tool_input=tool_input,
            )
            return PermissionResultDeny(message="approval timed out; denied")
        except asyncio.CancelledError:
            await self._emit("permission_resolved", {"id": request.id, "outcome": "cancelled"})
            raise
        finally:
            self.pending.pop(request.id, None)

        if answer.approved and answer.remember:
            for raw in verdict.resolved:
                candidate = Path(raw)
                self.policy.add_session_root(
                    candidate if candidate.is_dir() else candidate.parent
                )

        outcome = "approved" if answer.approved else "denied"
        await self._emit("permission_resolved", {"id": request.id, "outcome": outcome})
        await self._audit(
            layer="ui",
            tool_name=tool_name,
            tool_use_id=context.tool_use_id,
            agent_id=context.agent_id,
            verdict=path_guard.Verdict(
                "allow" if answer.approved else "deny",
                answer.reason or f"{outcome} by user",
                verdict.candidates,
                verdict.resolved,
            ),
            tool_input=tool_input,
        )
        if answer.approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message=answer.reason or "denied by user")

    def resolve(self, request_id: str, answer: ApprovalAnswer) -> bool:
        """Answer a pending approval. Returns False if it is unknown or already done."""
        request = self.pending.get(request_id)
        if request is None or request.future.done():
            return False
        request.future.set_result(answer)
        return True

    def fail_all_pending(self, reason: str) -> None:
        """Deny every waiting approval, so cancelling a run never wedges a task."""
        for request in list(self.pending.values()):
            if not request.future.done():
                request.future.set_result(ApprovalAnswer(approved=False, reason=reason))
