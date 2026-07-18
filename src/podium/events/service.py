"""Event persistence + cursor reads. Callers pass a `tenant_session`; RLS scopes every statement."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from podium.events.models import Event


async def max_company_seq(session: AsyncSession, company_id: uuid.UUID) -> int:
    """The highest seq written for a company (0 if none) — used to seed the mirror's counter."""
    stmt = select(func.coalesce(func.max(Event.seq), 0)).where(Event.company_id == company_id)
    return int((await session.execute(stmt)).scalar_one())


async def append_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    seq: int,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None,
    type: str,
    employee_id: str | None,
    payload: dict[str, Any],
    created_at: datetime,
    trace_id: uuid.UUID | None = None,
    task_id: str | None = None,
) -> Event:
    event = Event(
        company_id=company_id,
        seq=seq,
        workspace_id=workspace_id,
        run_id=run_id,
        type=type,
        trace_id=trace_id,
        task_id=task_id,
        employee_id=employee_id,
        payload=payload,
        created_at=created_at,
    )
    session.add(event)
    await session.flush()
    return event


async def list_run_events(
    session: AsyncSession, run_id: uuid.UUID, *, after: int, limit: int
) -> Sequence[Event]:
    """A run's events with seq > `after`, ordered, capped at `limit`. RLS scopes to the tenant."""
    stmt = (
        select(Event)
        .where(Event.run_id == run_id, Event.seq > after)
        .order_by(Event.seq)
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_company_events(
    session: AsyncSession, company_id: uuid.UUID, *, after: int, limit: int
) -> Sequence[Event]:
    """A company's events with seq > `after`, ordered — the SSE replay/tail read. RLS scopes it."""
    stmt = (
        select(Event)
        .where(Event.company_id == company_id, Event.seq > after)
        .order_by(Event.seq)
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()
