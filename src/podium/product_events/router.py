"""Canonical normalized product-event stream."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Resource, decide, get_sessionmaker
from podium.companies import get_company
from podium.db import tenant_session
from podium.events._broadcaster import Broadcaster
from podium.product_events._stream import product_event_stream, resolve_product_stream_actor

router = APIRouter(prefix="/v1", tags=["product-events"])


def _get_broadcaster(request: Request) -> Broadcaster:
    broadcaster: Broadcaster = request.app.state.broadcaster
    return broadcaster


@router.get("/workspaces/{workspace_id}/companies/{company_id}/stream")
async def stream(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    after: int = Query(0, ge=0),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    broadcaster: Broadcaster = Depends(_get_broadcaster),
) -> StreamingResponse:
    actor = await resolve_product_stream_actor(
        request,
        sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
    )
    if workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="company not found")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="company not found")
        if actor.company_id is not None and actor.company_id != company_id:
            raise HTTPException(status_code=404, detail="company not found")
        if not decide(
            actor,
            "read",
            Resource(kind="company", workspace_id=actor.workspace_id, company_id=company_id),
        ):
            raise HTTPException(status_code=403, detail="forbidden")

    header_id = request.headers.get("last-event-id")
    cursor = int(header_id) if header_id and header_id.isdigit() else after
    return StreamingResponse(
        product_event_stream(
            request,
            company_id=company_id,
            workspace_id=actor.workspace_id,
            cursor=cursor,
            sessionmaker=sessionmaker,
            broadcaster=broadcaster,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
