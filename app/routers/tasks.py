"""Tasks: CRUD, inline team creation, and triggering a run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import McpServer, Role, Skill, Task, TaskMcpServer, TaskSkill, Team, TeamRole
from app.schemas import InlineTeam, TaskIn, TaskOut
from app.security.policy import PolicyError, resolve_project_root
from app.services.runner import manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


async def _materialise_inline_team(session: AsyncSession, spec: InlineTeam) -> int:
    """Create a team (and any brand-new roles) defined inline on the task form."""
    if (await session.execute(select(Team).where(Team.name == spec.name))).scalars().first():
        raise HTTPException(409, f"a team named {spec.name!r} already exists")

    role_ids = list(spec.role_ids)
    for new_role in spec.new_roles:
        existing = (
            await session.execute(select(Role).where(Role.name == new_role.name))
        ).scalars().first()
        if existing is not None:
            raise HTTPException(409, f"a role named {new_role.name!r} already exists")
        row = Role(
            name=new_role.name,
            description=new_role.description,
            system_prompt=new_role.system_prompt,
            model_config_id=new_role.model_config_id,
        )
        session.add(row)
        await session.flush()
        role_ids.append(row.id)

    found = (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all()
    if len(found) != len(set(role_ids)):
        missing = sorted(set(role_ids) - {r.id for r in found})
        raise HTTPException(422, f"unknown role ids: {missing}")
    by_id = {r.id: r for r in found}

    team = Team(name=spec.name, description=spec.description)
    session.add(team)
    await session.flush()
    lead_index = 0
    if spec.lead_role_name:
        for index, role_id in enumerate(role_ids):
            if by_id[role_id].name == spec.lead_role_name:
                lead_index = index
                break
    for position, role_id in enumerate(role_ids):
        session.add(
            TeamRole(
                team_id=team.id,
                role_id=role_id,
                position=position,
                is_lead=position == lead_index,
            )
        )
    await session.flush()
    return team.id


async def _validate_references(session: AsyncSession, payload: TaskIn) -> None:
    if payload.skill_ids:
        rows = (
            await session.execute(select(Skill.id).where(Skill.id.in_(payload.skill_ids)))
        ).scalars().all()
        missing = sorted(set(payload.skill_ids) - set(rows))
        if missing:
            raise HTTPException(422, f"unknown skill ids: {missing}")
    if payload.mcp_server_ids:
        rows = (
            await session.execute(
                select(McpServer.id).where(McpServer.id.in_(payload.mcp_server_ids))
            )
        ).scalars().all()
        missing = sorted(set(payload.mcp_server_ids) - set(rows))
        if missing:
            raise HTTPException(422, f"unknown MCP server ids: {missing}")


def _apply_flags(row: Task, payload: TaskIn) -> None:
    row.name = payload.name
    row.prompt = payload.prompt
    row.project_path = payload.project_path
    row.model_config_id = payload.model_config_id
    row.trust_project_settings = payload.trust_project_settings
    row.sandbox_bash = payload.sandbox_bash
    row.paranoid_mode = payload.paranoid_mode
    row.network_enabled = payload.network_enabled
    row.approval_timeout_s = payload.approval_timeout_s
    row.max_turns = payload.max_turns
    row.max_budget_usd = payload.max_budget_usd


async def _set_links(session: AsyncSession, task: Task, payload: TaskIn) -> None:
    """Replace the skill / MCP links.

    Done with DELETE statements rather than by clearing the ORM collections: on a
    freshly flushed Task those collections are unloaded, and touching them would
    trigger a lazy load that async SQLAlchemy cannot perform.
    """
    await session.execute(delete(TaskSkill).where(TaskSkill.task_id == task.id))
    await session.execute(delete(TaskMcpServer).where(TaskMcpServer.task_id == task.id))
    for skill_id in dict.fromkeys(payload.skill_ids):
        session.add(TaskSkill(task_id=task.id, skill_id=skill_id))
    for server_id in dict.fromkeys(payload.mcp_server_ids):
        session.add(TaskMcpServer(task_id=task.id, server_id=server_id))


def _task_loaders():
    """Eager-load everything TaskOut and the run snapshot read."""
    return (
        selectinload(Task.team).selectinload(Team.members).selectinload(TeamRole.role),
        selectinload(Task.skill_links),
        selectinload(Task.mcp_links),
    )


async def _get(session: AsyncSession, task_id: int) -> Task:
    stmt = (
        select(Task)
        .options(*_task_loaders())
        .where(Task.id == task_id)
        .execution_options(populate_existing=True)
    )
    task = (await session.execute(stmt)).scalars().first()
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@router.get("", response_model=list[TaskOut])
async def list_tasks(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Task).options(*_task_loaders()).order_by(Task.name))
    ).scalars().all()
    return [TaskOut.of(r) for r in rows]


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(payload: TaskIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        resolve_project_root(payload.project_path)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _validate_references(session, payload)

    team_id = payload.team_id
    if payload.inline_team is not None:
        team_id = await _materialise_inline_team(session, payload.inline_team)
    else:
        team = await session.get(Team, team_id)
        if team is None:
            raise HTTPException(422, f"unknown team id: {team_id}")
        if not team.members:
            raise HTTPException(422, f"team {team.name!r} has no roles")

    row = Task(team_id=team_id, prompt=payload.prompt, project_path=payload.project_path,
               name=payload.name)
    _apply_flags(row, payload)
    row.team_id = team_id
    session.add(row)
    await session.flush()
    await _set_links(session, row, payload)
    await session.commit()
    return TaskOut.of(await _get(session, row.id))


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, payload: TaskIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        resolve_project_root(payload.project_path)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _validate_references(session, payload)

    task = await _get(session, task_id)
    if payload.inline_team is not None:
        task.team_id = await _materialise_inline_team(session, payload.inline_team)
    elif payload.team_id is not None:
        team = await session.get(Team, payload.team_id)
        if team is None:
            raise HTTPException(422, f"unknown team id: {payload.team_id}")
        task.team_id = payload.team_id
    _apply_flags(task, payload)
    await _set_links(session, task, payload)
    await session.commit()
    return TaskOut.of(await _get(session, task_id))


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get(session, task_id)
    await session.delete(task)
    await session.commit()


@router.post("/{task_id}/run", status_code=202)
async def run_task(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get(session, task_id)
    if not task.prompt.strip():
        raise HTTPException(422, "this task has no prompt")
    if not task.project_path.strip():
        raise HTTPException(422, "this task has no project path")
    try:
        run_id = await manager.start(session, task)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"run_id": run_id}
