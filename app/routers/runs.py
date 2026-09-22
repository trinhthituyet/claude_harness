"""Runs: history, live SSE stream, approvals, cancel."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import PermissionDecision, RunEvent, TaskRun
from app.schemas import ApprovalIn, DecisionOut, EventOut, RunOut
from app.security.gate import ApprovalAnswer
from app.services.runner import manager

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Keeps intermediaries and browsers from treating an idle stream as dead.
HEARTBEAT_SECONDS = 15


@router.get("", response_model=list[RunOut])
async def list_runs(
    task_id: int | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(TaskRun).order_by(TaskRun.started_at.desc()).limit(min(limit, 200))
    if task_id is not None:
        stmt = stmt.where(TaskRun.task_id == task_id)
    return (await session.execute(stmt)).scalars().all()


@router.get("/pending-approvals")
async def pending_approvals():
    """Approvals waiting on an answer, across every live run."""
    return manager.pending_approvals()


@router.get("/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await session.get(TaskRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    events = (
        await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )
    ).scalars().all()
    decisions = (
        await session.execute(
            select(PermissionDecision)
            .where(PermissionDecision.run_id == run_id)
            .order_by(PermissionDecision.id)
        )
    ).scalars().all()
    live = manager.get(run_id)
    return {
        "run": RunOut.model_validate(run).model_dump(),
        "events": [
            EventOut(seq=e.seq, type=e.type, ts=e.ts, payload=e.payload_json).model_dump()
            for e in events
        ],
        "decisions": [
            DecisionOut(
                ts=d.ts,
                layer=d.layer,
                tool_name=d.tool_name,
                decision=d.decision,
                reason=d.reason,
                resolved_paths=d.resolved_paths_json or [],
            ).model_dump()
            for d in decisions
        ],
        "live": live is not None,
        "pending_approvals": live.pending_approvals() if live else [],
        "team": run.team_snapshot_json,
        "model": run.model_snapshot_json,
        "options": run.options_json,
    }


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Live event stream. Honours Last-Event-ID so a reload resumes without gaps."""
    header = request.headers.get("last-event-id")
    after = int(header) if header and header.isdigit() else last_event_id

    run = await session.get(TaskRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")

    live = manager.get(run_id)

    async def finished_stream() -> AsyncIterator[str]:
        rows = (
            await session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after)
                .order_by(RunEvent.seq)
            )
        ).scalars().all()
        for row in rows:
            yield _sse(
                {
                    "seq": row.seq,
                    "type": row.type,
                    "ts": row.ts.isoformat() if row.ts else None,
                    "payload": row.payload_json,
                }
            )
        yield "event: _eof\ndata: {}\n\n"

    if live is None:
        return StreamingResponse(finished_stream(), media_type="text/event-stream")

    bus = live.bus

    async def live_stream() -> AsyncIterator[str]:
        queue = bus.subscribe()
        try:
            for event in await bus.replay(after):
                yield _sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event.get("type") == "_eof":
                    yield "event: _eof\ndata: {}\n\n"
                    return
                yield _sse(event)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        live_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/approvals/{request_id}")
async def answer_approval(run_id: str, request_id: str, payload: ApprovalIn):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "run is not live")
    ok = run.answer_approval(
        request_id,
        ApprovalAnswer(
            approved=payload.approved, remember=payload.remember, reason=payload.reason
        ),
    )
    if not ok:
        raise HTTPException(409, "that approval is unknown or already answered")
    return {"ok": True}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str):
    if not await manager.cancel(run_id):
        raise HTTPException(404, "run is not live")
    return {"ok": True}
