"""Walk a workflow graph: parallel supersteps, named artifacts, bounded loops.

The execution model follows LangGraph's (see ``docs/example_graph.py``), because that is
what the shapes people actually draw require:

* A **frontier** of nodes is active at once. Everything in it runs concurrently, then
  their selected successors are unioned and deduplicated to form the next frontier.
  Deduplication *is* the join: when architect, designer and tester all point at
  ``manager_review``, the next frontier is ``{manager_review}`` and it runs once.
* A node either **fans out** — several unconditional edges, all taken — or **branches**,
  where a tool-less query with structured output picks *which* of its conditional edges
  apply. A branch may select several, which is how "only the flagged roles re-run" works.
* Each step files its output under a **named artifact** (``arch_doc``, ``code``, …), so a
  later step can be handed exactly the documents it needs rather than a positional digest.
* The router may attach **feedback per target**, so a role re-running after review sees
  the points addressed to it and not the others'.
* Budgets are per-node visits plus a whole-run superstep cap. Exhausting either goes to
  the workflow's **escalation node** when it has one, instead of simply failing. An edge
  can **reset** named nodes' visit counts, which is how the example graph gives the review
  loop a fresh budget when work comes back from the project manager.

Every step runs through the same :class:`~app.security.gate.Gate` as a flat task run, so
path confinement and approval prompts apply identically.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from app.security.gate import Gate
from app.security.policy import RunPolicy
from app.services import messages, options_builder
from app.services.events import EventBus
from app.services.snapshot import RunSnapshot, WorkflowEdgeSpec, WorkflowNodeSpec, WorkflowSpec

log = logging.getLogger("harness.workflow")

#: Characters of an artifact carried into a later step's briefing.
DIGEST_CHARS = 4000
#: How many steps of one superstep may run at once.
MAX_PARALLEL_STEPS = 3


class WorkflowError(RuntimeError):
    pass


@dataclasses.dataclass
class StepResult:
    node_key: str
    role: str
    visit: int
    output: str
    result: ResultMessage | None
    artifact: str = ""


@dataclasses.dataclass
class WorkflowOutcome:
    steps: list[StepResult]
    reason: str  # end | max_steps | max_visits | escalated | cancelled | dead_end
    total_cost_usd: float
    num_turns: int
    path: list[list[str]] = dataclasses.field(default_factory=list)

    @property
    def final_output(self) -> str:
        return self.steps[-1].output if self.steps else ""


def _parse_json(text: Any) -> dict[str, Any] | None:
    """Best-effort JSON from a result string, for when structured output is empty."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _artifact_block(artifacts: dict[str, StepResult]) -> str:
    """The named outputs produced so far, the way the example graph's state reads."""
    if not artifacts:
        return ""
    lines = ["", "## What the team has produced so far", ""]
    for name, step in artifacts.items():
        lines.append(f"### {name} — by {step.role} ({step.node_key})")
        lines.append(step.output[-DIGEST_CHARS:] or "(no output)")
        lines.append("")
    return "\n".join(lines)


def _step_prompt(
    snapshot: RunSnapshot,
    node: WorkflowNodeSpec,
    artifacts: dict[str, StepResult],
    feedback: list[str],
) -> str:
    parts = ["# Goal", "", snapshot.prompt.strip()]
    if node.instructions.strip():
        parts += ["", "## Your step", "", node.instructions.strip()]
    if feedback:
        parts += [
            "",
            "## Feedback addressed to you",
            "",
            "Your previous output was reviewed. Fix these points and return the complete "
            "updated version:",
            "",
            *(f"- {item}" for item in feedback),
        ]
    block = _artifact_block(artifacts)
    if block:
        parts.append(block)
    parts += [
        "",
        f"Do your part of this goal now. Your output is filed as `{node.artifact}` for "
        "the rest of the team. Finish by stating what you produced.",
    ]
    return "\n".join(parts)


