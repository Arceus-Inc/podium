"""Atomic, compare-and-swap advancement of the timeline projection."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from podium.timeline import repository
from podium.timeline.models import TimelineItem
from podium.timeline.service_types import (
    ProjectionResult,
    ProjectionStatus,
    TimelineExclusion,
    TimelineItemDraft,
)


class ProjectionCursorMismatch(ValueError):
    """The projector was called with a stale, skipped, or out-of-order cursor."""


class SourceEventNotFound(ValueError):
    """The requested durable event is absent or outside the current tenant."""


class ProjectionVersionMismatch(ValueError):
    """A different projector build owns the existing durable cursor."""


class OccurredAtMustBeUTC(ValueError):
    """Timeline timestamps must be timezone-aware UTC datetimes."""


async def project_timeline_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    workspace_id: uuid.UUID,
    projector: str,
    projector_version: str,
    expected_prior_seq: int,
    item: TimelineItemDraft | None = None,
    exclusion: TimelineExclusion | None = None,
) -> ProjectionResult:
    """Project or explicitly exclude exactly the next durable event in one savepoint."""
    if (item is None) == (exclusion is None):
        raise ValueError("provide exactly one timeline item or exclusion")
    if item is not None:
        source_event_seq = item.source_event_seq
        if item.category != item.event_type.category:
            raise ValueError("timeline category must match the event type prefix")
        if item.occurred_at.tzinfo is None or item.occurred_at.utcoffset() != timedelta(0):
            raise OccurredAtMustBeUTC("occurred_at must be timezone-aware UTC")
    else:
        assert exclusion is not None
        source_event_seq = exclusion.source_event_seq
    if source_event_seq != expected_prior_seq + 1:
        raise ProjectionCursorMismatch("source event must immediately follow the expected cursor")

    async with session.begin_nested():
        cursor = await repository.get_cursor(session, company_id=company_id, projector=projector)
        if cursor is not None and cursor.projector_version != projector_version:
            raise ProjectionVersionMismatch("projector version differs from the durable cursor")
        if not await repository.source_event_exists(
            session, company_id=company_id, workspace_id=workspace_id, seq=source_event_seq
        ):
            raise SourceEventNotFound("source event is not visible in this tenant")
        if expected_prior_seq == 0:
            await repository.create_initial_cursor(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector=projector,
                projector_version=projector_version,
            )
        if await repository.advance_cursor(
            session,
            company_id=company_id,
            projector=projector,
            expected_prior_seq=expected_prior_seq,
            next_seq=source_event_seq,
            projector_version=projector_version,
        ):
            inserted: TimelineItem | None = None
            if item is not None:
                inserted = await repository.insert_item(
                    session, company_id=company_id, workspace_id=workspace_id, item=item
                )
            return ProjectionResult(ProjectionStatus.APPLIED, inserted)

        cursor = await repository.get_cursor(session, company_id=company_id, projector=projector)
        if cursor is not None and cursor.projector_version != projector_version:
            raise ProjectionVersionMismatch("projector version differs from the durable cursor")
        if cursor is not None and cursor.last_event_seq >= source_event_seq:
            return ProjectionResult(ProjectionStatus.REPLAYED, None)
        raise ProjectionCursorMismatch("cursor does not match the expected prior event")
