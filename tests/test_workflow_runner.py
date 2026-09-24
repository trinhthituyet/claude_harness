"""The workflow executor: supersteps, fan-out, joins, budgets and escalation.

Node execution and edge routing are stubbed, so these run with no model and no network.
What is under test is the walk itself — including the shape of the LangGraph example in
docs/example_graph.py, which is the reason the executor works in supersteps at all.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from app.security.gate import Gate
from app.security.policy import RunPolicy
from app.services import workflow_runner
from app.services.events import EventBus
from app.services.snapshot import (
    ModelSpec,
    RoleSpec,
    RunSnapshot,
    WorkflowEdgeSpec,
    WorkflowNodeSpec,
    WorkflowSpec,
)
from app.services.workflow_runner import StepResult, WorkflowRunner


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return Path(os.path.realpath(project))


def node(key: str, max_visits: int = 3, is_start: bool = False,
         output_key: str = "") -> WorkflowNodeSpec:
    return WorkflowNodeSpec(
        key=key,
        role=RoleSpec(name=f"{key}-role", description="", system_prompt="do work", is_lead=True),
        instructions="",
        is_start=is_start,
        max_visits=max_visits,
        output_key=output_key,
    )


def edge(from_key, to_key, label="go", condition="", is_default=False, resets=()) -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(from_key, to_key, label, condition, is_default, tuple(resets))


def make_runner(root: Path, workflow: WorkflowSpec) -> tuple[WorkflowRunner, list]:
    emitted: list[tuple[str, dict]] = []

    async def persist(event):
        return None

    async def replay(after):
        return []

    bus = EventBus("test", persist=persist, replay=replay)
    original = bus.emit

    async def recording(type_, payload):
        emitted.append((type_, payload))
        return await original(type_, payload)

    bus.emit = recording  # type: ignore[method-assign]

    snapshot = RunSnapshot(
        task_id=1, task_name="t", prompt="ship the thing", project_path=str(root), root=root,
        roles=[n.role for n in workflow.nodes], model=ModelSpec(model_id="claude-sonnet-5"),
        skills=[], mcp_servers={}, trusted_servers=frozenset(),
        trust_project_settings=False, sandbox_bash=True, paranoid_mode=False,
        network_enabled=False, approval_timeout_s=300, max_turns=None, max_budget_usd=None,
        workflow=workflow,
    )
    policy = RunPolicy(root=root)

    async def noop(*args, **kwargs):
        return None

    gate = Gate(policy, emit=noop, audit=noop)
    return WorkflowRunner(snapshot, policy, gate, bus), emitted


def stub_steps(runner: WorkflowRunner, outputs: dict[str, str] | None = None) -> list[str]:
    """Replace node execution with a recorder. Returns the visit order."""
    visited: list[str] = []
    seen_prompts: dict[str, str] = {}

    async def fake_run_node(node_spec, visit, artifacts, feedback):
        visited.append(f"{node_spec.key}#{visit}")
        seen_prompts[node_spec.key] = workflow_runner._step_prompt(
            runner.snapshot, node_spec, artifacts, feedback
        )
        await runner.bus.emit(
            "node_started", {"node": node_spec.key, "visit": visit, "feedback": feedback}
        )
        text = (outputs or {}).get(node_spec.key, f"output of {node_spec.key}")
        return StepResult(node_spec.key, node_spec.role.name, visit, text, None,
                          node_spec.artifact)

    runner._run_node = fake_run_node  # type: ignore[method-assign]
    runner.seen_prompts = seen_prompts  # type: ignore[attr-defined]
    return visited


def stub_routing(runner: WorkflowRunner, picks: list[list[str] | None]) -> None:
    """Feed the router a fixed sequence of label lists (None/[] = finish)."""
    remaining = list(picks)

    async def fake_choose(node_spec, edges, step):
        if not edges:
            return [], ""
        if runner.workflow.fans_out(node_spec.key):
            return [(e, "") for e in edges], "fan-out"
        if len(edges) == 1 and not edges[0].conditional:
            return [(edges[0], "")], ""
        labels = remaining.pop(0) if remaining else None
        if not labels:
            return [], "stubbed finish"
        chosen = [(e, f"feedback for {e.label}") for e in edges if e.label in labels]
        return chosen, f"stubbed {labels}"

    runner._choose_edges = fake_choose  # type: ignore[method-assign]


# ------------------------------------------------------------ basic sequences


async def test_a_linear_workflow_visits_each_node_once(root):
    workflow = WorkflowSpec(
        id=1, name="linear", max_steps=10,
        nodes=(node("design", is_start=True), node("build")),
        edges=(edge("design", "build", "to_build"), edge("build", None, "done")),
    )
    runner, events = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [])

    outcome = await runner.execute()
    assert visited == ["design#1", "build#1"]
    assert outcome.reason == "end"
    assert outcome.path == [["design"], ["build"]]


async def test_a_node_with_no_outgoing_edges_ends_the_workflow(root):
    workflow = WorkflowSpec(
        id=1, name="terminal", max_steps=10, nodes=(node("only", is_start=True),), edges=(),
    )
    runner, _ = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [])
    outcome = await runner.execute()
    assert visited == ["only#1"] and outcome.reason == "end"


# -------------------------------------------------------- fan-out and joining


async def test_unconditional_edges_fan_out_in_one_superstep(root):
    """manager_dispatch -> [architect, designer, tester], as the example graph does."""
    workflow = WorkflowSpec(
        id=1, name="fanout", max_steps=10,
        nodes=(node("dispatch", is_start=True), node("architect"), node("designer"),
               node("tester"), node("review")),
        edges=(
            edge("dispatch", "architect", "to_architect"),
            edge("dispatch", "designer", "to_designer"),
            edge("dispatch", "tester", "to_tester"),
            edge("architect", "review", "report"),
            edge("designer", "review", "report"),
            edge("tester", "review", "report"),
            edge("review", None, "done"),
        ),
    )
    runner, events = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [])

    outcome = await runner.execute()
    assert outcome.path[0] == ["dispatch"]
    assert sorted(outcome.path[1]) == ["architect", "designer", "tester"]
    # The three converging edges join: review runs once, not three times.
    assert outcome.path[2] == ["review"]
    assert len([v for v in visited if v.startswith("review")]) == 1
    assert outcome.reason == "end"

    parallel = [p for t, p in events if t == "superstep"]
    assert parallel and sorted(parallel[0]["parallel"]) == ["architect", "designer", "tester"]


async def test_a_join_merges_feedback_from_every_branch(root):
    """Two branches sending the same node back to work hand it both notes."""
    workflow = WorkflowSpec(
        id=1, name="merge", max_steps=10,
        nodes=(node("a", is_start=True), node("b"), node("fix", max_visits=2)),
        edges=(
            edge("a", "b", "to_b"),
            edge("b", "fix", "needs_fix", condition="something is wrong"),
            edge("b", None, "ok", condition="fine", is_default=True),
            edge("fix", None, "done"),
        ),
    )
    runner, _ = make_runner(root, workflow)
    stub_steps(runner)
    stub_routing(runner, [["needs_fix"]])
    await runner.execute()
    # The feedback the router attached reached the node's prompt.
    assert "feedback for needs_fix" in runner.seen_prompts["fix"]
    assert "Feedback addressed to you" in runner.seen_prompts["fix"]


async def test_a_branch_may_select_several_targets(root):
    """route_after_review returns only the flagged roles — one, two or all three."""
    workflow = WorkflowSpec(
        id=1, name="subset", max_steps=10,
        nodes=(node("review", is_start=True, max_visits=2), node("architect"),
               node("designer"), node("tester"), node("engineer")),
        edges=(
            edge("review", "architect", "arch", condition="the architecture has issues"),
            edge("review", "designer", "design", condition="the design has issues"),
            edge("review", "tester", "tests", condition="the tests have issues"),
            edge("review", "engineer", "approved", condition="all three are fine",
                 is_default=True),
            edge("architect", None, "done"),
            edge("designer", None, "done"),
            edge("tester", None, "done"),
            edge("engineer", None, "done"),
        ),
    )
    runner, _ = make_runner(root, workflow)
    stub_steps(runner)
    # Only the architect and tester were flagged.
    stub_routing(runner, [["arch", "tests"]])
    outcome = await runner.execute()
    assert sorted(outcome.path[1]) == ["architect", "tester"]
    assert "designer" not in [k for group in outcome.path for k in group]


# -------------------------------------------------------------- named outputs


async def test_a_step_sees_earlier_artifacts_by_name(root):
    workflow = WorkflowSpec(
        id=1, name="artifacts", max_steps=10,
        nodes=(node("architect", is_start=True, output_key="arch_doc"),
               node("engineer", output_key="code")),
        edges=(edge("architect", "engineer", "to_engineer"), edge("engineer", None, "done")),
    )
    runner, events = make_runner(root, workflow)
    stub_steps(runner, {"architect": "use a state machine"})
    stub_routing(runner, [])
    outcome = await runner.execute()

    prompt = runner.seen_prompts["engineer"]
    assert "arch_doc" in prompt                  # named, not positional
    assert "use a state machine" in prompt
    assert "filed as `code`" in prompt           # told where its own output goes
    finished = [p for t, p in events if t == "workflow_finished"][0]
    assert sorted(finished["artifacts"]) == ["arch_doc", "code"]
    assert outcome.reason == "end"


# ------------------------------------------------------------------- budgets


async def test_a_node_visit_budget_stops_an_endless_loop(root):
    workflow = WorkflowSpec(
        id=1, name="stuck", max_steps=50,
        nodes=(node("build", max_visits=2, is_start=True), node("test", max_visits=9)),
        edges=(
            edge("build", "test", "to_test"),
            edge("test", "build", "rework", condition="tests fail"),
            edge("test", None, "approved", condition="tests pass"),
        ),
    )
    runner, events = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [["rework"]] * 20)

    outcome = await runner.execute()
    assert outcome.reason == "max_visits"
    assert visited == ["build#1", "test#1", "build#2", "test#2"]
    stopped = [p for t, p in events if t == "workflow_stopped"]
    assert stopped and stopped[-1]["nodes"] == ["build"]


async def test_the_superstep_budget_is_the_outer_bound(root):
    workflow = WorkflowSpec(
        id=1, name="budget", max_steps=3,
        nodes=(node("a", max_visits=99, is_start=True),), edges=(edge("a", "a", "again"),),
    )
    runner, _ = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [])
    outcome = await runner.execute()
    assert outcome.reason == "max_steps"
    assert len(visited) == 3


async def test_an_edge_can_reset_a_visit_budget(root):
    """The example graph resets the review loop when work comes back from the PM."""
    workflow = WorkflowSpec(
        id=1, name="reset", max_steps=20,
        nodes=(node("review", is_start=True, max_visits=2), node("engineer", max_visits=3),
               node("pm", max_visits=3)),
        edges=(
            edge("review", "engineer", "approved", condition="looks good", is_default=True),
            edge("engineer", "pm", "to_pm"),
            # Sending work upstream resets the review budget, as route_after_pm does.
            edge("pm", "review", "arch_issue", condition="the architecture is wrong",
                 resets=("review",)),
            edge("pm", None, "accepted", condition="all good"),
        ),
    )
    runner, events = make_runner(root, workflow)
    visited = stub_steps(runner)
    # review -> engineer -> pm -> (reset) review -> engineer -> pm -> accepted
    stub_routing(runner, [["approved"], ["arch_issue"], ["approved"], ["accepted"]])

    outcome = await runner.execute()
    resets = [p for t, p in events if t == "visits_reset"]
    assert resets and resets[0]["node"] == "review"
    # Without the reset, review's budget of 2 would have been spent; it runs twice more.
    assert visited.count("review#1") == 2, visited
    assert outcome.reason == "end"


# ---------------------------------------------------------------- escalation


async def test_exhausting_a_budget_diverts_to_the_escalation_node(root):
    workflow = WorkflowSpec(
        id=1, name="escalate", max_steps=50,
        nodes=(node("build", max_visits=1, is_start=True), node("test", max_visits=9),
               node("human")),
        edges=(
            edge("build", "test", "to_test"),
            edge("test", "build", "rework", condition="tests fail"),
            edge("test", None, "approved", condition="tests pass"),
            edge("human", None, "handed_over"),
        ),
        escalation_key="human",
    )
    runner, events = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [["rework"]] * 10)

    outcome = await runner.execute()
    assert "human#1" in visited, visited
    assert outcome.reason == "escalated"
    stopped = [p for t, p in events if t == "workflow_stopped"]
    assert stopped and "handing over" in stopped[0]["note"]


async def test_escalation_happens_only_once(root):
    """A looping escalation node must not keep the run alive forever."""
    workflow = WorkflowSpec(
        id=1, name="escalate-loop", max_steps=6,
        nodes=(node("a", max_visits=1, is_start=True), node("human", max_visits=1)),
        edges=(edge("a", "a", "again"), edge("human", "a", "back")),
        escalation_key="human",
    )
    runner, _ = make_runner(root, workflow)
    visited = stub_steps(runner)
    stub_routing(runner, [])
    outcome = await runner.execute()
    assert visited.count("human#1") == 1, visited
    assert len(visited) <= 6


# ---------------------------------------------------- the example graph shape


def example_graph() -> WorkflowSpec:
    """The shape of docs/example_graph.py, as this harness expresses it."""
    return WorkflowSpec(
        id=1, name="software team", max_steps=30,
        nodes=(
            node("dispatch", is_start=True, max_visits=2),
            node("architect", max_visits=4, output_key="arch_doc"),
            node("designer", max_visits=4, output_key="design_doc"),
            node("tester", max_visits=4, output_key="test_cases"),
            node("review", max_visits=4, output_key="review_notes"),
            node("engineer", max_visits=4, output_key="code"),
            node("pm", max_visits=4, output_key="pm_report"),
            node("human", max_visits=1),
        ),
        edges=(
            edge("dispatch", "architect", "to_architect"),
            edge("dispatch", "designer", "to_designer"),
            edge("dispatch", "tester", "to_tester"),
            edge("architect", "review", "report"),
            edge("designer", "review", "report"),
            edge("tester", "review", "report"),
            edge("review", "architect", "arch_issues",
                 condition="the architecture needs changes"),
            edge("review", "designer", "design_issues", condition="the design needs changes"),
            edge("review", "tester", "test_issues", condition="the tests need changes"),
            edge("review", "engineer", "approved", condition="all three are acceptable",
                 is_default=True),
            edge("engineer", "pm", "deliver"),
            edge("pm", "engineer", "code_bug", condition="the code itself is wrong"),
            edge("pm", "architect", "arch_issue", condition="the architecture is wrong",
                 resets=("review", "architect")),
            edge("pm", "designer", "design_issue", condition="the design is wrong",
                 resets=("review", "designer")),
            edge("pm", "tester", "test_issue", condition="a test case is wrong",
                 resets=("review", "tester")),
            edge("pm", None, "accepted", condition="tests pass and it matches the docs",
                 is_default=True),
            edge("human", None, "handed_over"),
        ),
        escalation_key="human",
    )


def test_the_example_graph_validates():
    from app.services import graph

    spec = example_graph()
    graph.validate(
        [graph.NodeSpec(n.key, n.is_start, n.max_visits, n.artifact) for n in spec.nodes],
        [
            graph.EdgeSpec(e.from_key, e.to_key, e.label, e.condition, e.is_default, e.resets)
            for e in spec.edges
        ],
        spec.escalation_key,
    )


async def test_the_example_graph_happy_path(root):
    """dispatch fans out, review approves, engineer delivers, PM accepts."""
    runner, _ = make_runner(root, example_graph())
    stub_steps(runner)
    stub_routing(runner, [["approved"], ["accepted"]])

    outcome = await runner.execute()
    assert outcome.path[0] == ["dispatch"]
    assert sorted(outcome.path[1]) == ["architect", "designer", "tester"]
    assert outcome.path[2] == ["review"]
    assert outcome.path[3] == ["engineer"]
    assert outcome.path[4] == ["pm"]
    assert outcome.reason == "end"


async def test_the_example_graph_review_loop_reruns_only_flagged_roles(root):
    runner, _ = make_runner(root, example_graph())
    visited = stub_steps(runner)
    # Review flags the architect and tester, then approves; the PM accepts.
    stub_routing(runner, [["arch_issues", "test_issues"], ["approved"], ["accepted"]])

    outcome = await runner.execute()
    assert sorted(outcome.path[2]) == ["review"]
    assert sorted(outcome.path[3]) == ["architect", "tester"]   # designer not re-run
    assert outcome.path[4] == ["review"]                        # they rejoin the review
    assert visited.count("designer#1") == 1
    assert outcome.reason == "end"


async def test_the_example_graph_pm_sends_work_upstream_with_a_fresh_budget(root):
    runner, events = make_runner(root, example_graph())
    visited = stub_steps(runner)
    stub_routing(runner, [
        ["approved"],      # review -> engineer
        ["arch_issue"],    # pm -> architect, resetting the review loop
        ["approved"],      # review -> engineer again
        ["accepted"],      # pm accepts
    ])

    outcome = await runner.execute()
    # dispatch, fan-out, review, engineer, pm, then back to the architect.
    flat = [group for group in outcome.path]
    pm_index = next(i for i, group in enumerate(flat) if group == ["pm"])
    assert flat[pm_index + 1] == ["architect"], flat
    resets = [p["node"] for t, p in events if t == "visits_reset"]
    assert "review" in resets
    assert outcome.reason == "end"
    assert visited.count("engineer#1") >= 1


async def test_the_example_graph_escalates_when_the_review_loop_will_not_settle(root):
    runner, _ = make_runner(root, example_graph())
    visited = stub_steps(runner)
    # Review keeps flagging the architect and never approves.
    stub_routing(runner, [["arch_issues"]] * 20)

    outcome = await runner.execute()
    assert "human#1" in visited, visited
    assert outcome.reason == "escalated"


# ---------------------------------------------------------- step isolation


def test_each_step_is_a_single_role_session_with_the_task_security(root):
    workflow = WorkflowSpec(
        id=1, name="iso", max_steps=5, nodes=(node("build", is_start=True),), edges=(),
    )
    runner, _ = make_runner(root, workflow)
    options, blob = runner._options_for(workflow.nodes[0])
    assert options.allowed_tools == []
    assert options.permission_mode == "default"
    assert options.can_use_tool is not None
    assert options.hooks["PreToolUse"]
    assert options.cwd == str(root)
    assert options.agents is None
    assert "deny" in blob["permissions"]


async def test_cancelling_stops_after_the_current_superstep(root):
    workflow = WorkflowSpec(
        id=1, name="cancel", max_steps=10,
        nodes=(node("a", is_start=True), node("b")),
        edges=(edge("a", "b", "next"), edge("b", None, "done")),
    )
    runner, _ = make_runner(root, workflow)
    visited: list[str] = []

    async def fake_run_node(node_spec, visit, artifacts, feedback):
        visited.append(node_spec.key)
        await runner.cancel()
        return StepResult(node_spec.key, node_spec.role.name, visit, "partial", None,
                          node_spec.artifact)

    runner._run_node = fake_run_node  # type: ignore[method-assign]
    stub_routing(runner, [])
    outcome = await runner.execute()
    assert visited == ["a"]
    assert outcome.reason == "cancelled"


# --------------------------------------------------------- per-node JSON output


def schema_node(key: str, schema: dict, **kwargs) -> WorkflowNodeSpec:
    base = node(key, **kwargs)
    return dataclasses.replace(base, output_schema=schema)


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "issues"],
}


async def test_a_schema_node_asks_for_structured_output(root):
    workflow = WorkflowSpec(
        id=1, name="schema", max_steps=5,
        nodes=(schema_node("review", REVIEW_SCHEMA, is_start=True),), edges=(),
    )
    runner, _ = make_runner(root, workflow)
    captured = {}

    async def fake_run(node_spec, visit, artifacts, feedback):
        options, _ = runner._options_for(node_spec)
        if node_spec.output_schema is not None:
            options = dataclasses.replace(
                options,
                output_format={"type": "json_schema", "schema": node_spec.output_schema},
            )
        captured["output_format"] = options.output_format
        # The gate's hook must stay on: structured output is delivered by a tool call
        # the guard recognises, so there is no reason to drop it.
        captured["hooks"] = options.hooks
        return StepResult(node_spec.key, "r", visit, "prose", None, node_spec.artifact,
                          {"approved": True, "issues": []})

    runner._run_node = fake_run  # type: ignore[method-assign]
    stub_routing(runner, [])
    await runner.execute()

    assert captured["output_format"]["schema"] == REVIEW_SCHEMA
    assert captured["hooks"]["PreToolUse"]


async def test_structured_output_is_what_later_steps_see(root):
    workflow = WorkflowSpec(
        id=1, name="schema", max_steps=5,
        nodes=(schema_node("review", REVIEW_SCHEMA, is_start=True, output_key="verdict"),
               node("engineer")),
        edges=(edge("review", "engineer", "next"), edge("engineer", None, "done")),
    )
    runner, events = make_runner(root, workflow)
    data = {"approved": False, "issues": ["add() subtracts"]}

    async def fake_run(node_spec, visit, artifacts, feedback):
        if node_spec.key == "engineer":
            runner.seen = workflow_runner._step_prompt(
                runner.snapshot, node_spec, artifacts, feedback
            )
            return StepResult(node_spec.key, "e", visit, "built it", None,
                              node_spec.artifact)
        return StepResult(node_spec.key, "r", visit, "prose form", None,
                          node_spec.artifact, data)

    runner._run_node = fake_run  # type: ignore[method-assign]
    stub_routing(runner, [])
    outcome_steps = (await runner.execute()).steps

    # The engineer is shown the JSON, labelled as such — not the prose.
    assert "verdict (JSON)" in runner.seen
    assert '"approved": false' in runner.seen
    assert "add() subtracts" in runner.seen
    # The prose is still on the step, it is just not what downstream reads.
    assert outcome_steps[0].output == "prose form"
    assert outcome_steps[0].rendered.startswith("{")


def test_structured_falls_back_to_json_in_the_prose(root):
    """The structured channel can come back empty; the JSON is usually in the text too."""
    workflow = WorkflowSpec(id=1, name="s", max_steps=2,
                            nodes=(node("a", is_start=True),), edges=())
    runner, _ = make_runner(root, workflow)
    assert runner._structured(None, 'here it is: {"approved": true} done') == {"approved": True}
    assert runner._structured(None, "no json here") is None


async def test_a_node_that_ignores_its_schema_is_reported_not_hidden(root):
    workflow = WorkflowSpec(
        id=1, name="schema", max_steps=5,
        nodes=(schema_node("review", REVIEW_SCHEMA, is_start=True),), edges=(),
    )
    runner, events = make_runner(root, workflow)

    async def fake_run(node_spec, visit, artifacts, feedback):
        # Reproduce _run_node's schema handling with prose that carries no JSON.
        text = "I reviewed it and it seems fine."
        data = runner._structured(None, text)
        if data is None:
            await runner.bus.emit(
                "schema_unsatisfied",
                {"node": node_spec.key, "artifact": node_spec.artifact, "note": "no match"},
            )
        return StepResult(node_spec.key, "r", visit, text, None, node_spec.artifact, data)

    runner._run_node = fake_run  # type: ignore[method-assign]
    stub_routing(runner, [])
    outcome = await runner.execute()

    assert [t for t, _ in events if t == "schema_unsatisfied"]
    # The prose is still kept, so the run stays useful.
    assert outcome.steps[0].output.startswith("I reviewed it")
    assert outcome.steps[0].data is None


def test_a_step_prompt_states_the_required_shape(root):
    workflow = WorkflowSpec(
        id=1, name="s", max_steps=2,
        nodes=(schema_node("review", REVIEW_SCHEMA, is_start=True),), edges=(),
    )
    runner, _ = make_runner(root, workflow)
    prompt = workflow_runner._step_prompt(runner.snapshot, workflow.nodes[0], {}, [])
    assert "must match this shape" in prompt
    assert '"approved"' in prompt
    assert "structured result" in prompt


def test_a_plain_node_is_not_asked_for_json(root):
    workflow = WorkflowSpec(id=1, name="s", max_steps=2,
                            nodes=(node("a", is_start=True),), edges=())
    runner, _ = make_runner(root, workflow)
    prompt = workflow_runner._step_prompt(runner.snapshot, workflow.nodes[0], {}, [])
    assert "must match this shape" not in prompt
    assert "stating what you produced" in prompt


# ------------------------------------------- expression conditions on edges


ISSUES_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}


def expr_graph() -> WorkflowSpec:
    """review branches on its own JSON, with no model call in the loop."""
    return WorkflowSpec(
        id=1, name="expr", max_steps=10,
        nodes=(
            dataclasses.replace(node("review", is_start=True, max_visits=3),
                                output_key="review_notes", output_schema=ISSUES_SCHEMA),
            node("architect", max_visits=3),
            node("engineer", max_visits=3),
        ),
        edges=(
            WorkflowEdgeSpec("review", "architect", "arch_issues", "", False, (),
                             "issues contains architecture"),
            WorkflowEdgeSpec("review", "engineer", "approved", "", True, (),
                             "approved is true"),
            edge("architect", "review", "back"),
            edge("engineer", None, "done"),
        ),
    )


def stub_data_steps(runner: WorkflowRunner, data: dict[str, dict | None]) -> list[str]:
    """Nodes return canned structured results, in sequence per node key."""
    visited: list[str] = []
    queues = {k: list(v) if isinstance(v, list) else [v] for k, v in data.items()}

    async def fake_run_node(node_spec, visit, artifacts, feedback):
        visited.append(f"{node_spec.key}#{visit}")
        runner.last_prompt = workflow_runner._step_prompt(
            runner.snapshot, node_spec, artifacts, feedback
        )
        queue = queues.get(node_spec.key) or [None]
        payload = queue.pop(0) if queue else None
        return StepResult(node_spec.key, node_spec.role.name, visit,
                          "prose", None, node_spec.artifact, payload)

    runner._run_node = fake_run_node  # type: ignore[method-assign]
    return visited


async def test_an_expression_routes_without_asking_a_model(root):
    runner, events = make_runner(root, expr_graph())
    visited = stub_data_steps(runner, {
        "review": [{"approved": False, "issues": ["architecture is too coupled"]},
                   {"approved": True, "issues": []}],
    })

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("the model was asked despite a matching expression")

    # Any model call would go through the SDK; the expressions must settle it first.
    runner._choose_edges_via_model = must_not_be_called  # type: ignore[attr-defined]

    outcome = await runner.execute()
    assert visited[:2] == ["review#1", "architect#1"], visited
    assert outcome.reason == "end"

    evaluated = [p for t, p in events if t == "conditions_evaluated"]
    assert evaluated[0]["matched"] == ["arch_issues"], evaluated[0]
    assert "approved=false" in " ".join(evaluated[0]["results"])


async def test_the_expression_reads_this_step_and_other_artifacts(root):
    """`issues contains architecture` reads the current step; a dotted name reads another."""
    workflow = WorkflowSpec(
        id=1, name="cross", max_steps=6,
        nodes=(
            dataclasses.replace(node("a", is_start=True), output_key="first",
                                output_schema=ISSUES_SCHEMA),
            dataclasses.replace(node("b"), output_key="second",
                                output_schema=ISSUES_SCHEMA),
            node("c"),
        ),
        edges=(
            edge("a", "b", "to_b"),
            WorkflowEdgeSpec("b", "c", "earlier_failed", "", False, (),
                             "first.approved is false"),
            WorkflowEdgeSpec("b", None, "fine", "", True, (), "first.approved is true"),
            edge("c", None, "done"),
        ),
    )
    runner, events = make_runner(root, workflow)
    visited = stub_data_steps(runner, {
        "a": [{"approved": False, "issues": []}],
        "b": [{"approved": True, "issues": []}],
    })
    outcome = await runner.execute()
    assert "c#1" in visited, visited
    matched = [p["matched"] for t, p in events if t == "conditions_evaluated"]
    assert matched and matched[-1] == ["earlier_failed"]
    assert outcome.reason == "end"


async def test_no_expression_matching_takes_the_default(root):
    runner, events = make_runner(root, expr_graph())
    stub_data_steps(runner, {"review": [{"approved": True, "issues": []}]})
    outcome = await runner.execute()
    taken = [p["label"] for t, p in events if t == "edge_taken"]
    assert "approved" in taken, taken
    assert outcome.reason == "end"


async def test_a_broken_expression_is_reported_not_silently_false(root):
    workflow = WorkflowSpec(
        id=1, name="broken", max_steps=4,
        nodes=(dataclasses.replace(node("a", is_start=True), output_schema=ISSUES_SCHEMA),
               node("b")),
        edges=(
            WorkflowEdgeSpec("a", "b", "bad", "", False, (), "issues contains"),
            WorkflowEdgeSpec("a", None, "ok", "", True, (), "approved is true"),
            edge("b", None, "done"),
        ),
    )
    runner, events = make_runner(root, workflow)
    stub_data_steps(runner, {"a": [{"approved": True, "issues": []}]})
    await runner.execute()
    errors = [p for t, p in events if t == "condition_error"]
    assert errors and errors[0]["label"] == "bad"
    assert "right-hand side" in errors[0]["error"]


async def test_an_expression_edge_needs_no_worded_condition(root):
    """An expression counts as describing the edge, so validation is satisfied."""
    from app.services import graph

    spec = expr_graph()
    graph.validate(
        [graph.NodeSpec(n.key, n.is_start, n.max_visits, n.artifact, n.output_schema)
         for n in spec.nodes],
        [graph.EdgeSpec(e.from_key, e.to_key, e.label, e.condition, e.is_default,
                        e.resets, e.expression) for e in spec.edges],
    )


# ------------------------------------------------- selecting a node's inputs


async def test_a_node_with_no_inputs_sees_everything(root):
    workflow = WorkflowSpec(
        id=1, name="all", max_steps=4,
        nodes=(dataclasses.replace(node("a", is_start=True), output_key="first"),
               node("b")),
        edges=(edge("a", "b", "next"), edge("b", None, "done")),
    )
    runner, _ = make_runner(root, workflow)
    stub_data_steps(runner, {})
    await runner.execute()
    assert "What the team has produced so far" in runner.last_prompt
    assert "first" in runner.last_prompt


async def test_a_node_can_select_one_field_of_an_earlier_result(root):
    """The motivating case: a role reworking sees only the notes addressed to it."""
    workflow = WorkflowSpec(
        id=1, name="selected", max_steps=4,
        nodes=(
            dataclasses.replace(node("review", is_start=True), output_key="review_notes",
                                output_schema=ISSUES_SCHEMA),
            dataclasses.replace(
                node("architect"),
                inputs=(("review_notes", "issues", "my_notes"),),
            ),
        ),
        edges=(edge("review", "architect", "next"), edge("architect", None, "done")),
    )
    runner, _ = make_runner(root, workflow)
    stub_data_steps(runner, {
        "review": [{"approved": False, "issues": ["architecture is too coupled"]}],
    })
    await runner.execute()

    prompt = runner.last_prompt
    assert "## Your inputs" in prompt
    assert "### my_notes" in prompt
    assert "architecture is too coupled" in prompt
    # The rest of the review — its approved flag — was not handed over.
    assert '"approved"' not in prompt


def test_select_inputs_skips_what_has_not_been_produced_yet(root):
    workflow = WorkflowSpec(
        id=1, name="s", max_steps=2,
        nodes=(dataclasses.replace(node("a", is_start=True),
                                   inputs=(("later", "", "later"),)),),
        edges=(),
    )
    runner, _ = make_runner(root, workflow)
    assert workflow_runner.select_inputs(workflow.nodes[0], {}) == []


def test_select_inputs_handles_a_missing_field_gracefully(root):
    workflow = WorkflowSpec(
        id=1, name="s", max_steps=2,
        nodes=(dataclasses.replace(node("a", is_start=True),
                                   inputs=(("src", "nope.deeper", "x"),)),),
        edges=(),
    )
    runner, _ = make_runner(root, workflow)
    step = StepResult("s", "r", 1, "prose", None, "src", {"present": 1})
    assert workflow_runner.select_inputs(workflow.nodes[0], {"src": step}) == []


def test_select_inputs_falls_back_to_prose_when_there_is_no_json(root):
    workflow = WorkflowSpec(
        id=1, name="s", max_steps=2,
        nodes=(dataclasses.replace(node("a", is_start=True),
                                   inputs=(("src", "field", "x"),)),),
        edges=(),
    )
    runner, _ = make_runner(root, workflow)
    step = StepResult("s", "r", 1, "just words", None, "src", None)
    assert workflow_runner.select_inputs(workflow.nodes[0], {"src": step}) == [
        ("x", "just words")
    ]
