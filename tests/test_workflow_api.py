"""Workflow API: CRUD, validation feedback, and using one on a task."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "wf.db"))
    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    import app.main

    importlib.reload(app.main)

    application = app.main.create_app()
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as http:
            yield http


@pytest.fixture
def project(tmp_path: Path) -> str:
    directory = tmp_path / "proj"
    directory.mkdir()
    return str(Path(os.path.realpath(directory)))


async def make_role(client, name):
    response = await client.post(
        "/api/roles", json={"name": name, "description": name, "system_prompt": f"You are {name}."}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def loop_workflow(client, name="Build and test"):
    build = await make_role(client, "Engineer")
    test = await make_role(client, "Tester")
    return await client.post("/api/workflows", json={
        "name": name,
        "description": "Test failures loop back to the engineer.",
        "max_steps": 12,
        "nodes": [
            {"key": "build", "role_id": build, "is_start": True, "max_visits": 3},
            {"key": "test", "role_id": test, "max_visits": 3},
        ],
        "edges": [
            {"from_key": "build", "to_key": "test", "label": "to_test"},
            {"from_key": "test", "to_key": "build", "label": "rework",
             "condition": "any test fails"},
            {"from_key": "test", "to_key": None, "label": "approved",
             "condition": "everything passes", "is_default": True},
        ],
    })


# ------------------------------------------------------------------ the basics


async def test_starts_empty_with_examples_offered(client):
    assert (await client.get("/api/workflows")).json() == []
    examples = (await client.get("/api/workflows/examples")).json()
    assert len(examples) >= 1
    assert all("nodes" in e and "edges" in e for e in examples)


async def test_create_and_read_back_a_looping_workflow(client):
    created = await loop_workflow(client)
    assert created.status_code == 201, created.text
    body = created.json()

    assert [n["key"] for n in body["nodes"]] == ["build", "test"]
    assert [n["is_start"] for n in body["nodes"]] == [True, False]
    assert [n["role_name"] for n in body["nodes"]] == ["Engineer", "Tester"]

    # Edges come back keyed by node key, with END expressed as a null target.
    by_label = {e["label"]: e for e in body["edges"]}
    assert by_label["rework"]["from_key"] == "test"
    assert by_label["rework"]["to_key"] == "build"        # the loop
    assert by_label["approved"]["to_key"] is None         # finishes
    assert by_label["approved"]["is_default"] is True


async def test_the_first_node_becomes_the_start_when_none_is_marked(client):
    role = await make_role(client, "Solo")
    body = (await client.post("/api/workflows", json={
        "name": "implicit start",
        "nodes": [{"key": "one", "role_id": role}, {"key": "two", "role_id": role}],
        "edges": [{"from_key": "one", "to_key": "two", "label": "next"}],
    })).json()
    assert [n["is_start"] for n in body["nodes"]] == [True, False]


async def test_update_replaces_the_graph(client):
    created = (await loop_workflow(client)).json()
    role = created["nodes"][0]["role_id"]
    updated = await client.put(f"/api/workflows/{created['id']}", json={
        "name": "Simplified",
        "max_steps": 5,
        "nodes": [{"key": "only", "role_id": role, "is_start": True}],
        "edges": [],
    })
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert [n["key"] for n in body["nodes"]] == ["only"]
    assert body["edges"] == []
    assert body["max_steps"] == 5


async def test_duplicate_name_conflicts(client):
    await loop_workflow(client, "Dup")
    role = await make_role(client, "Another")
    again = await client.post("/api/workflows", json={
        "name": "Dup", "nodes": [{"key": "a", "role_id": role}], "edges": [],
    })
    assert again.status_code == 409


async def test_delete(client):
    created = (await loop_workflow(client)).json()
    assert (await client.delete(f"/api/workflows/{created['id']}")).status_code == 204
    assert (await client.get("/api/workflows")).json() == []
    assert (await client.get(f"/api/workflows/{created['id']}")).status_code == 404


# ------------------------------------------------------------------ validation


async def test_unknown_role_is_rejected(client):
    response = await client.post("/api/workflows", json={
        "name": "bad role", "nodes": [{"key": "a", "role_id": 999}], "edges": [],
    })
    assert response.status_code == 422
    assert "unknown role ids" in response.text


async def test_several_unconditional_edges_are_accepted_as_a_fan_out(client):
    """Every arm runs in parallel, as manager_dispatch does in the example graph."""
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "fan out",
        "nodes": [{"key": "dispatch", "role_id": role, "is_start": True},
                  {"key": "b", "role_id": role}, {"key": "c", "role_id": role}],
        "edges": [{"from_key": "dispatch", "to_key": "b", "label": "to_b"},
                  {"from_key": "dispatch", "to_key": "c", "label": "to_c"},
                  {"from_key": "b", "to_key": None, "label": "done"},
                  {"from_key": "c", "to_key": None, "label": "done"}],
    })
    assert response.status_code == 201, response.text


async def test_mixing_conditional_and_unconditional_edges_is_rejected(client):
    """Ambiguous: is the bare edge a fan-out arm, or an undescribed fallback?"""
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "ambiguous",
        "nodes": [{"key": "a", "role_id": role, "is_start": True},
                  {"key": "b", "role_id": role}, {"key": "c", "role_id": role}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "x"},
                  {"from_key": "a", "to_key": "c", "label": "y",
                   "condition": "something is wrong"},
                  {"from_key": "b", "to_key": None, "label": "done"},
                  {"from_key": "c", "to_key": None, "label": "done"}],
    })
    assert response.status_code == 422
    assert "mixes unconditional edges" in response.text


async def test_a_graph_that_cannot_finish_is_rejected(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "endless",
        "nodes": [{"key": "a", "role_id": role, "is_start": True}, {"key": "b", "role_id": role}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "there"},
                  {"from_key": "b", "to_key": "a", "label": "back"}],
    })
    assert response.status_code == 422
    assert "never finish" in response.text


async def test_validate_endpoint_reports_problems_without_saving(client):
    role = await make_role(client, "Any")
    result = (await client.post("/api/workflows/validate", json={
        "name": "draft",
        "nodes": [{"key": "a", "role_id": role, "is_start": True}, {"key": "lonely", "role_id": role}],
        "edges": [],
    })).json()
    assert result["ok"] is False
    assert any("unreachable" in p for p in result["problems"])
    assert (await client.get("/api/workflows")).json() == []  # nothing was stored


async def test_validate_endpoint_accepts_a_good_graph(client):
    role = await make_role(client, "Any")
    result = (await client.post("/api/workflows/validate", json={
        "name": "draft", "nodes": [{"key": "a", "role_id": role}], "edges": [],
    })).json()
    assert result == {"ok": True, "problems": []}


# ----------------------------------------------------------- tasks and runs


async def test_a_task_can_run_a_workflow_instead_of_a_team(client, project):
    workflow = (await loop_workflow(client)).json()
    created = await client.post("/api/tasks", json={
        "name": "ship it", "prompt": "Fix the parser", "project_path": project,
        "workflow_id": workflow["id"],
    })
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["workflow_id"] == workflow["id"]
    assert task["workflow_name"] == workflow["name"]
    assert task["team_id"] is None


async def test_a_task_cannot_have_both_a_team_and_a_workflow(client, project):
    workflow = (await loop_workflow(client)).json()
    role = workflow["nodes"][0]["role_id"]
    team = (await client.post("/api/teams", json={
        "name": "Squad", "members": [{"role_id": role, "is_lead": True}],
    })).json()
    response = await client.post("/api/tasks", json={
        "name": "both", "prompt": "x", "project_path": project,
        "team_id": team["id"], "workflow_id": workflow["id"],
    })
    assert response.status_code == 422
    assert "only one of" in response.text


async def test_a_task_still_needs_one_of_them(client, project):
    response = await client.post("/api/tasks", json={
        "name": "neither", "prompt": "x", "project_path": project,
    })
    assert response.status_code == 422


async def test_switching_a_task_from_a_team_to_a_workflow(client, project):
    workflow = (await loop_workflow(client)).json()
    role = workflow["nodes"][0]["role_id"]
    team = (await client.post("/api/teams", json={
        "name": "Squad", "members": [{"role_id": role, "is_lead": True}],
    })).json()
    task = (await client.post("/api/tasks", json={
        "name": "t", "prompt": "x", "project_path": project, "team_id": team["id"],
    })).json()

    updated = await client.put(f"/api/tasks/{task['id']}", json={
        "name": "t", "prompt": "x", "project_path": project, "workflow_id": workflow["id"],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["workflow_id"] == workflow["id"]
    assert updated.json()["team_id"] is None


async def test_the_run_snapshot_captures_the_graph(client, project):
    """A run must stay explainable after the workflow is edited or deleted."""
    from app.services.snapshot import build_snapshot
    from app.services import crud
    from app.db import sessionmaker

    workflow = (await loop_workflow(client)).json()
    task = (await client.post("/api/tasks", json={
        "name": "t", "prompt": "Fix the parser", "project_path": project,
        "workflow_id": workflow["id"],
    })).json()

    async with sessionmaker()() as session:
        row = await crud.load_task(session, task["id"])
        snapshot = await build_snapshot(session, row)

    assert snapshot.workflow is not None
    assert snapshot.workflow.start.key == "build"
    assert [n.key for n in snapshot.workflow.nodes] == ["build", "test"]
    assert snapshot.workflow.public()["edges"][2]["to"] == "END"
    # Every node's role is in the roster, so run history shows who took part.
    assert [r.name for r in snapshot.roles] == ["Engineer", "Tester"]


# ---------------------------------------------------------------- canvas layout


async def test_positions_round_trip(client):
    role = await make_role(client, "Placer")
    created = (await client.post("/api/workflows", json={
        "name": "placed",
        "nodes": [
            {"key": "a", "role_id": role, "is_start": True, "pos_x": 120.0, "pos_y": 40.0},
            {"key": "b", "role_id": role},
        ],
        "edges": [{"from_key": "a", "to_key": "b", "label": "next"}],
    })).json()
    by_key = {n["key"]: n for n in created["nodes"]}
    assert (by_key["a"]["pos_x"], by_key["a"]["pos_y"]) == (120.0, 40.0)
    # An unplaced node stays null, which is the signal to lay it out automatically.
    assert by_key["b"]["pos_x"] is None


async def test_layout_endpoint_saves_positions_without_touching_the_graph(client):
    created = (await loop_workflow(client)).json()
    response = await client.patch(f"/api/workflows/{created['id']}/layout", json={
        "positions": [{"key": "build", "x": 30, "y": 30}, {"key": "test", "x": 260, "y": 130}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    by_key = {n["key"]: n for n in body["nodes"]}
    assert (by_key["build"]["pos_x"], by_key["build"]["pos_y"]) == (30.0, 30.0)
    assert (by_key["test"]["pos_x"], by_key["test"]["pos_y"]) == (260.0, 130.0)
    # The graph itself is untouched.
    assert len(body["edges"]) == len(created["edges"])
    assert [n["key"] for n in body["nodes"]] == ["build", "test"]


async def test_layout_survives_a_graph_edit_that_keeps_the_node(client):
    created = (await loop_workflow(client)).json()
    await client.patch(f"/api/workflows/{created['id']}/layout", json={
        "positions": [{"key": "build", "x": 50, "y": 60}],
    })
    role = created["nodes"][0]["role_id"]
    updated = (await client.put(f"/api/workflows/{created['id']}", json={
        "name": created["name"],
        "max_steps": created["max_steps"],
        "nodes": [{"key": "build", "role_id": role, "is_start": True,
                   "pos_x": 50, "pos_y": 60}],
        "edges": [],
    })).json()
    assert (updated["nodes"][0]["pos_x"], updated["nodes"][0]["pos_y"]) == (50.0, 60.0)


async def test_layout_rejects_unknown_node_keys(client):
    created = (await loop_workflow(client)).json()
    response = await client.patch(f"/api/workflows/{created['id']}/layout", json={
        "positions": [{"key": "ghost", "x": 1, "y": 2}],
    })
    assert response.status_code == 422
    assert "unknown node keys" in response.text


async def test_layout_on_a_missing_workflow_is_404(client):
    response = await client.patch("/api/workflows/999/layout", json={
        "positions": [{"key": "a", "x": 0, "y": 0}],
    })
    assert response.status_code == 404


async def test_layout_needs_at_least_one_position(client):
    created = (await loop_workflow(client)).json()
    response = await client.patch(
        f"/api/workflows/{created['id']}/layout", json={"positions": []}
    )
    assert response.status_code == 422


async def test_an_invalid_graph_can_still_be_dragged(client):
    """Positions must be savable while a graph is mid-assembly and would not validate."""
    role = await make_role(client, "Any")
    created = (await client.post("/api/workflows", json={
        "name": "wip", "nodes": [{"key": "a", "role_id": role, "is_start": True}], "edges": [],
    })).json()
    # A half-described branch: a full save would be rejected.
    bad = await client.put(f"/api/workflows/{created['id']}", json={
        "name": "wip",
        "nodes": [{"key": "a", "role_id": role, "is_start": True},
                  {"key": "b", "role_id": role}, {"key": "c", "role_id": role}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "x"},
                  {"from_key": "a", "to_key": "c", "label": "y",
                   "condition": "something is wrong"}],
    })
    assert bad.status_code == 422

    # Dragging the node that does exist still works.
    ok = await client.patch(f"/api/workflows/{created['id']}/layout", json={
        "positions": [{"key": "a", "x": 90, "y": 90}],
    })
    assert ok.status_code == 200
    assert ok.json()["nodes"][0]["pos_x"] == 90.0


# --------------------------------------------- the example graph, end to end


async def test_the_example_graph_can_be_stored_and_read_back(client):
    """docs/example_graph.py expressed through the API, with every feature it needs."""
    roles = {}
    for name in ("Manager", "Architect", "Designer", "Tester", "Engineer",
                 "ProjectManager", "Human"):
        roles[name] = await make_role(client, name)

    created = await client.post("/api/workflows", json={
        "name": "Software team",
        "description": "The LangGraph example: parallel design, review loop, delivery loop.",
        "max_steps": 30,
        "escalation_key": "human",
        "nodes": [
            {"key": "dispatch", "role_id": roles["Manager"], "is_start": True,
             "max_visits": 2, "output_key": "brief"},
            {"key": "architect", "role_id": roles["Architect"], "max_visits": 4,
             "output_key": "arch_doc"},
            {"key": "designer", "role_id": roles["Designer"], "max_visits": 4,
             "output_key": "design_doc"},
            {"key": "tester", "role_id": roles["Tester"], "max_visits": 4,
             "output_key": "test_cases"},
            {"key": "review", "role_id": roles["Manager"], "max_visits": 4,
             "output_key": "review_notes"},
            {"key": "engineer", "role_id": roles["Engineer"], "max_visits": 4,
             "output_key": "code"},
            {"key": "pm", "role_id": roles["ProjectManager"], "max_visits": 4,
             "output_key": "pm_report"},
            {"key": "human", "role_id": roles["Human"], "max_visits": 1},
        ],
        "edges": [
            # fan-out
            {"from_key": "dispatch", "to_key": "architect", "label": "to_architect"},
            {"from_key": "dispatch", "to_key": "designer", "label": "to_designer"},
            {"from_key": "dispatch", "to_key": "tester", "label": "to_tester"},
            # join
            {"from_key": "architect", "to_key": "review", "label": "report"},
            {"from_key": "designer", "to_key": "review", "label": "report"},
            {"from_key": "tester", "to_key": "review", "label": "report"},
            # review loop: a subset re-runs
            {"from_key": "review", "to_key": "architect", "label": "arch_issues",
             "condition": "the architecture needs changes"},
            {"from_key": "review", "to_key": "designer", "label": "design_issues",
             "condition": "the design needs changes"},
            {"from_key": "review", "to_key": "tester", "label": "test_issues",
             "condition": "the tests need changes"},
            {"from_key": "review", "to_key": "engineer", "label": "approved",
             "condition": "all three are acceptable", "is_default": True},
            # delivery loop, with resets when work goes back upstream
            {"from_key": "engineer", "to_key": "pm", "label": "deliver"},
            {"from_key": "pm", "to_key": "engineer", "label": "code_bug",
             "condition": "the code itself is wrong"},
            {"from_key": "pm", "to_key": "architect", "label": "arch_issue",
             "condition": "the architecture is wrong", "resets": ["review", "architect"]},
            {"from_key": "pm", "to_key": None, "label": "accepted",
             "condition": "tests pass and it matches the documents", "is_default": True},
            {"from_key": "human", "to_key": None, "label": "handed_over"},
        ],
    })
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["escalation_key"] == "human"
    by_key = {n["key"]: n for n in body["nodes"]}
    assert by_key["architect"]["output_key"] == "arch_doc"
    assert by_key["dispatch"]["is_start"] is True

    by_label = {e["label"]: e for e in body["edges"]}
    # The fan-out arms are unconditional.
    assert by_label["to_architect"]["condition"] == ""
    # The review loop carries conditions and one default.
    assert by_label["arch_issues"]["condition"].startswith("the architecture")
    assert by_label["approved"]["is_default"] is True
    # The upstream edge resets the review budget.
    assert sorted(by_label["arch_issue"]["resets"]) == ["architect", "review"]
    # Acceptance finishes the workflow.
    assert by_label["accepted"]["to_key"] is None

    # And it survives a round trip.
    reread = (await client.get(f"/api/workflows/{body['id']}")).json()
    assert reread["escalation_key"] == "human"
    assert sorted(n["output_key"] for n in reread["nodes"]) == [
        "arch_doc", "brief", "code", "design_doc", "human", "pm_report",
        "review_notes", "test_cases",
    ]


async def test_an_unknown_escalation_node_is_rejected(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "bad escalation", "escalation_key": "nobody",
        "nodes": [{"key": "a", "role_id": role, "is_start": True}], "edges": [],
    })
    assert response.status_code == 422
    assert "escalation node" in response.text


async def test_two_nodes_writing_the_same_artifact_is_rejected(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "clashing outputs",
        "nodes": [{"key": "a", "role_id": role, "is_start": True, "output_key": "doc"},
                  {"key": "b", "role_id": role, "output_key": "doc"}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "next"}],
    })
    assert response.status_code == 422
    assert "same output name" in response.text


# ------------------------------------------------------ per-node output schemas


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["approved", "summary"],
    "additionalProperties": False,
}


async def test_a_node_output_schema_round_trips(client):
    role = await make_role(client, "Reviewer")
    created = await client.post("/api/workflows", json={
        "name": "structured",
        "nodes": [{"key": "review", "role_id": role, "is_start": True,
                   "output_key": "verdict", "output_schema": REVIEW_SCHEMA}],
        "edges": [],
    })
    assert created.status_code == 201, created.text
    node = created.json()["nodes"][0]
    assert node["output_schema"] == REVIEW_SCHEMA
    assert node["output_key"] == "verdict"

    reread = (await client.get(f"/api/workflows/{created.json()['id']}")).json()
    assert reread["nodes"][0]["output_schema"]["properties"]["approved"]["type"] == "boolean"


async def test_a_node_without_a_schema_returns_null(client):
    role = await make_role(client, "Any")
    created = await client.post("/api/workflows", json={
        "name": "prose", "nodes": [{"key": "a", "role_id": role}], "edges": [],
    })
    assert created.json()["nodes"][0]["output_schema"] is None


async def test_an_unusable_output_schema_is_rejected(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "bad schema",
        "nodes": [{"key": "a", "role_id": role, "output_schema": {"type": "string"}}],
        "edges": [],
    })
    assert response.status_code == 422
    assert "output schema" in response.text


async def test_a_schema_with_an_untyped_field_is_rejected(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "untyped",
        "nodes": [{"key": "a", "role_id": role,
                   "output_schema": {"type": "object", "properties": {"x": {}}}}],
        "edges": [],
    })
    assert response.status_code == 422
    # The body is JSON, so the quotes in the message arrive escaped.
    assert "needs a" in response.text and "type" in response.text


async def test_the_validate_endpoint_reports_schema_problems(client):
    role = await make_role(client, "Any")
    result = (await client.post("/api/workflows/validate", json={
        "name": "draft",
        "nodes": [{"key": "a", "role_id": role,
                   "output_schema": {"type": "object", "properties": {}}}],
        "edges": [],
    })).json()
    assert result["ok"] is False
    assert any("properties" in p for p in result["problems"])


async def test_the_shipped_example_carries_schemas(client):
    """The example's review and pm steps mirror ReviewResult and PMReport."""
    examples = (await client.get("/api/workflows/examples")).json()
    team = next(e for e in examples if e["name"] == "Software team")
    by_key = {n["key"]: n for n in team["nodes"]}
    assert by_key["review"]["output_schema"]["properties"]["approved"]["type"] == "boolean"
    failure = by_key["pm"]["output_schema"]["properties"]["failure_type"]
    assert "code_bug" in failure["enum"]


