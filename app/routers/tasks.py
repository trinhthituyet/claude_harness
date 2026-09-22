"""Tasks: CRUD, inline team creation, and triggering a run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Task
from app.schemas import TaskIn, TaskOut
from app.security.policy import PolicyError, resolve_project_root
from app.services import crud
from app.services.runner import manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _http(exc: crud.CrudError) -> HTTPException:
    return HTTPException(409 if exc.conflict else 422, str(exc))


@router.get("", response_model=list[TaskOut])
async def list_tasks(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Task).options(*crud.task_loaders()).order_by(Task.name))
    ).scalars().all()
    return [TaskOut.of(r) for r in rows]


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(payload: TaskIn, session: AsyncSession = Depends(get_session)):
    try:
        row = await crud.create_task(session, payload)
        loaded = await crud.load_task(session, row.id)
    except crud.CrudError as exc:
        raise _http(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = TaskOut.of(loaded)
    await session.commit()
    return result


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, payload: TaskIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
        resolve_project_root(payload.project_path)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        task = await crud.load_task(session, task_id)
        await crud.assert_references_exist(session, payload)
        task.team_id = await crud.resolve_task_team(session, payload)
        crud.apply_task_flags(task, payload)
        await crud.set_task_links(session, task, payload)
        loaded = await crud.load_task(session, task_id)
    except crud.CrudError as exc:
        raise HTTPException(404 if "no task" in str(exc) else 422, str(exc)) from exc
    result = TaskOut.of(loaded)
    await session.commit()
    return result


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Task, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    await session.delete(row)
    await session.commit()


@router.post("/{task_id}/run", status_code=202)
async def run_task(task_id: int, session: AsyncSession = Depends(get_session)):
    try:
        task = await crud.load_task(session, task_id)
    except crud.CrudError as exc:
        raise HTTPException(404, str(exc)) from exc
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
