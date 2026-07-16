"""Events cursor-paging API. Authenticate → decide → tenant_session (RLS scopes to the caller)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.db import tenant_session
from podium.events.schemas import EventOut, EventPage, EventPageMeta
from podium.events.service import list_run_events
from podium.runs import get_run

router = APIRouter(prefix="/v1", tags=["events"])

_MAX_LIMIT = 500


@router.get("/runs/{run_id}/events", response_model=EventPage)
async def list_events(
    run_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> EventPage:
    if not decide(actor, "read", Resource(kind="run", workspace_id=actor.workspace_id)):
        raise HTTPException(status_code=403, detail="forbidden")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_run(session, run_id) is None:  # RLS hides another tenant's run → 404
            raise HTTPException(status_code=404, detail="run not found")
        rows = await list_run_events(session, run_id, after=after, limit=limit)
        events = [EventOut.model_validate(row) for row in rows]
    has_next = len(events) == limit
    next_after = events[-1].seq if events else None
    return EventPage(data=events, meta=EventPageMeta(next_after=next_after, has_next=has_next))
