"""MCP servers: CRUD plus suggestions and a connectivity check."""

from __future__ import annotations

import asyncio
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import McpServer
from app.schemas import McpIn, McpOut
from app.services import catalog

router = APIRouter(prefix="/api/mcps", tags=["mcps"])


def _apply(row: McpServer, payload: McpIn) -> None:
    row.name = payload.name
    row.description = payload.description
    row.transport = payload.transport
    row.command = payload.command
    row.args_json = list(payload.args)
    row.env_json = dict(payload.env)
    row.url = payload.url
    row.headers_json = dict(payload.headers)
    row.enabled = payload.enabled
    row.trusted = payload.trusted


@router.get("", response_model=list[McpOut])
async def list_mcps(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    return [McpOut.of(r) for r in rows]


@router.get("/suggested")
async def suggested(session: AsyncSession = Depends(get_session)):
    names = {
        r.name for r in (await session.execute(select(McpServer))).scalars().all()
    }
    return [s for s in catalog.SUGGESTED_MCPS if s["name"] not in names]


@router.post("", response_model=McpOut, status_code=201)
async def create_mcp(payload: McpIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (
        await session.execute(select(McpServer).where(McpServer.name == payload.name))
    ).scalars().first():
        raise HTTPException(409, f"an MCP server named {payload.name!r} already exists")
    row = McpServer(name=payload.name)
    _apply(row, payload)
    session.add(row)
    await session.commit()
    return McpOut.of(row)


@router.put("/{server_id}", response_model=McpOut)
async def update_mcp(
    server_id: int, payload: McpIn, session: AsyncSession = Depends(get_session)
):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = await session.get(McpServer, server_id)
    if row is None:
        raise HTTPException(404, "MCP server not found")
    # An empty env value means "keep the stored secret".
    merged = {**(row.env_json or {}), **{k: v for k, v in payload.env.items() if v}}
    _apply(row, payload)
    row.env_json = merged
    await session.commit()
    return McpOut.of(row)


@router.delete("/{server_id}", status_code=204)
async def delete_mcp(server_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(McpServer, server_id)
    if row is None:
        raise HTTPException(404, "MCP server not found")
    await session.delete(row)
    await session.commit()


@router.post("/{server_id}/test")
async def test_mcp(server_id: int, session: AsyncSession = Depends(get_session)):
    """A cheap reachability check: is the command on PATH / does the URL respond?"""
    row = await session.get(McpServer, server_id)
    if row is None:
        raise HTTPException(404, "MCP server not found")

    if row.transport == "stdio":
        if not row.command:
            return {"ok": False, "detail": "no command configured"}
        resolved = shutil.which(row.command)
        if resolved is None:
            return {"ok": False, "detail": f"{row.command!r} is not on PATH"}
        return {"ok": True, "detail": f"found {resolved}"}

    if not row.url:
        return {"ok": False, "detail": "no url configured"}
    try:
        import urllib.request

        def probe() -> int:
            request = urllib.request.Request(row.url, method="HEAD")
            for key, value in (row.headers_json or {}).items():
                request.add_header(key, value)
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status

        status = await asyncio.wait_for(asyncio.to_thread(probe), timeout=8)
        return {"ok": status < 500, "detail": f"HTTP {status}"}
    except Exception as exc:  # noqa: BLE001 - the message is the useful part
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