# ----------------------------------------- expression conditions and node inputs


async def test_an_expression_edge_round_trips(client):
    """The motivating case, stored and read back."""
    role = await make_role(client, "Reviewer")
    created = await client.post("/api/workflows", json={
        "name": "expr",
        "nodes": [
            {"key": "review", "role_id": role, "is_start": True,
             "output_key": "review_notes",
             "output_schema": {"type": "object", "properties": {
                 "approved": {"type": "boolean"},
                 "issues": {"type": "array", "items": {"type": "string"}}}}},
            {"key": "architect", "role_id": role},
        ],
        "edges": [
            {"from_key": "review", "to_key": "architect", "label": "arch_issues",
             "expression": "issues contains architecture"},
            {"from_key": "review", "to_key": None, "label": "approved",
             "expression": "approved is true", "is_default": True},
            {"from_key": "architect", "to_key": "review", "label": "back"},
        ],
    })
    assert created.status_code == 201, created.text
    by_label = {e["label"]: e for e in created.json()["edges"]}
    assert by_label["arch_issues"]["expression"] == "issues contains architecture"
    assert by_label["arch_issues"]["condition"] == ""


async def test_an_expression_satisfies_the_branch_rule(client):
    """An expression describes the edge, so a branch needs no worded condition."""
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "expr branch",
        "nodes": [{"key": "a", "role_id": role, "is_start": True},
                  {"key": "b", "role_id": role}, {"key": "c", "role_id": role}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "x",
                   "expression": "score > 5"},
                  {"from_key": "a", "to_key": "c", "label": "y", "is_default": True},
                  {"from_key": "b", "to_key": None, "label": "done"},
                  {"from_key": "c", "to_key": None, "label": "done"}],
    })
    assert response.status_code == 201, response.text


