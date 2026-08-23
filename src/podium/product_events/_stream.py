"""Canonical normalized product-event SSE stream."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, resolve_actor
from podium.db import tenant_session
from podium.events._broadcaster import Broadcaster
from podium.events.service import list_product_events
from podium.product_events import ProductEvent
from podium.stream_tickets import redeem_stream_ticket

_CHUNK = 500
_KEEPALIVE_SECONDS = 15.0


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


async def resolve_product_stream_actor(
    request: Request,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Actor:
    bearer_token = _bearer_token(request)
    ticket = request.query_params.get("ticket")
    access_token = request.query_params.get("access_token")
    if access_token is not None or (bearer_token is None) == (ticket is None):
        raise HTTPException(status_code=401, detail="unauthorized")
    if bearer_token is not None:
        async with sessionmaker() as session:
            actor = await resolve_actor(session, bearer_token)
        if actor is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return actor
    assert ticket is not None
    async with tenant_session(sessionmaker, workspace_id) as session:
        redeemed = await redeem_stream_ticket(
            session,
            ticket=ticket,
            workspace_id=workspace_id,
            company_id=company_id,
        )
    if redeemed is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return redeemed.actor


def _frame(event: ProductEvent) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _events_after(
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    after: int,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[ProductEvent]:
    cursor = after
    while True:
        async with tenant_session(sessionmaker, workspace_id) as session:
            rows = await list_product_events(session, company_id, after=cursor, limit=_CHUNK)
        for row in rows:
            cursor = row.seq
            yield row
        if len(rows) < _CHUNK:
            return


async def product_event_stream(
    request: Request,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    cursor: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    broadcaster: Broadcaster,
) -> AsyncIterator[str]:
    sub = broadcaster.subscribe(company_id)
    last = cursor
    try:
        async for row in _events_after(company_id, workspace_id, last, sessionmaker):
            last = row.seq
            yield _frame(row)
        while True:
            if await request.is_disconnected():
                return
            try:
                await asyncio.wait_for(sub.get(), timeout=_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if sub.overflowed:
                return
            async for row in _events_after(company_id, workspace_id, last, sessionmaker):
                last = row.seq
                yield _frame(row)
    finally:
        broadcaster.unsubscribe(sub)
