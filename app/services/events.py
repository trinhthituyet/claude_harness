"""Per-run event bus: ring buffer + fan-out + persistence.

Events are persisted before they are published, so a browser reconnecting with
``Last-Event-ID: N`` can be served the gap from the buffer (or the table) and never
misses anything that happened while it was away.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import sessionmaker
from app.models import RunEvent


class RunEventBus:
    def __init__(self, run_id: str, buffer_size: int | None = None) -> None:
        self.run_id = run_id
        self._seq = 0
        self._buffer: deque[dict[str, Any]] = deque(
            maxlen=buffer_size or settings.event_buffer_size
        )
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

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
        await self._persist(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass
        return event

    async def _persist(self, event: dict[str, Any]) -> None:
        async with sessionmaker()() as session:
            session.add(
                RunEvent(
                    run_id=self.run_id,
                    seq=event["seq"],
                    type=event["type"],
                    payload_json=event["payload"],
                )
            )
            await session.commit()

    async def replay(self, after_seq: int) -> list[dict[str, Any]]:
        """Events after ``after_seq``, from the buffer when possible."""
        buffered = [e for e in self._buffer if e["seq"] > after_seq]
        oldest = self._buffer[0]["seq"] if self._buffer else None
        if oldest is not None and after_seq + 1 >= oldest:
            return buffered
        async with sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == self.run_id, RunEvent.seq > after_seq)
                    .order_by(RunEvent.seq)
                )
            ).scalars().all()
        return [
            {
                "seq": r.seq,
                "type": r.type,
                "ts": r.ts.isoformat() if r.ts else None,
                "payload": r.payload_json,
            }
            for r in rows
        ]

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
                queue.put_nowait({"seq": -1, "type": "_eof", "ts": None, "payload": {}})
            except asyncio.QueueFull:  # pragma: no cover
                pass
