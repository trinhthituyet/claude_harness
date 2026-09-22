"""Roles and teams."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Role, Team, TeamRole
from app.schemas import RoleIn, RoleOut, TeamIn, TeamOut
from app.services import catalog, crud

roles_router = APIRouter(prefix="/api/roles", tags=["roles"])
teams_router = APIRouter(prefix="/api/teams", tags=["teams"])


def _http(exc: crud.CrudError) -> HTTPException:
    return HTTPException(409 if exc.conflict else 422, str(exc))


@roles_router.get("", response_model=list[RoleOut])
async def list_roles(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(Role).order_by(Role.name))).scalars().all()


@roles_router.get("/suggested")
async def suggested_roles(session: AsyncSession = Depends(get_session)):
    names = {r.name for r in (await session.execute(select(Role))).scalars().all()}
    return [r for r in catalog.DEFAULT_ROLES if r["name"] not in names]


@roles_router.post("", response_model=RoleOut, status_code=201)
async def create_role(payload: RoleIn, session: AsyncSession = Depends(get_session)):
    try:
        row = await crud.create_role(session, payload)
    except crud.CrudError as exc:
        raise _http(exc) from exc
    await session.commit()
    return row


@roles_router.put("/{role_id}", response_model=RoleOut)
async def update_role(role_id: int, payload: RoleIn, session: AsyncSession = Depends(get_session)):
    try:
        row = await crud.update_role(session, role_id, payload)
    except crud.CrudError as exc:
        raise HTTPException(404 if "no role" in str(exc) else 422, str(exc)) from exc
    await session.commit()
    return row


@roles_router.delete("/{role_id}", status_code=204)
async def delete_role(role_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Role, role_id)
    if row is None:
        raise HTTPException(404, "role not found")
    await session.delete(row)
    await session.commit()


@teams_router.get("", response_model=list[TeamOut])
async def list_teams(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Team)
            .options(selectinload(Team.members).selectinload(TeamRole.role))
            .order_by(Team.name)
        )
    ).scalars().all()
    return [TeamOut.of(r) for r in rows]


@teams_router.post("", response_model=TeamOut, status_code=201)
async def create_team(payload: TeamIn, session: AsyncSession = Depends(get_session)):
    try:
        team = await crud.create_team(session, payload)
        loaded = await crud.load_team(session, team.id)
    except crud.CrudError as exc:
        raise _http(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = TeamOut.of(loaded)
    await session.commit()
    return result


@teams_router.put("/{team_id}", response_model=TeamOut)
async def update_team(team_id: int, payload: TeamIn, session: AsyncSession = Depends(get_session)):
    try:
        team = await crud.load_team(session, team_id)
        await crud.replace_team_members(session, team, payload)
        loaded = await crud.load_team(session, team_id)
    except crud.CrudError as exc:
        raise HTTPException(404 if "no team" in str(exc) else 422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = TeamOut.of(loaded)
    await session.commit()
    return result


@teams_router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Team, team_id)
    if row is None:
        raise HTTPException(404, "team not found")
    await session.delete(row)
    await session.commit()
