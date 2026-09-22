"""Skills: installed list, suggestions, import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Skill
from app.schemas import SkillIn, SkillOut
from app.services import catalog, skill_discovery

router = APIRouter(prefix="/api/skills", tags=["skills"])


async def _reconcile(session: AsyncSession, project_path: str | None = None) -> list[Skill]:
    """Make the skills table reflect what is actually on disk."""
    discovered = skill_discovery.discover(project_path)
    by_name = {d.name: d for d in discovered}

    rows = (await session.execute(select(Skill))).scalars().all()
    existing = {r.name: r for r in rows}

    for name, found in by_name.items():
        row = existing.get(name)
        if row is None:
            row = Skill(name=name, origin="discovered")
            session.add(row)
            existing[name] = row
        row.description = found.description or row.description
        row.install_path = str(found.path)
        row.scope = found.scope
        row.status = "installed"
        row.metadata_json = dict(found.metadata)

    # A skill that vanished from disk is no longer installed, but keep the row so
    # tasks referencing it still render.
    for name, row in existing.items():
        if name not in by_name and row.origin == "discovered":
            row.status = "suggested"
            row.install_path = None

    await session.commit()
    return (await session.execute(select(Skill).order_by(Skill.name))).scalars().all()


@router.get("", response_model=list[SkillOut])
async def list_skills(session: AsyncSession = Depends(get_session)):
    return await _reconcile(session)


@router.get("/discover", response_model=list[SkillOut])
async def rediscover(project_path: str | None = None, session: AsyncSession = Depends(get_session)):
    return await _reconcile(session, project_path)


@router.get("/suggested")
async def suggested(session: AsyncSession = Depends(get_session)):
    installed = {
        r.name
        for r in (
            await session.execute(select(Skill).where(Skill.status == "installed"))
        ).scalars().all()
    }
    return [s for s in catalog.SUGGESTED_SKILLS if s["name"] not in installed]


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(payload: SkillIn, session: AsyncSession = Depends(get_session)):
    if (
        await session.execute(select(Skill).where(Skill.name == payload.name))
    ).scalars().first():
        raise HTTPException(409, f"a skill named {payload.name!r} already exists")
    row = Skill(
        name=payload.name,
        description=payload.description,
        source_url=payload.source_url,
        enabled=payload.enabled,
        status="suggested",
        origin="suggested",
    )
    session.add(row)
    await session.commit()
    return row


@router.post("/import", response_model=SkillOut, status_code=201)
async def import_skill(
    session: AsyncSession = Depends(get_session),
    file: UploadFile | None = File(default=None),
    name: str | None = Form(default=None),
    body: str | None = Form(default=None),
):
    """Install a skill from a .zip, an uploaded SKILL.md, or pasted markdown."""
    try:
        if file is not None:
            data = await file.read()
            filename = (file.filename or "").lower()
            if filename.endswith(".zip"):
                path = skill_discovery.install_from_zip(data)
            else:
                text = data.decode("utf-8", errors="replace")
                meta = skill_discovery.parse_frontmatter(text)
                skill_name = name or meta.get("name")
                if not skill_name:
                    raise HTTPException(422, "provide a name, or frontmatter with a name")
                path = skill_discovery.install_from_markdown(skill_name, text)
        elif name and body:
            path = skill_discovery.install_from_markdown(name, body)
        else:
            raise HTTPException(422, "upload a file, or provide both name and body")
    except skill_discovery.SkillImportError as exc:
        raise HTTPException(422, str(exc)) from exc

    rows = await _reconcile(session)
    for row in rows:
        if row.install_path == str(path):
            return row
    raise HTTPException(500, "skill installed but could not be read back")


@router.put("/{skill_id}", response_model=SkillOut)
async def update_skill(
    skill_id: int, payload: SkillIn, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Skill, skill_id)
    if row is None:
        raise HTTPException(404, "skill not found")
    row.description = payload.description
    row.source_url = payload.source_url
    row.enabled = payload.enabled
    await session.commit()
    return row


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Skill, skill_id)
    if row is None:
        raise HTTPException(404, "skill not found")
    # Only the DB row is removed; files on disk are left alone on purpose.
    await session.delete(row)
    await session.commit()
