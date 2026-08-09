"""Event persistence + cursor reads. Callers pass a `tenant_session`; RLS scopes every statement."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import ConfigDict, TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from podium.events._broadcaster import EVENTS_CHANNEL
from podium.events.models import Event, EventChannel, EventPayload
from podium.product_events import ProductEvent, ProductEventDraft

_json_payload = TypeAdapter(EventPayload, config=ConfigDict(allow_inf_nan=False))


async def max_company_seq(session: AsyncSession, company_id: uuid.UUID) -> int:
    """The highest durable sequence for a company (0 if none)."""
    stmt = select(func.coalesce(func.max(Event.seq), 0)).where(Event.company_id == company_id)
    return int((await session.execute(stmt)).scalar_one())


async def allocate_company_seq(session: AsyncSession, company_id: uuid.UUID) -> int:
    """Reserve this transaction's next company sequence without consuming it on rollback."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:company_id AS text), 0))"),
        {"company_id": str(company_id)},
    )
    return await max_company_seq(session, company_id) + 1


async def append_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None,
    type: str,
    employee_id: str | None,
    payload: EventPayload,
    created_at: datetime,
    trace_id: uuid.UUID | None = None,
    task_id: str | None = None,
    channel: EventChannel = EventChannel.RAW,
    seq: int | None = None,
) -> Event:
    """Append one event and notify tailers in the caller's transaction.

    ``seq`` is accepted only as a legacy assertion for test fixtures; it never assigns the value.
    """
    validated_payload = _json_payload.validate_python(payload)
    allocated_seq = await allocate_company_seq(session, company_id)
    if seq is not None and seq != allocated_seq:
        raise ValueError(f"expected event sequence {seq}, allocated {allocated_seq}")
    event = Event(
        company_id=company_id,
        seq=allocated_seq,
        workspace_id=workspace_id,
        run_id=run_id,
        type=type,
        trace_id=trace_id,
        task_id=task_id,
        employee_id=employee_id,
        payload=validated_payload,
        created_at=created_at,
        channel=channel,
    )
    session.add(event)
    await session.flush()
    await _notify(session, event)
    return event


async def append_product_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    draft: ProductEventDraft,
) -> ProductEvent:
    """Persist a validated normalized event, returning the database-assigned envelope."""
    event = await append_event(
        session,
        company_id=company_id,
        workspace_id=workspace_id,
        run_id=None,
        type=draft.type,
        employee_id=None,
        payload=draft.storage_payload(),
        created_at=draft.occurred_at,
        channel=EventChannel.PRODUCT,
    )
    return _product_event(event)


async def list_run_events(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    after: int,
    limit: int,
    channel: EventChannel | None = EventChannel.RAW,
) -> Sequence[Event]:
    """A run's events with seq > `after`, ordered, capped at `limit`. RLS scopes to the tenant."""
    conditions = [Event.run_id == run_id, Event.seq > after]
    if channel is not None:
        conditions.append(Event.channel == channel)
    stmt = select(Event).where(*conditions).order_by(Event.seq).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def list_company_events(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    after: int,
    limit: int,
    channel: EventChannel | None = EventChannel.RAW,
) -> Sequence[Event]:
    """A company's events with seq > `after`, ordered — the SSE replay/tail read. RLS scopes it."""
    conditions = [Event.company_id == company_id, Event.seq > after]
    if channel is not None:
        conditions.append(Event.channel == channel)
    stmt = select(Event).where(*conditions).order_by(Event.seq).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def list_product_events(
    session: AsyncSession, company_id: uuid.UUID, *, after: int, limit: int
) -> Sequence[ProductEvent]:
    """List normalized envelopes by the same company cursor as raw diagnostics."""
    events = await list_company_events(
        session,
        company_id,
        after=after,
        limit=limit,
        channel=EventChannel.PRODUCT,
    )
    return tuple(_product_event(event) for event in events)


def _product_event(event: Event) -> ProductEvent:
    draft = ProductEventDraft.model_validate(event.payload)
    return ProductEvent(
        event_id=str(event.event_id),
        seq=event.seq,
        type=draft.type,
        company_id=str(event.company_id),
        occurred_at=draft.occurred_at,
        actor=draft.actor,
        subject=draft.subject,
        causation_id=draft.causation_id,
        correlation_id=draft.correlation_id,
        changed_fields=draft.changed_fields,
        data=draft.data,
    )


async def _notify(session: AsyncSession, event: Event) -> None:
    body = {
        "company_id": str(event.company_id),
        "seq": event.seq,
        "run_id": str(event.run_id) if event.run_id is not None else None,
        "type": event.type,
    }
    await session.execute(
        text("SELECT pg_notify(:channel, :body)"),
        {"channel": EVENTS_CHANNEL, "body": json.dumps(body)},
    )
