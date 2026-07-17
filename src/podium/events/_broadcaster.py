"""In-process fan-out of Postgres NOTIFY to per-client SSE queues.

One `Broadcaster` per api process owns a dedicated asyncpg connection that `LISTEN`s the global
`podium_events` channel (the mirror's transactional-outbox wake). Each SSE connection `subscribe`s and
drains its own bounded queue; a slow client that overflows is flagged (its stream ends → it
reconnects and replays from the durable table). Kept deliberately behind a small surface so a
`RedisBroadcaster` can drop in when multi-worker fan-out saturates Postgres NOTIFY.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import asyncpg
import structlog

_log = structlog.get_logger("podium.broadcaster")

EVENTS_CHANNEL = "podium_events"  # the mirror's outbox NOTIFY channel (imported by the mirror too)


@dataclass(frozen=True)
class Wake:
    """A NOTIFY parsed off the channel — a cursor + hint, not the event itself (the row has that)."""

    company_id: uuid.UUID
    seq: int
    run_id: uuid.UUID | None
    type: str


class Subscription:
    """One SSE client's inbox. `overflowed` means the client fell behind and should be dropped."""

    def __init__(self, company_id: uuid.UUID, maxsize: int) -> None:
        self.company_id = company_id
        self.overflowed = False
        self._queue: asyncio.Queue[Wake] = asyncio.Queue(maxsize=maxsize)

    def _offer(self, wake: Wake) -> None:
        try:
            self._queue.put_nowait(wake)
        except asyncio.QueueFull:
            self.overflowed = True  # never block the listener callback on one slow client

    async def get(self) -> Wake:
        return await self._queue.get()


class Broadcaster:
    def __init__(
        self, dsn: str, *, channel: str = EVENTS_CHANNEL, default_maxsize: int = 1000
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._default_maxsize = default_maxsize
        self._subs: dict[uuid.UUID, set[Subscription]] = defaultdict(set)
        self._conn: asyncpg.Connection | None = None

    @classmethod
    def from_url(cls, database_url: str, **kwargs: Any) -> Broadcaster:
        """Build from a SQLAlchemy async URL (`postgresql+asyncpg://…`) → an asyncpg DSN."""
        return cls(database_url.replace("+asyncpg", ""), **kwargs)

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(self._channel, self._on_notify)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def subscribe(self, company_id: uuid.UUID, *, maxsize: int | None = None) -> Subscription:
        sub = Subscription(company_id, maxsize or self._default_maxsize)
        self._subs[company_id].add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.company_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._subs[sub.company_id]

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
            raw_run_id = data.get("run_id")
            wake = Wake(
                company_id=uuid.UUID(data["company_id"]),
                seq=int(data["seq"]),
                run_id=uuid.UUID(raw_run_id) if raw_run_id is not None else None,
                type=data["type"],
            )
        except (ValueError, KeyError, TypeError):
            _log.warning("bad_notify_payload", payload=payload)
            return
        for sub in tuple(self._subs.get(wake.company_id, ())):
            sub._offer(wake)
