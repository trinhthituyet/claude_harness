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
* An edge may carry an **expression** — ``issues contains architecture`` — evaluated by
  the harness against the source step's structured result. That is deterministic, instant
  and free; the model is only asked about edges described in words, and only when no
  expression already decided the matter.
* A node may **select its inputs** (``review_notes.architect_issues``) instead of being
  shown every artifact, which keeps a re-running role focused on what concerns it.
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
from app.services import expr as expr_lang
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
    #: The structured result, when the node declared an output schema.
    data: dict[str, Any] | None = None

    @property
    def rendered(self) -> str:
        """What later steps and the router are shown for this step."""
        if self.data is not None:
            return json.dumps(self.data, indent=2, default=str)
        return self.output


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


def select_inputs(
    node: WorkflowNodeSpec, artifacts: dict[str, StepResult]
) -> list[tuple[str, str]]:
    """The ``(label, text)`` pairs this step should be shown.

    With no selection a step sees every artifact, which is the right default for a small
    graph. Once a node declares inputs it sees only those — and a dotted path pulls one
    field out of a structured result, so a role reworking gets just its own notes.
    """
    if not node.inputs:
        return [
            (f"{name}{' (JSON)' if step.data is not None else ''}", step.rendered)
            for name, step in artifacts.items()
        ]

    chosen: list[tuple[str, str]] = []
    for source, path, label in node.inputs:
        step = artifacts.get(source)
        if step is None:
            # Not produced yet — normal on a first pass through a loop.
            continue
        if not path:
            shape = " (JSON)" if step.data is not None else ""
            chosen.append((f"{label}{shape}", step.rendered))
            continue
        if step.data is None:
            chosen.append((label, step.rendered))
            continue
        value = expr_lang.resolve(_split_path(path), step.data)
        if isinstance(value, expr_lang.Missing):
            continue
        chosen.append((
            label,
            value if isinstance(value, str) else json.dumps(value, indent=2, default=str),
        ))
    return chosen


def _split_path(path: str) -> list:
    parts: list = []
    for segment in path.replace("[", ".").replace("]", "").split("."):
        if not segment:
            continue
        parts.append(int(segment) if segment.lstrip("-").isdigit() else segment)
    return parts


