"""The SSE generator + stream auth. Kept out of the router so the handler stays a thin wire-up.

Reconnect-safe by construction: subscribe to the broadcaster FIRST, then replay `seq > cursor` from
the durable table (chunked), then tail — emitting only `seq > last`, so nothing is missed in the
replay→tail seam and nothing is duplicated (seq is per-company monotonic + single-writer).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, resolve_actor
from podium.db import tenant_session
from podium.events._broadcaster import Broadcaster
from podium.events.models import Event
from podium.events.schemas import EventOut
from podium.events.service import list_company_events

_CHUNK = 500
_KEEPALIVE_SECONDS = 15.0


async def resolve_stream_actor(
    request: Request, sessionmaker: async_sessionmaker[AsyncSession]
) -> Actor:
    """Auth for raw diagnostic SSE: bearer header only, never a query credential."""
    if request.query_params.get("access_token") is not None or request.query_params.get(
        "ticket"
    ) is not None:
        raise HTTPException(status_code=401, detail="unauthorized")
    header = request.headers.get("authorization")
    token: str | None = None
    if header and header.lower().startswith("bearer "):
        token = header[7:].strip() or None
    async with sessionmaker() as session:
        actor = await resolve_actor(session, token)
    if actor is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return actor


def _frame(event: Event) -> str:
    data = EventOut.model_validate(event).model_dump_json()
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


async def _events_after(
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    after: int,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Event]:
    """Every event past `after`, chunked so a huge run never buffers wholesale. RLS scopes it."""
    cursor = after
    while True:
        async with tenant_session(sessionmaker, workspace_id) as session:
            rows = await list_company_events(session, company_id, after=cursor, limit=_CHUNK)
        for row in rows:
            cursor = row.seq
            yield row
        if len(rows) < _CHUNK:
            return


async def event_stream(
    request: Request,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    cursor: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    broadcaster: Broadcaster,
) -> AsyncIterator[str]:
    sub = broadcaster.subscribe(company_id)  # buffer live BEFORE reading history (closes the seam)
    last = cursor
    try:
        async for row in _events_after(company_id, workspace_id, last, sessionmaker):  # replay
            last = row.seq
            yield _frame(row)
        while True:  # tail
            if await request.is_disconnected():
                return
            try:
                await asyncio.wait_for(sub.get(), timeout=_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"  # keepalive so proxies don't reap the idle stream
                continue
            if sub.overflowed:  # slow client — end the stream; it reconnects and replays
                return
            async for row in _events_after(company_id, workspace_id, last, sessionmaker):
                last = row.seq
                yield _frame(row)
    finally:
        broadcaster.unsubscribe(sub)
