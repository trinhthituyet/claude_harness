"""The workflow executor: supersteps, fan-out, joins, budgets and escalation.

Node execution and edge routing are stubbed, so these run with no model and no network.
What is under test is the walk itself — including the shape of the LangGraph example in
docs/example_graph.py, which is the reason the executor works in supersteps at all.
"""

from __future__ import annotations

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