def _artifact_block(node: WorkflowNodeSpec, artifacts: dict[str, StepResult]) -> str:
    """The inputs this step is given, named."""
    selected = select_inputs(node, artifacts)
    if not selected:
        return ""
    heading = (
        "## Your inputs" if node.inputs else "## What the team has produced so far"
    )
    lines = ["", heading, ""]
    for label, text in selected:
        lines.append(f"### {label}")
        lines.append(text[-DIGEST_CHARS:] or "(empty)")
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
    block = _artifact_block(node, artifacts)
    if block:
        parts.append(block)
    if node.output_schema is not None:
        parts += [
            "",
            "## Your result must match this shape",
            "",
            "Do the work first, then report it as a structured result matching this "
            "JSON Schema. The fields are what the rest of the team will read:",
            "",
            "```json",
            json.dumps(node.output_schema, indent=2),
            "```",
        ]
    parts += [
        "",
        f"Do your part of this goal now. Your output is filed as `{node.artifact}` for "
        "the rest of the team."
        + (
            " Finish with the structured result."
            if node.output_schema is not None
            else " Finish by stating what you produced."
        ),
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
        #: Structured artifacts by name, for expressions to read across steps.
        self._artifact_data: dict[str, Any] = {}
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
            if node.output_schema is not None:
                # The gate's hook stays on: structured output arrives through a
                # StructuredOutput tool call, which the guard recognises as harmless.
                options = dataclasses.replace(
                    options,
                    output_format={"type": "json_schema", "schema": node.output_schema},
                )
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
            data: dict[str, Any] | None = None
            if node.output_schema is not None:
                data = self._structured(result, text)
                if data is None:
                    # The node was held to a shape and did not produce it. Keep the prose
                    # so the run is still useful, but say so — a silently unstructured
                    # artifact would break whatever reads its fields.
                    await self.bus.emit(
                        "schema_unsatisfied",
                        {"node": node.key, "artifact": node.artifact,
                         "note": "no result matching the declared schema; kept the prose "
                                 "output instead"},
                    )
                    log.warning(
                        "workflow %s: node %s did not satisfy its output schema",
                        self.workflow.name, node.key,
                    )

            await self.bus.emit(
                "node_finished",
                {"node": node.key, "role": node.role.name, "visit": visit,
                 "artifact": node.artifact, "chars": len(text),
                 "structured": data is not None,
                 "fields": sorted(data) if data else None,
                 "cost_usd": result.total_cost_usd if result else None},
            )
            return StepResult(node.key, node.role.name, visit, text, result,
                              node.artifact, data)

    @staticmethod
    def _structured(result: ResultMessage | None, text: str) -> dict[str, Any] | None:
        """The structured result, falling back to JSON embedded in the prose."""
        if result is not None and isinstance(result.structured_output, dict):
            return result.structured_output
        for candidate in ((result.result if result else None), text):
            parsed = _parse_json(candidate)
            if parsed is not None:
                return parsed
        return None

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

        # Expressions are deterministic, so they decide before anything is asked of a
        # model. If any matched, that is the answer — no call, no judgement, no cost.
        matched, tested = await self._evaluate_expressions(node, edges, step)
        if matched:
            return matched, "matched " + ", ".join(
                f"{edge.label} ({edge.expression})" for edge, _ in matched
            )

        worded = [edge for edge in edges if edge.condition.strip()]
        if not worded:
            # Every edge was an expression and none matched: fall to the default rather
            # than asking a model about conditions nobody wrote in words.
            if default is not None:
                return [(default, "")], (
                    f"no expression matched ({tested}); took the default {default.label!r}"
                )
            await self.bus.emit(
                "routing_failed",
                {"node": node.key, "detail": f"no expression matched ({tested})",
                 "fallback": None},
            )
            return [], f"no expression matched ({tested}) and there is no default"

        edges = worded + ([default] if default is not None and default not in worded else [])
        labels = [edge.label for edge in edges]

        options, _ = self._options_for(node)
        # A routing decision must not touch anything: no tools, and an answer constrained
        # to this node's own edge labels. The gate's hook stays registered — structured
        # output arrives through a ``StructuredOutput`` tool call, which the guard now
        # recognises as having no filesystem effect, so there is no reason to drop it.
        router_options = dataclasses.replace(
            options,
            tools=[],
            mcp_servers={},
            agents=None,
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
            f"{step.rendered[-DIGEST_CHARS:] or '(no output)'}\n\n"
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

    async def _evaluate_expressions(
        self, node: WorkflowNodeSpec, edges: list[WorkflowEdgeSpec], step: StepResult
    ) -> tuple[list[tuple[WorkflowEdgeSpec, str]], str]:
        """Test every edge that carries an expression against this step's result.

        The context is the step's own structured fields, plus each artifact by name — so
        ``issues contains architecture`` reads this step, and
        ``review_notes.approved`` reads another's.
        """
        with_expressions = [e for e in edges if e.expression.strip()]
        if not with_expressions:
            return [], "no expressions"

        context: dict[str, Any] = dict(self._artifact_data)
        if step.data:
            # This step's own fields take precedence, so the common case is unqualified.
            context.update(step.data)
        context.setdefault("output", step.output)

        matched: list[tuple[WorkflowEdgeSpec, str]] = []
        tested: list[str] = []
        for edge in with_expressions:
            try:
                result = expr_lang.evaluate(edge.expression, context)
            except expr_lang.ExprError as exc:
                # A broken expression must not silently never fire.
                await self.bus.emit(
                    "condition_error",
                    {"node": node.key, "label": edge.label,
                     "expression": edge.expression, "error": str(exc)},
                )
                log.warning(
                    "workflow %s: edge %s expression failed: %s",
                    self.workflow.name, edge.label, exc,
                )
                tested.append(f"{edge.label}=error")
                continue
            tested.append(f"{edge.label}={'true' if result else 'false'}")
            if result:
                matched.append((edge, ""))

        await self.bus.emit(
            "conditions_evaluated",
            {"node": node.key, "results": tested,
             "matched": [edge.label for edge, _ in matched]},
        )
        return matched, ", ".join(tested)

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
                if step.data is not None:
                    self._artifact_data[step.artifact] = step.data

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
