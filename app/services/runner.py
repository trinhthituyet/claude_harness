"""Run a Claude Agent SDK session for one task and stream its events.

Execution model: async in-process, one asyncio.Task and one ClaudeSDKClient per
run, bounded by a semaphore. A run outlives the HTTP request that started it.
See docs/DESIGN.md section 3.2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    __version__ as sdk_version,
)

from app.config import settings
from app.db import sessionmaker
from app.models import PermissionDecision, Task, TaskRun
from app.security.gate import ApprovalAnswer, Gate
from app.security.path_guard import Verdict
from app.security.policy import RunPolicy
from app.services import messages, options_builder
from app.services.events import run_event_bus
from app.services.snapshot import RunSnapshot, build_snapshot
from app.services.workflow_runner import WorkflowOutcome, WorkflowRunner

log = logging.getLogger("harness.runner")


class PreflightError(RuntimeError):
    """The session's reported configuration does not match what we asked for."""


class Run:
    def __init__(self, run_id: str, snapshot: RunSnapshot) -> None:
        self.id = run_id
        self.snapshot = snapshot
        self.bus = run_event_bus(run_id)
        self.policy = RunPolicy(
            root=snapshot.root,
            trusted_mcp_servers=snapshot.trusted_servers,
            network_enabled=snapshot.network_enabled,
            paranoid=snapshot.paranoid_mode,
        )
        self.gate = Gate(
            self.policy,
            emit=self.bus.emit,
            audit=self._audit,
            approval_timeout_s=snapshot.approval_timeout_s,
        )
        self._client: ClaudeSDKClient | None = None
        self._stderr: list[str] = []
        self._output: list[str] = []
        self._result: ResultMessage | None = None
        self._cancelled = False
        self._preflight_done = False
        self._workflow: WorkflowRunner | None = None
        self._workflow_outcome: WorkflowOutcome | None = None

    # ----------------------------------------------------------------- audit

    async def _audit(
        self,
        *,
        layer: str,
        tool_name: str,
        tool_use_id: str | None,
        agent_id: str | None,
        verdict: Verdict,
        tool_input: dict[str, Any],
    ) -> None:
        async with sessionmaker()() as session:
            session.add(
                PermissionDecision(
                    run_id=self.id,
                    layer=layer,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    agent_id=agent_id,
                    decision=verdict.decision,
                    reason=verdict.reason,
                    candidate_paths_json=verdict.candidates,
                    resolved_paths_json=verdict.resolved,
                    tool_input_json=messages.truncate_input(tool_input),
                )
            )
            await session.commit()

    # ---------------------------------------------------------------- driver

    async def execute(self) -> None:
        await self._set_status("running")
        options, settings_blob = options_builder.build_options(
            self.snapshot,
            self.policy,
            self.gate,
            session_id=self.id,
            stderr_sink=self._capture_stderr,
        )
        await self._record_config(options, settings_blob)
        await self.bus.emit(
            "status",
            {
                "state": "running",
                "project_path": str(self.policy.root),
                "paranoid_mode": self.snapshot.paranoid_mode,
                "sandbox_bash": self.snapshot.sandbox_bash,
                "network_enabled": self.snapshot.network_enabled,
                "lead": self.snapshot.lead.name,
                "teammates": [r.name for r in self.snapshot.teammates],
                "workflow": (
                    self.snapshot.workflow.name if self.snapshot.workflow else None
                ),
            },
        )
        try:
            if self.snapshot.workflow is not None:
                await self._execute_workflow()
            else:
                await self._execute_session(options)
        except asyncio.CancelledError:
            self._cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI and the row
            log.exception("run %s failed", self.id)
            await self.bus.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
            await self._finalize("failed", "exception", str(exc))
            return
        finally:
            self.gate.fail_all_pending("run ended")
            self._client = None
            self._workflow = None

        if self._cancelled:
            await self._finalize("cancelled", "cancelled", None)
        else:
            await self._finalize_from_result()

    async def _execute_session(self, options) -> None:
        """A flat team: one session, the lead delegating to subagents."""
        async with ClaudeSDKClient(options=options) as client:
            self._client = client
            await client.query(self.snapshot.prompt)
            async for message in client.receive_response():
                await self._handle(message)

    async def _execute_workflow(self) -> None:
        """A graph: one session per step, with the runner deciding the path."""
        runner = WorkflowRunner(
            self.snapshot,
            self.policy,
            self.gate,
            self.bus,
            stderr_sink=self._capture_stderr,
        )
        self._workflow = runner
        outcome = await runner.execute()
        self._workflow_outcome = outcome
        self._output = [step.output for step in outcome.steps if step.output]
        # The last step's result stands in for the run, with the graph's totals.
        last = next(
            (step.result for step in reversed(outcome.steps) if step.result is not None), None
        )
        self._result = last

    async def _handle(self, message: Any) -> None:
        if isinstance(message, SystemMessage):
            await self._handle_system(message)
            return
        for type_, payload in messages.translate(message):
            await self.bus.emit(type_, payload)
        self._output.extend(messages.collect_text(message))
        if isinstance(message, ResultMessage):
            self._result = message

    async def _handle_system(self, message: SystemMessage) -> None:
        data = message.data or {}
        if message.subtype == "init" and not self._preflight_done:
            self._preflight_done = True
            await self._preflight(data)
            async with sessionmaker()() as session:
                run = await session.get(TaskRun, self.id)
                if run is not None:
                    run.sdk_session_id = data.get("session_id")
                    run.cli_version = data.get("claude_code_version")
                    await session.commit()
        await self.bus.emit(
            "system",
            {
                "subtype": message.subtype,
                "data": {
                    k: data.get(k)
                    for k in ("cwd", "model", "permissionMode", "tools", "mcp_servers", "skills")
                    if k in data
                },
            },
        )

    async def _preflight(self, init: dict[str, Any]) -> None:
        """Assert the session really got the configuration we asked for.

        The settings blob is interpreted by the CLI, which silently ignores
        settings that fail validation in headless mode — so a typo could remove the
        deny rules with no error anywhere. Checking the init event is cheap
        insurance. See docs/DESIGN.md 3.4.
        """
        problems: list[str] = []
        expected_cwd = str(self.policy.root)
        actual_cwd = init.get("cwd")
        if actual_cwd and actual_cwd != expected_cwd:
            problems.append(f"cwd is {actual_cwd!r}, expected {expected_cwd!r}")

        expected_mode = "dontAsk" if self.snapshot.paranoid_mode else "default"
        actual_mode = init.get("permissionMode")
        if actual_mode and actual_mode != expected_mode:
            problems.append(f"permissionMode is {actual_mode!r}, expected {expected_mode!r}")

        tools = init.get("tools") or []
        if not self.snapshot.network_enabled:
            leaked = [t for t in tools if t in options_builder.NETWORK_TOOLS]
            if leaked:
                problems.append(f"network tools present despite being disabled: {leaked}")

        servers = init.get("mcp_servers")
        if isinstance(servers, list):
            actual = {s.get("name") for s in servers if isinstance(s, dict)}
            expected = set(self.snapshot.mcp_servers)
            if actual - expected:
                problems.append(
                    f"unexpected MCP servers loaded: {sorted(actual - expected)} "
                    "(strict_mcp_config did not take effect)"
                )
            for server in servers:
                if isinstance(server, dict):
                    await self.bus.emit(
                        "system",
                        {"subtype": "mcp_status", "data": {
                            "name": server.get("name"), "status": server.get("status")}},
                    )

        if problems:
            raise PreflightError("; ".join(problems))

    # ------------------------------------------------------------ bookkeeping

    def _capture_stderr(self, line: str) -> None:
        if len(self._stderr) < 200:
            self._stderr.append(line)
        if "sandbox_apply: Operation not permitted" in line:
            # Nested sandboxing failed: Bash would run unconfined. Do not continue.
            asyncio.create_task(self._abort_unsandboxed())

    async def _abort_unsandboxed(self) -> None:
        await self.bus.emit(
            "error",
            {
                "message": (
                    "the OS sandbox could not be applied (sandbox_apply: Operation not "
                    "permitted), so Bash would run unconfined. Aborting. Run the harness "
                    "outside an existing sandbox, or turn off sandbox_bash to accept the risk."
                )
            },
        )
        await self.cancel()

    async def _record_config(self, options, settings_blob: dict[str, Any]) -> None:
        async with sessionmaker()() as session:
            run = await session.get(TaskRun, self.id)
            if run is None:
                return
            run.sdk_version = sdk_version
            run.settings_json = settings_blob
            run.options_json = {
                "cwd": options.cwd,
                "permission_mode": options.permission_mode,
                "allowed_tools": options.allowed_tools,
                "disallowed_tools": options.disallowed_tools,
                "setting_sources": options.setting_sources,
                "skills": options.skills,
                "strict_mcp_config": options.strict_mcp_config,
                "mcp_servers": sorted(options.mcp_servers or {}),
                "agents": sorted((options.agents or {}).keys()),
                "model": options.model,
                "sandbox": options.sandbox,
                "max_turns": options.max_turns,
                "max_budget_usd": options.max_budget_usd,
                "env_keys": sorted(options.env.keys()),
            }
            await session.commit()

    async def _set_status(self, status: str) -> None:
        async with sessionmaker()() as session:
            run = await session.get(TaskRun, self.id)
            if run is not None:
                run.status = status
                await session.commit()

    async def _finalize_from_result(self) -> None:
        outcome = self._workflow_outcome
        if outcome is not None:
            # A workflow's verdict is how the walk ended, not how its last step did.
            if not outcome.steps:
                await self._finalize("failed", "exception", "the workflow ran no steps")
                return
            status = "completed" if outcome.reason == "end" else "failed"
            if outcome.reason == "cancelled":
                status = "cancelled"
            await self._finalize(
                status,
                f"workflow_{outcome.reason}",
                None,
                result=self._result,
                totals=(outcome.num_turns, outcome.total_cost_usd),
            )
            return

        result = self._result
        if result is None:
            await self._finalize("failed", "exception", "session ended without a result")
            return
        status = "failed" if result.is_error else "completed"
        reason = result.subtype or ("error" if result.is_error else "success")
        await self._finalize(status, reason, None, result=result)

    async def _finalize(
        self,
        status: str,
        exit_reason: str,
        error_text: str | None,
        result: ResultMessage | None = None,
        totals: tuple[int, float] | None = None,
    ) -> None:
        async with sessionmaker()() as session:
            run = await session.get(TaskRun, self.id)
            if run is not None:
                run.status = status
                run.exit_reason = exit_reason
                run.ended_at = datetime.now(timezone.utc)
                run.output_text = "\n\n".join(self._output)[-20000:]
                if error_text:
                    stderr = "\n".join(self._stderr[-20:])
                    run.error_text = f"{error_text}\n\n{stderr}" if stderr else error_text
                if totals is not None:
                    run.num_turns, run.total_cost_usd = totals
                elif result is not None:
                    run.num_turns = result.num_turns
                    run.total_cost_usd = result.total_cost_usd
                    run.duration_ms = result.duration_ms
                await session.commit()
        await self._reconcile_denials(result)
        await self.bus.emit("status", {"state": status, "exit_reason": exit_reason})
        self.bus.close()

    async def _reconcile_denials(self, result: ResultMessage | None) -> None:
        """Cross-check the CLI's denial list against what our layers recorded.

        A denial the CLI reports that we never saw means something was blocked by a
        mechanism outside the gate — worth surfacing rather than silently trusting.
        """
        reported = list(result.permission_denials or []) if result else []
        if not reported:
            return
        async with sessionmaker()() as session:
            from sqlalchemy import select

            rows = (
                await session.execute(
                    select(PermissionDecision.tool_use_id).where(
                        PermissionDecision.run_id == self.id,
                        PermissionDecision.decision == "deny",
                    )
                )
            ).scalars().all()
        seen = {r for r in rows if r}
        unseen = [
            d
            for d in reported
            if isinstance(d, dict) and d.get("tool_use_id") and d["tool_use_id"] not in seen
        ]
        await self.bus.emit(
            "permission",
            {
                "layer": "reconcile",
                "reported": len(reported),
                "unattributed": len(unseen),
                "note": (
                    "denials the harness gate did not record (blocked by a deny rule "
                    "or by the CLI itself)"
                    if unseen
                    else "all denials accounted for"
                ),
            },
        )

    # --------------------------------------------------------------- controls

    async def cancel(self) -> None:
        self._cancelled = True
        self.gate.fail_all_pending("run cancelled")
        workflow = self._workflow
        if workflow is not None:
            with contextlib.suppress(Exception):
                await workflow.cancel()
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.interrupt()

    def answer_approval(self, request_id: str, answer: ApprovalAnswer) -> bool:
        return self.gate.resolve(request_id, answer)

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [req.public() for req in self.gate.pending.values()]


