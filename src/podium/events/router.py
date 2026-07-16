"""Events cursor-paging API. Authenticate → decide → tenant_session (RLS scopes to the caller)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.db import tenant_session
from podium.events._broadcaster import Broadcaster
from podium.events._stream import event_stream, resolve_stream_actor
from podium.events.schemas import EventOut, EventPage, EventPageMeta
from podium.events.service import list_run_events
from podium.runs import get_run

router = APIRouter(prefix="/v1", tags=["events"])

_MAX_LIMIT = 500


def _get_broadcaster(request: Request) -> Broadcaster:
    broadcaster: Broadcaster = request.app.state.broadcaster
    return broadcaster


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
        # Fetch one extra to know if a next page exists without a false positive on a full-but-final page.
        rows = await list_run_events(session, run_id, after=after, limit=limit + 1)
    has_next = len(rows) > limit
    events = [EventOut.model_validate(row) for row in rows[:limit]]
    next_after = events[-1].seq if events else None
    return EventPage(data=events, meta=EventPageMeta(next_after=next_after, has_next=has_next))


@router.get("/companies/{company_id}/stream")
async def stream(
    company_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    broadcaster: Broadcaster = Depends(_get_broadcaster),
) -> StreamingResponse:
    """Live per-company SSE. Resume with `Last-Event-ID: <seq>` (or `?after=`); replays then tails."""
    actor = await resolve_stream_actor(request, sessionmaker)
    if not decide(
        actor,
        "read",
        Resource(kind="company", workspace_id=actor.workspace_id, company_id=company_id),
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id) is None:  # RLS hides a foreign company → 404
            raise HTTPException(status_code=404, detail="company not found")

    header_id = request.headers.get("last-event-id")
    cursor = int(header_id) if header_id and header_id.isdigit() else after

    return StreamingResponse(
        event_stream(
            request,
            company_id=company_id,
            workspace_id=actor.workspace_id,
            cursor=cursor,
            sessionmaker=sessionmaker,
            broadcaster=broadcaster,
        ),
        media_type="text/event-stream",
    )
