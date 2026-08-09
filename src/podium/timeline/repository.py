"""Small SQLAlchemy primitives used by the timeline projector."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from podium.events.models import Event
from podium.timeline.models import ProjectionCursor, TimelineItem
from podium.timeline.service_types import TimelineItemDraft


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """A bounded keyset page from the durable timeline projection."""

    items: tuple[TimelineItem, ...]
    has_more: bool
    as_of_seq: int


@dataclass(frozen=True, slots=True)
class TimelineCursor:
    """The durable projection position used for timeline pagination."""

    source_event_seq: int


async def source_event_exists(
    session: AsyncSession, *, company_id: uuid.UUID, workspace_id: uuid.UUID, seq: int
) -> bool:
    stmt = select(Event.seq).where(
        Event.company_id == company_id,
        Event.workspace_id == workspace_id,
        Event.seq == seq,
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def create_initial_cursor(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    projector: str,
    projector_version: str,
) -> None:
    stmt = (
        insert(ProjectionCursor)
        .values(
            company_id=company_id,
            workspace_id=workspace_id,
            projector=projector,
            last_event_seq=0,
            projector_version=projector_version,
        )
        .on_conflict_do_nothing(index_elements=["company_id", "projector"])
    )
    await session.execute(stmt)


async def advance_cursor(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    projector: str,
    expected_prior_seq: int,
    next_seq: int,
    projector_version: str,
) -> bool:
    stmt = (
        update(ProjectionCursor)
        .where(
            ProjectionCursor.company_id == company_id,
            ProjectionCursor.projector == projector,
            ProjectionCursor.last_event_seq == expected_prior_seq,
        )
        .values(
            last_event_seq=next_seq, projector_version=projector_version, projected_at=func.now()
        )
        .returning(ProjectionCursor.company_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def get_cursor(
    session: AsyncSession, *, company_id: uuid.UUID, projector: str
) -> ProjectionCursor | None:
    stmt = select(ProjectionCursor).where(
        ProjectionCursor.company_id == company_id, ProjectionCursor.projector == projector
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_item(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    item: TimelineItemDraft,
) -> TimelineItem:
    row = TimelineItem(
        company_id=company_id,
        source_event_seq=item.source_event_seq,
        workspace_id=workspace_id,
        category=item.category,
        event_type=item.event_type,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        attention=item.attention,
        attention_state=item.attention_state,
        title=item.title,
        summary=item.summary,
        actor_type=item.actor_type,
        actor_id=item.actor_id,
        run_id=item.run_id,
        occurred_at=item.occurred_at,
    )
    session.add(row)
    await session.flush()
    return row


async def list_items(
    session: AsyncSession, *, company_id: uuid.UUID, limit: int
) -> list[TimelineItem]:
    stmt = (
        select(TimelineItem)
        .where(TimelineItem.company_id == company_id)
        .order_by(TimelineItem.occurred_at, TimelineItem.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def clear_projection(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    projector: str,
) -> None:
    """Remove one company's materialized timeline so it can be rebuilt from durable events."""
    await session.execute(
        delete(TimelineItem).where(
            TimelineItem.company_id == company_id,
            TimelineItem.workspace_id == workspace_id,
        )
    )
    await session.execute(
        delete(ProjectionCursor).where(
            ProjectionCursor.company_id == company_id,
            ProjectionCursor.workspace_id == workspace_id,
            ProjectionCursor.projector == projector,
        )
    )


async def get_item(
    session: AsyncSession, *, company_id: uuid.UUID, item_id: uuid.UUID
) -> TimelineItem | None:
    stmt = select(TimelineItem).where(
        TimelineItem.company_id == company_id,
        TimelineItem.id == item_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def page_items(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    limit: int,
    cursor: TimelineCursor | None = None,
    category: str | None = None,
    attention: bool | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> TimelinePage:
    """Read one newest-first page without consulting the source-event store."""
    filters = [TimelineItem.company_id == company_id]
    if category is not None:
        filters.append(TimelineItem.category == category)
    if attention is not None:
        filters.append(TimelineItem.attention.is_(attention))
    if actor_type is not None:
        filters.append(TimelineItem.actor_type == actor_type)
    if actor_id is not None:
        filters.append(TimelineItem.actor_id == actor_id)
    if subject_type is not None:
        filters.append(TimelineItem.subject_type == subject_type)
    if subject_id is not None:
        filters.append(TimelineItem.subject_id == subject_id)
    if occurred_after is not None:
        filters.append(TimelineItem.occurred_at >= occurred_after)
    if occurred_before is not None:
        filters.append(TimelineItem.occurred_at <= occurred_before)
    if cursor is not None:
        filters.append(TimelineItem.source_event_seq < cursor.source_event_seq)
    as_of_stmt = select(func.coalesce(func.max(TimelineItem.source_event_seq), 0)).where(
        TimelineItem.company_id == company_id
    )
    as_of_seq = int((await session.execute(as_of_stmt)).scalar_one())
    stmt = (
        select(TimelineItem)
        .where(*filters)
        .order_by(
            desc(TimelineItem.source_event_seq),
        )
        .limit(limit + 1)
    )
    rows = tuple((await session.execute(stmt)).scalars())
    return TimelinePage(items=rows[:limit], has_more=len(rows) > limit, as_of_seq=as_of_seq)