async def test_an_unparseable_expression_is_rejected_with_the_reason(client):
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "bad expr",
        "nodes": [{"key": "a", "role_id": role, "is_start": True}],
        "edges": [{"from_key": "a", "to_key": None, "label": "x",
                   "expression": "issues contains"}],
    })
    assert response.status_code == 422
    assert "expression" in response.text and "right-hand side" in response.text


async def test_node_inputs_round_trip(client):
    role = await make_role(client, "Any")
    created = await client.post("/api/workflows", json={
        "name": "inputs",
        "nodes": [
            {"key": "review", "role_id": role, "is_start": True,
             "output_key": "review_notes"},
            {"key": "architect", "role_id": role,
             "inputs": [{"from": "review_notes", "path": "issues", "as": "my_notes"}]},
        ],
        "edges": [{"from_key": "review", "to_key": "architect", "label": "next"},
                  {"from_key": "architect", "to_key": None, "label": "done"}],
    })
    assert created.status_code == 201, created.text
    architect = next(n for n in created.json()["nodes"] if n["key"] == "architect")
    assert architect["inputs"] == [
        {"from": "review_notes", "path": "issues", "as": "my_notes"}
    ]


async def test_an_input_from_an_unknown_artifact_is_rejected(client):
    """A typo in a source name would otherwise silently hand the step nothing."""
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "bad input",
        "nodes": [{"key": "a", "role_id": role, "is_start": True},
                  {"key": "b", "role_id": role,
                   "inputs": [{"from": "reviewnotes", "path": "issues"}]}],
        "edges": [{"from_key": "a", "to_key": "b", "label": "next"},
                  {"from_key": "b", "to_key": None, "label": "done"}],
    })
    assert response.status_code == 422
    assert "which no node writes" in response.text


async def test_an_input_may_name_the_producing_nodes_own_key(client):
    """With no output_key, a node's artifact is its key, so that is a valid source."""
    role = await make_role(client, "Any")
    response = await client.post("/api/workflows", json={
        "name": "key as source",
        "nodes": [{"key": "design", "role_id": role, "is_start": True},
                  {"key": "build", "role_id": role,
                   "inputs": [{"from": "design"}]}],
        "edges": [{"from_key": "design", "to_key": "build", "label": "next"},
                  {"from_key": "build", "to_key": None, "label": "done"}],
    })
    assert response.status_code == 201, response.text
