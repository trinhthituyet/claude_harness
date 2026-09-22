"""Event bus: ring buffer + fan-out + persistence.

Used by both task runs and chat sessions. Events are persisted before they are
published, so a browser reconnecting with ``Last-Event-ID: N`` can be served the
gap and never misses anything that happened while it was away.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.config import settings
from app.db import sessionmaker
from app.models import ChatMessage, RunEvent

Persist = Callable[[dict[str, Any]], Awaitable[None]]
Replay = Callable[[int], Awaitable[list[dict[str, Any]]]]

EOF_EVENT = {"seq": -1, "type": "_eof", "ts": None, "payload": {}}


class EventBus:
    def __init__(
        self,
        stream_id: str,
        *,
        persist: Persist,
        replay: Replay,
        buffer_size: int | None = None,
        start_seq: int = 0,
    ) -> None:
        self.stream_id = stream_id
        self._seq = start_seq
        self._persist_one = persist
        self._replay_stored = replay
        self._buffer: deque[dict[str, Any]] = deque(
            maxlen=buffer_size or settings.event_buffer_size
        )
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def seq(self) -> int:
        return self._seq

    async def emit(self, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "type": type_,
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            self._buffer.append(event)
        await self._persist_one(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass
        return event

    async def replay(self, after_seq: int) -> list[dict[str, Any]]:
        """Events after ``after_seq``, from the buffer when it still holds them."""
        buffered = [e for e in self._buffer if e["seq"] > after_seq]
        oldest = self._buffer[0]["seq"] if self._buffer else None
        if oldest is not None and after_seq + 1 >= oldest:
            return buffered
        return await self._replay_stored(after_seq)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def close(self) -> None:
        self._closed = True
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(dict(EOF_EVENT))
            except asyncio.QueueFull:  # pragma: no cover
                pass


def _row_to_event(row: Any) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "type": row.type,
        "ts": row.ts.isoformat() if row.ts else None,
        "payload": row.payload_json,
    }


def run_event_bus(run_id: str) -> EventBus:
    async def persist(event: dict[str, Any]) -> None:
        async with sessionmaker()() as session:
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=event["seq"],
                    type=event["type"],
                    payload_json=event["payload"],
                )
            )
            await session.commit()

    async def replay(after_seq: int) -> list[dict[str, Any]]:
        async with sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                    .order_by(RunEvent.seq)
                )
            ).scalars().all()
        return [_row_to_event(r) for r in rows]

    return EventBus(run_id, persist=persist, replay=replay)


def chat_event_bus(chat_id: str, start_seq: int = 0) -> EventBus:
    """The chat transcript is also its event log, so one table serves both."""

    async def persist(event: dict[str, Any]) -> None:
        async with sessionmaker()() as session:
            session.add(
                ChatMessage(
                    chat_id=chat_id,
                    seq=event["seq"],
                    type=event["type"],
                    payload_json=event["payload"],
                )
            )
            await session.commit()

    async def replay(after_seq: int) -> list[dict[str, Any]]:
        async with sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == chat_id, ChatMessage.seq > after_seq)
                    .order_by(ChatMessage.seq)
                )
            ).scalars().all()
        return [_row_to_event(r) for r in rows]

    return EventBus(chat_id, persist=persist, replay=replay, start_seq=start_seq)
