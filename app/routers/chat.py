"""Chat: sessions, messages, live stream, confirmations."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ChatMessage, ChatSession
from app.routers.runs import HEARTBEAT_SECONDS, _sse
from app.schemas import ChatConfirmIn, ChatMessageIn, ChatOut, ChatStartIn
from app.services.chat import ConfirmAnswer, manager

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("", response_model=list[ChatOut])
async def list_chats(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(ChatSession).order_by(ChatSession.updated_at.desc()))
    ).scalars().all()
    return rows


@router.get("/pending-confirmations")
async def pending_confirmations():
    """Changes waiting on a yes/no, across every open chat."""
    return manager.pending_confirmations()


@router.post("", status_code=201)
async def start_chat(payload: ChatStartIn):
    chat_id = await manager.create(payload.title or "New chat")
    if payload.message:
        chat = await manager.attach(chat_id)
        await chat.send(payload.message)
    return {"chat_id": chat_id}


@router.get("/{chat_id}")
async def get_chat(chat_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(ChatSession, chat_id)
    if row is None:
        raise HTTPException(404, "chat not found")
    messages = (
        await session.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.seq)
        )
    ).scalars().all()
    live = manager.get(chat_id)
    return {
        "chat": ChatOut.model_validate(row).model_dump(),
        "messages": [
            {
                "seq": m.seq,
                "type": m.type,
                "ts": m.ts.isoformat() if m.ts else None,
                "payload": m.payload_json,
            }
            for m in messages
        ],
        "pending_confirmations": live.pending_confirmations() if live else [],
        "busy": bool(live and live.busy),
    }


@router.post("/{chat_id}/messages", status_code=202)
async def send_message(chat_id: str, payload: ChatMessageIn):
    if not payload.text.strip():
        raise HTTPException(422, "a message needs some text")
    try:
        chat = await manager.attach(chat_id)
    except KeyError as exc:
        raise HTTPException(404, "chat not found") from exc
    try:
        await chat.send(payload.text)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@router.post("/{chat_id}/confirmations/{request_id}")
async def confirm(chat_id: str, request_id: str, payload: ChatConfirmIn):
    chat = manager.get(chat_id)
    if chat is None:
        raise HTTPException(404, "chat is not open")
    ok = chat.confirm(
        request_id, ConfirmAnswer(approved=payload.approved, reason=payload.reason)
    )
    if not ok:
        raise HTTPException(409, "that confirmation is unknown or already answered")
    return {"ok": True}


@router.get("/{chat_id}/events")
async def stream(
    chat_id: str,
    request: Request,
    last_event_id: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Live transcript. Honours Last-Event-ID so a reload resumes without gaps."""
    header = request.headers.get("last-event-id")
    after = int(header) if header and header.isdigit() else last_event_id

    if await session.get(ChatSession, chat_id) is None:
        raise HTTPException(404, "chat not found")
    chat = manager.get(chat_id)

    if chat is None:
        async def stored() -> AsyncIterator[str]:
            rows = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == chat_id, ChatMessage.seq > after)
                    .order_by(ChatMessage.seq)
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

        return StreamingResponse(stored(), media_type="text/event-stream")

    bus = chat.bus

    async def live() -> AsyncIterator[str]:
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
        live(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(chat_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(ChatSession, chat_id)
    if row is None:
        raise HTTPException(404, "chat not found")
    await manager.close(chat_id)
    await session.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
    await session.delete(row)
    await session.commit()
