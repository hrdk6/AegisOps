"""Event hub: fans out PostgreSQL NOTIFY events to SSE subscribers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from aegis.db.listen import Listener
from aegis.db.models import Event
from aegis.db.session import Database

log = logging.getLogger("aegis.api.events")


def serialize(e: Event) -> dict[str, Any]:
    return {"id": e.id, "incident_id": e.incident_id, "ts": e.ts.isoformat(), "type": e.type, "message": e.message,
            "actor": e.actor, "data": e.data}


class EventHub:
    def __init__(self, db: Database, dsn: str) -> None:
        self.db = db
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.listener = Listener(dsn, "aegis_events", self._on_notify)

    def start(self) -> None:
        self.listener.start()

    async def stop(self) -> None:
        await self.listener.stop()

    async def _on_notify(self, payload: str) -> None:
        event_id = json.loads(payload)["id"]
        async with self.db.session() as s:
            e = await s.get(Event, event_id)
        if e is None:
            return
        data = serialize(e)
        for q in list(self.subscribers):
            if q.qsize() > 500:
                continue  # slow consumer: drop (client resyncs via Last-Event-ID)
            q.put_nowait(data)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)

    async def backlog(self, after: int, limit: int = 200) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Event).where(Event.id > after).order_by(Event.id).limit(limit))).scalars()
            return [serialize(e) for e in rows]
