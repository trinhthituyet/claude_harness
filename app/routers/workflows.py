"""Workflows: the graph of roles a task can run instead of a flat team."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Workflow
from app.schemas import LayoutIn, WorkflowIn, WorkflowOut
from app.services import crud, graph

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


def _http(exc: crud.CrudError) -> HTTPException:
    return HTTPException(409 if exc.conflict else 422, str(exc))


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Workflow).options(*crud.workflow_loaders()).order_by(Workflow.name)
        )
    ).scalars().all()
    return [WorkflowOut.of(r) for r in rows]


@router.get("/examples")
async def examples():
    """Shapes worth starting from, described in terms of role names to fill in."""
    return [
        {
            "name": "Software team",
            "description": "Parallel design, a review loop that re-runs only the roles "
                           "with issues, then a delivery loop — the shape of "
                           "docs/example_graph.py.",
            "max_steps": 24,
            "escalation_key": "human",
            "nodes": [
                {"key": "dispatch", "role": "Software Architect", "is_start": True,
                 "output_key": "brief", "max_visits": 1,
                 "instructions": "State briefly what each role should produce."},
                {"key": "architect", "role": "Software Architect",
                 "output_key": "arch_doc", "max_visits": 3},
                {"key": "designer", "role": "Designer",
                 "output_key": "design_doc", "max_visits": 3},
                {"key": "tester", "role": "Tester",
                 "output_key": "test_cases", "max_visits": 3},
                {"key": "review", "role": "Software Architect",
                 "output_key": "review_notes", "max_visits": 3,
                 "instructions": "Review the three artifacts against the goal and each "
                                 "other. Say which role must change something."},
                {"key": "engineer", "role": "Software Engineer",
                 "output_key": "code", "max_visits": 3},
                {"key": "pm", "role": "Tester", "output_key": "pm_report", "max_visits": 3,
                 "instructions": "Check the code against the tests and the documents. "
                                 "Classify what is wrong, or accept."},
                {"key": "human", "role": "Software Architect", "max_visits": 1,
                 "instructions": "Summarise what is unresolved and stop."},
            ],
            "edges": [
                # Unconditional siblings fan out: all three run in parallel.
                {"from_key": "dispatch", "to_key": "architect", "label": "to_architect"},
                {"from_key": "dispatch", "to_key": "designer", "label": "to_designer"},
                {"from_key": "dispatch", "to_key": "tester", "label": "to_tester"},
                # Converging on one node joins them: review runs once.
                {"from_key": "architect", "to_key": "review", "label": "report"},
                {"from_key": "designer", "to_key": "review", "label": "report"},
                {"from_key": "tester", "to_key": "review", "label": "report"},
                # A branch may select several arms: only the flagged roles re-run.
                {"from_key": "review", "to_key": "architect", "label": "arch_issues",
                 "condition": "the architecture must change"},
                {"from_key": "review", "to_key": "designer", "label": "design_issues",
                 "condition": "the design must change"},
                {"from_key": "review", "to_key": "tester", "label": "test_issues",
                 "condition": "the test cases must change"},
                {"from_key": "review", "to_key": "engineer", "label": "approved",
                 "condition": "all three are acceptable", "is_default": True},
                {"from_key": "engineer", "to_key": "pm", "label": "deliver"},
                {"from_key": "pm", "to_key": "engineer", "label": "code_bug",
                 "condition": "the code itself is wrong"},
                {"from_key": "pm", "to_key": "architect", "label": "arch_issue",
                 "condition": "the architecture is wrong",
                 "resets": ["review", "architect"]},
                {"from_key": "pm", "to_key": "tester", "label": "test_issue",
                 "condition": "a test case is wrong rather than the code",
                 "resets": ["review", "tester"]},
                {"from_key": "pm", "to_key": None, "label": "accepted",
                 "condition": "it passes and matches the documents", "is_default": True},
                {"from_key": "human", "to_key": None, "label": "handed_over"},
            ],
        },
        {
            "name": "Design, build, test",
            "description": "The classic loop: test failures go back to the engineer.",
            "nodes": [
                {"key": "design", "role": "Software Architect", "is_start": True},
                {"key": "build", "role": "Software Engineer"},
                {"key": "test", "role": "Tester"},
            ],
            "edges": [
                {"from_key": "design", "to_key": "build", "label": "to_build"},
                {"from_key": "build", "to_key": "test", "label": "to_test"},
                {"from_key": "test", "to_key": "build", "label": "needs_rework",
                 "condition": "any test fails, or the implementation is incomplete"},
                {"from_key": "test", "to_key": None, "label": "approved",
                 "condition": "everything passes and the goal is met", "is_default": True},
            ],
        },
        {
            "name": "Review then fix",
            "description": "One reviewer, one fixer, looping until the review is clean.",
            "nodes": [
                {"key": "review", "role": "Software Architect", "is_start": True},
                {"key": "fix", "role": "Software Engineer"},
            ],
            "edges": [
                {"from_key": "review", "to_key": "fix", "label": "has_findings",
                 "condition": "the review found something that must change"},
                {"from_key": "review", "to_key": None, "label": "clean",
                 "condition": "nothing needs changing", "is_default": True},
                {"from_key": "fix", "to_key": "review", "label": "back_to_review"},
            ],
        },
    ]


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(workflow_id: int, session: AsyncSession = Depends(get_session)):
    try:
        return WorkflowOut.of(await crud.load_workflow(session, workflow_id))
    except crud.CrudError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/validate")
async def validate(payload: WorkflowIn):
    """Check a graph without saving it, so the editor can show problems as you type."""
    try:
        payload.check()
    except graph.GraphError as exc:
        return {"ok": False, "problems": str(exc).split("; ")}
    except ValueError as exc:
        return {"ok": False, "problems": [str(exc)]}
    return {"ok": True, "problems": []}


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(payload: WorkflowIn, session: AsyncSession = Depends(get_session)):
    try:
        row = await crud.create_workflow(session, payload)
        loaded = await crud.load_workflow(session, row.id)
    except crud.CrudError as exc:
        raise _http(exc) from exc
    except ValueError as exc:  # includes GraphError
        raise HTTPException(422, str(exc)) from exc
    result = WorkflowOut.of(loaded)
    await session.commit()
    return result


@router.put("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: int, payload: WorkflowIn, session: AsyncSession = Depends(get_session)
):
    try:
        await crud.update_workflow(session, workflow_id, payload)
        loaded = await crud.load_workflow(session, workflow_id)
    except crud.CrudError as exc:
        raise HTTPException(404 if "no workflow" in str(exc) else 422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = WorkflowOut.of(loaded)
    await session.commit()
    return result


@router.patch("/{workflow_id}/layout", response_model=WorkflowOut)
async def save_layout(
    workflow_id: int, payload: LayoutIn, session: AsyncSession = Depends(get_session)
):
    """Persist canvas positions only.

    Dragging a node must not rewrite the graph or re-run validation — a half-built
    graph is a normal state to be dragging around in, and a full save would reject it.
    """
    try:
        workflow = await crud.load_workflow(session, workflow_id)
    except crud.CrudError as exc:
        raise HTTPException(404, str(exc)) from exc

    by_key = {node.key: node for node in workflow.nodes}
    unknown = [p.key for p in payload.positions if p.key not in by_key]
    if unknown:
        raise HTTPException(422, f"unknown node keys: {unknown}")
    for position in payload.positions:
        node = by_key[position.key]
        node.pos_x = position.x
        node.pos_y = position.y
    await session.commit()
    return WorkflowOut.of(await crud.load_workflow(session, workflow_id))


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Workflow, workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    await session.delete(row)
    await session.commit()
