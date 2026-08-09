"""Small SQLAlchemy primitives used by the timeline projector."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from podium.events.models import Event
from podium.timeline.models import ProjectionCursor, TimelineItem
from podium.timeline.service_types import TimelineItemDraft


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
        .values(last_event_seq=next_seq, projector_version=projector_version, projected_at=func.now())
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