class WorkflowRunner:
    """Drives one workflow run inside an existing task run."""

    def __init__(
        self,
        snapshot: RunSnapshot,
        policy: RunPolicy,
        gate: Gate,
        bus: EventBus,
        *,
        stderr_sink=None,
    ) -> None:
        if snapshot.workflow is None:
            raise WorkflowError("this snapshot has no workflow")
        self.snapshot = snapshot
        self.workflow: WorkflowSpec = snapshot.workflow
        self.policy = policy
        self.gate = gate
        self.bus = bus
        self._stderr_sink = stderr_sink
        self.cancelled = False
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_STEPS)

    # ---------------------------------------------------------------- options

    def _options_for(self, node: WorkflowNodeSpec) -> tuple[ClaudeAgentOptions, dict[str, Any]]:
        """Build options for one step: the same task options, but this role alone.

        Reusing ``options_builder`` rather than assembling options here keeps every
        security choice — empty ``allowed_tools``, deny rules, sandbox, isolation — in
        one place, so a workflow step can never be configured more loosely than a
        plain run.
        """
        extra = (
            f"\n\n## This step\n\n{node.instructions.strip()}"
            if node.instructions.strip()
            else ""
        )
        role = dataclasses.replace(node.role, system_prompt=node.role.system_prompt + extra)
        step_snapshot = dataclasses.replace(self.snapshot, roles=[role], workflow=None)
        return options_builder.build_options(
            step_snapshot,
            self.policy,
            self.gate,
            session_id=str(uuid.uuid4()),
            stderr_sink=self._stderr_sink,
        )

    # ------------------------------------------------------------------ steps

    async def _run_node(
        self,
        node: WorkflowNodeSpec,
        visit: int,
        artifacts: dict[str, StepResult],
        feedback: list[str],
    ) -> StepResult:
        async with self._semaphore:
            options, _ = self._options_for(node)
            prompt = _step_prompt(self.snapshot, node, artifacts, feedback)
            output: list[str] = []
            result: ResultMessage | None = None

            await self.bus.emit(
                "node_started",
                {"node": node.key, "role": node.role.name, "visit": visit,
                 "max_visits": node.max_visits, "artifact": node.artifact,
                 "feedback": feedback},
            )
            log.info(
                "workflow %s: node %s (visit %d) started", self.workflow.name, node.key, visit
            )

            try:
                async with ClaudeSDKClient(options=options) as client:
                    self._clients[node.key] = client
                    await client.query(prompt)
                    async for message in client.receive_response():
                        for type_, payload in messages.translate(message, node=node.key):
                            await self.bus.emit(type_, payload)
                        output.extend(messages.collect_text(message))
                        if isinstance(message, ResultMessage):
                            result = message
            finally:
                self._clients.pop(node.key, None)

            text = "\n\n".join(output).strip()
            await self.bus.emit(
                "node_finished",
                {"node": node.key, "role": node.role.name, "visit": visit,
                 "artifact": node.artifact, "chars": len(text),
                 "cost_usd": result.total_cost_usd if result else None},
            )
            return StepResult(node.key, node.role.name, visit, text, result, node.artifact)

    # --------------------------------------------------------------- routing

    async def _choose_edges(
        self, node: WorkflowNodeSpec, edges: list[WorkflowEdgeSpec], step: StepResult
    ) -> tuple[list[tuple[WorkflowEdgeSpec, str]], str]:
        """Which outgoing edges to follow, each with any feedback for its target.

        Returns ``(selected, why)``. An empty selection finishes this branch. Several
        edges may come back: an unconditional fan-out takes all of them, and a branch may
        legitimately select more than one — that is how only the flagged roles re-run.

        The reasoning is returned rather than stored on the runner, because several nodes
        of one superstep are routed in turn and shared mutable state would cross wires.
        """
        if not edges:
            return [], ""
        if self.workflow.fans_out(node.key):
            return [(edge, "") for edge in edges], "fan-out: every branch runs"
        if len(edges) == 1 and not edges[0].conditional:
            return [(edges[0], "")], ""

        default = next((edge for edge in edges if edge.is_default), None)
        labels = [edge.label for edge in edges]

        options, _ = self._options_for(node)
        # A routing decision must not touch anything: no tools, and an answer
        # constrained to this node's own edge labels.
        #
        # ``hooks=None`` is measured, not stylistic: with a PreToolUse hook registered
        # the CLI returns an empty structured result, and routing silently falls back.
        # Dropping it costs nothing because this session has no tools for the hook to
        # fire on. ``can_use_tool`` stays — removing that instead produced
        # error_max_structured_output_retries.
        router_options = dataclasses.replace(
            options,
            tools=[],
            mcp_servers={},
            agents=None,
            hooks=None,
            max_turns=4,
            system_prompt=(
                "You route a workflow. Read the step's output and choose every edge that "
                "applies — usually exactly one, but select several when the work must go "
                "to several places at once. For each edge you pick, write the feedback "
                "the receiving role needs. Answer only with the structured result."
            ),
            output_format={
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "edges": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "enum": labels},
                                    "feedback": {"type": "string"},
                                },
                                "required": ["label", "feedback"],
                                "additionalProperties": False,
                            },
                        },
                        "why": {"type": "string"},
                    },
                    "required": ["edges", "why"],
                    "additionalProperties": False,
                },
            },
            session_id=str(uuid.uuid4()),
        )

        described = "\n".join(
            f"- {edge.label} → {edge.to_key or 'END'}: "
            f"{edge.condition.strip() or 'the fallback when nothing else fits'}"
            for edge in edges
        )
        prompt = (
            f"The step '{node.key}' (role: {step.role}) produced this output:\n\n"
            f"{step.output[-DIGEST_CHARS:] or '(no output)'}\n\n"
            f"Available edges:\n{described}\n\nWhich edges apply?"
        )

        chosen: list[tuple[WorkflowEdgeSpec, str]] = []
        why = ""
        raw = ""
        try:
            async with ClaudeSDKClient(options=router_options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        raw = f"{message.subtype}: {str(message.result)[:200]}"
                        data = message.structured_output
                        if not isinstance(data, dict):
                            data = _parse_json(message.result)
                        if isinstance(data, dict):
                            why = str(data.get("why", ""))[:500]
                            for item in data.get("edges") or []:
                                if not isinstance(item, dict):
                                    continue
                                match = next(
                                    (e for e in edges if e.label == item.get("label")), None
                                )
                                if match is not None and match not in [c[0] for c in chosen]:
                                    chosen.append((match, str(item.get("feedback", ""))[:2000]))
        except Exception as exc:  # noqa: BLE001 - routing must not abort the run
            log.warning("workflow %s: routing failed: %r", self.workflow.name, exc)
            raw = f"{type(exc).__name__}: {exc}"

        if not chosen:
            # Never guess a branch. Fall back to the default, and say so loudly: a
            # silent fallback looks identical to a real decision in the log.
            log.warning("workflow %s: no usable routing decision (%s)", self.workflow.name, raw)
            await self.bus.emit(
                "routing_failed",
                {"node": node.key, "detail": raw,
                 "fallback": default.label if default else None},
            )
            if default is not None:
                chosen = [(default, f"no usable routing decision ({raw})")]
                why = f"routing produced nothing usable ({raw}); took the default"
        return chosen, why

    # ------------------------------------------------------------------- walk

    async def execute(self) -> WorkflowOutcome:
        steps: list[StepResult] = []
        artifacts: dict[str, StepResult] = {}
        visits: dict[str, int] = {}
        path: list[list[str]] = []

        frontier: list[tuple[WorkflowNodeSpec, list[str]]] = [(self.workflow.start, [])]
        escalated = False
        reason = "end"
        superstep = 0

        await self.bus.emit("workflow_started", self.workflow.public())

        while frontier:
            if superstep >= self.workflow.max_steps:
                reason, frontier, escalated = await self._exhausted(
                    "max_steps", {"limit": self.workflow.max_steps}, escalated
                )
                if frontier:
                    continue
                break

            # Budgets are checked per node, so one exhausted arm of a fan-out does not
            # stop the others.
            runnable: list[tuple[WorkflowNodeSpec, list[str], int]] = []
            over: list[str] = []
            for node, feedback in frontier:
                count = visits.get(node.key, 0) + 1
                if count > node.max_visits:
                    over.append(node.key)
                    continue
                visits[node.key] = count
                runnable.append((node, feedback, count))

            if over and not runnable:
                reason, frontier, escalated = await self._exhausted(
                    "max_visits", {"nodes": over}, escalated
                )
                if frontier:
                    continue
                break
            if over:
                await self.bus.emit(
                    "workflow_stopped",
                    {"reason": "max_visits", "nodes": over, "partial": True,
                     "note": "these steps are out of visits; the rest of the graph continues"},
                )

            superstep += 1
            keys = [node.key for node, _, _ in runnable]
            path.append(keys)
            if len(keys) > 1:
                await self.bus.emit(
                    "superstep", {"index": superstep, "parallel": keys}
                )

            results = await asyncio.gather(
                *(
                    self._run_node(node, visit, dict(artifacts), feedback)
                    for node, feedback, visit in runnable
                )
            )
            for step in results:
                steps.append(step)
                artifacts[step.artifact] = step

            if self.cancelled:
                reason = "cancelled"
                break

            # Union the successors. A node reached from several branches runs once, with
            # their feedback merged — that is the join.
            pending: dict[str, list[str]] = {}
            finished_here = False
            for step in results:
                node = self.workflow.node(step.node_key)
                edges = self.workflow.outgoing(node.key)
                selected, why = await self._choose_edges(node, edges, step)
                if not selected:
                    finished_here = True
                    await self.bus.emit(
                        "edge_taken",
                        {"from": node.key, "to": "END", "label": None,
                         "why": why or "no outgoing edge"},
                    )
                    continue
                for edge, feedback in selected:
                    for key in edge.resets:
                        if visits.pop(key, None) is not None:
                            await self.bus.emit(
                                "visits_reset", {"node": key, "by": edge.label}
                            )
                    await self.bus.emit(
                        "edge_taken",
                        {"from": node.key, "to": edge.to_key or "END", "label": edge.label,
                         "why": why, "feedback": feedback[:300]},
                    )
                    if edge.to_key is None:
                        finished_here = True
                        continue
                    bucket = pending.setdefault(edge.to_key, [])
                    if feedback.strip():
                        bucket.append(feedback.strip())

            frontier = [
                (self.workflow.node(key), feedback) for key, feedback in pending.items()
            ]
            if not frontier:
                reason = "end" if finished_here else "dead_end"

        outcome = WorkflowOutcome(
            steps=steps,
            reason="escalated" if escalated and reason != "cancelled" else reason,
            total_cost_usd=sum(
                (s.result.total_cost_usd or 0.0) for s in steps if s.result is not None
            ),
            num_turns=sum((s.result.num_turns or 0) for s in steps if s.result is not None),
            path=path,
        )
        await self.bus.emit(
            "workflow_finished",
            {"reason": outcome.reason, "steps": len(steps), "supersteps": len(path),
             "path": [" + ".join(group) for group in path],
             "artifacts": sorted(artifacts),
             "total_cost_usd": outcome.total_cost_usd},
        )
        return outcome

    async def _exhausted(
        self, reason: str, detail: dict[str, Any], already: bool
    ) -> tuple[str, list[tuple[WorkflowNodeSpec, list[str]]], bool]:
        """A budget ran out: divert to the escalation node, or stop.

        Escalation happens at most once, so a graph whose escalation node itself loops
        cannot keep the run alive indefinitely. The event is awaited rather than fired
        and forgotten — the walk is about to end, and a lost event is a run whose history
        does not say why it stopped.
        """
        escalation = self.workflow.escalation_key
        if escalation and not already:
            note = f"{reason} reached; handing over to {escalation!r}"
            await self.bus.emit(
                "workflow_stopped", {"reason": reason, **detail, "note": note}
            )
            log.info("workflow %s: %s", self.workflow.name, note)
            return reason, [(self.workflow.node(escalation), [note])], True
        await self.bus.emit(
            "workflow_stopped",
            {"reason": reason, **detail, "note": "budget exhausted"},
        )
        return reason, [], already

    async def cancel(self) -> None:
        self.cancelled = True
        for client in list(self._clients.values()):
            try:
                await client.interrupt()
            except Exception:  # noqa: BLE001
                pass
