"""Roles and teams."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Role, Team, TeamRole
from app.schemas import RoleIn, RoleOut, TeamIn, TeamOut
from app.services import catalog

roles_router = APIRouter(prefix="/api/roles", tags=["roles"])
teams_router = APIRouter(prefix="/api/teams", tags=["teams"])


@roles_router.get("", response_model=list[RoleOut])
async def list_roles(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(Role).order_by(Role.name))).scalars().all()


@roles_router.get("/suggested")
async def suggested_roles(session: AsyncSession = Depends(get_session)):
    names = {r.name for r in (await session.execute(select(Role))).scalars().all()}
    return [r for r in catalog.DEFAULT_ROLES if r["name"] not in names]


@roles_router.post("", response_model=RoleOut, status_code=201)
async def create_role(payload: RoleIn, session: AsyncSession = Depends(get_session)):
    if (await session.execute(select(Role).where(Role.name == payload.name))).scalars().first():
        raise HTTPException(409, f"a role named {payload.name!r} already exists")
    row = Role(
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model_config_id=payload.model_config_id,
    )
    session.add(row)
    await session.commit()
    return row


@roles_router.put("/{role_id}", response_model=RoleOut)
async def update_role(role_id: int, payload: RoleIn, session: AsyncSession = Depends(get_session)):
    row = await session.get(Role, role_id)
    if row is None:
        raise HTTPException(404, "role not found")
    row.name = payload.name
    row.description = payload.description
    row.system_prompt = payload.system_prompt
    row.model_config_id = payload.model_config_id
    await session.commit()
    return row


@roles_router.delete("/{role_id}", status_code=204)
async def delete_role(role_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Role, role_id)
    if row is None:
        raise HTTPException(404, "role not found")
    await session.delete(row)
    await session.commit()


async def _load_team(session: AsyncSession, team_id: int) -> Team:
    """Re-read a team with its members eagerly loaded.

    ``session.get`` would hand back the identity-mapped instance whose collections
    may still be unloaded, and touching them then triggers a lazy load in a sync
    context — which async SQLAlchemy cannot do. ``populate_existing`` refreshes the
    cached instance instead.
    """
    stmt = (
        select(Team)
        .options(selectinload(Team.members).selectinload(TeamRole.role))
        .where(Team.id == team_id)
        .execution_options(populate_existing=True)
    )
    team = (await session.execute(stmt)).scalars().first()
    if team is None:
        raise HTTPException(404, "team not found")
    return team


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
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (await session.execute(select(Team).where(Team.name == payload.name))).scalars().first():
        raise HTTPException(409, f"a team named {payload.name!r} already exists")

    role_ids = [m.role_id for m in payload.members]
    found = (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all()
    if len(found) != len(role_ids):
        missing = sorted(set(role_ids) - {r.id for r in found})
        raise HTTPException(422, f"unknown role ids: {missing}")

    team = Team(name=payload.name, description=payload.description)
    session.add(team)
    await session.flush()
    any_lead = any(m.is_lead for m in payload.members)
    for position, member in enumerate(payload.members):
        session.add(
            TeamRole(
                team_id=team.id,
                role_id=member.role_id,
                position=position,
                is_lead=member.is_lead or (not any_lead and position == 0),
            )
        )
    await session.commit()
    return TeamOut.of(await _load_team(session, team.id))


@teams_router.put("/{team_id}", response_model=TeamOut)
async def update_team(team_id: int, payload: TeamIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    team = await _load_team(session, team_id)
    team.name = payload.name
    team.description = payload.description
    team.members.clear()
    await session.flush()
    any_lead = any(m.is_lead for m in payload.members)
    for position, member in enumerate(payload.members):
        session.add(
            TeamRole(
                team_id=team.id,
                role_id=member.role_id,
                position=position,
                is_lead=member.is_lead or (not any_lead and position == 0),
            )
        )
    await session.commit()
    return TeamOut.of(await _load_team(session, team_id))


@teams_router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: int, session: AsyncSession = Depends(get_session)):
    team = await _load_team(session, team_id)
    await session.delete(team)
    await session.commit()
