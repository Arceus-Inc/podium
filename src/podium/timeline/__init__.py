"""Timeline projection persistence API."""

from __future__ import annotations

from podium.timeline.mapping import (
    TIMELINE_RULES,
    ExclusionRule,
    TimelinePayloadError,
    map_timeline_event,
)
from podium.timeline.models import ProjectionCursor, TimelineItem, TimelineType
from podium.timeline.service import (
    OccurredAtMustBeUTC,
    ProjectionCursorMismatch,
    ProjectionVersionMismatch,
    SourceEventNotFound,
    project_timeline_event,
)
from podium.timeline.service_types import (
    ProjectionResult,
    ProjectionStatus,
    TimelineExclusion,
    TimelineItemDraft,
)

__all__ = [
    "TIMELINE_RULES",
    "ExclusionRule",
    "OccurredAtMustBeUTC",
    "ProjectionCursor",
    "ProjectionCursorMismatch",
    "ProjectionResult",
    "ProjectionStatus",
    "ProjectionVersionMismatch",
    "SourceEventNotFound",
    "TimelineExclusion",
    "TimelineItem",
    "TimelineItemDraft",
    "TimelinePayloadError",
    "TimelineType",
    "map_timeline_event",
    "project_timeline_event",
]
