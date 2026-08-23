"""Typed inputs and outcomes for one timeline projection step."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from podium.timeline.models import TimelineItem, TimelineType


@dataclass(frozen=True, slots=True)
class TimelineItemDraft:
    source_event_seq: int
    category: str
    event_type: TimelineType
    subject_type: str
    subject_id: str
    title: str
    occurred_at: datetime
    attention: bool = False
    attention_state: str | None = None
    summary: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    run_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.category != self.event_type.category:
            raise ValueError("timeline category must match the event type prefix")


@dataclass(frozen=True, slots=True)
class TimelineExclusion:
    source_event_seq: int


class ProjectionStatus(StrEnum):
    APPLIED = "applied"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    status: ProjectionStatus
    item: TimelineItem | None