class RunManager:
    """Owns live runs and bounds how many sessions exist at once."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._limit = max_concurrent or settings.max_concurrent_runs
        self._semaphore = asyncio.Semaphore(self._limit)
        self._runs: dict[str, Run] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def live_ids(self) -> list[str]:
        return list(self._runs)

    def pending_approvals(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for run_id, run in self._runs.items():
            for approval in run.pending_approvals():
                out.append({"run_id": run_id, **approval})
        return out

    async def start(self, session, task: Task) -> str:
        """Create the run row and launch it. Raises on invalid configuration."""
        snapshot = await build_snapshot(session, task)
        run_id = str(uuid.uuid4())
        session.add(
            TaskRun(
                id=run_id,
                task_id=task.id,
                status="queued",
                prompt_snapshot=snapshot.prompt,
                project_path_snapshot=str(snapshot.root),
                team_snapshot_json=snapshot.team_public(),
                workflow_snapshot_json=(
                    snapshot.workflow.public() if snapshot.workflow else {}
                ),
                model_snapshot_json=snapshot.model.public(),
                skills_snapshot_json=list(snapshot.skills),
                mcps_snapshot_json=sorted(snapshot.mcp_servers),
            )
        )
        await session.commit()

        run = Run(run_id, snapshot)
        self._runs[run_id] = run
        self._tasks[run_id] = asyncio.create_task(self._supervise(run), name=f"run:{run_id}")
        return run_id

    async def _supervise(self, run: Run) -> None:
        try:
            async with self._semaphore:
                await run.execute()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await run._finalize("cancelled", "cancelled", None)
            raise
        except Exception:  # pragma: no cover - execute() already handles its own
            log.exception("supervisor for run %s failed", run.id)
        finally:
            self._runs.pop(run.id, None)
            self._tasks.pop(run.id, None)

    async def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None:
            return False
        await run.cancel()
        return True

    async def shutdown(self) -> None:
        for run in list(self._runs.values()):
            with contextlib.suppress(Exception):
                await run.cancel()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


manager = RunManager()
