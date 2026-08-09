"""Frozen HTTP representations for the product timeline."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from podium.timeline.models import TimelineType


class TimelineItemView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: uuid.UUID
    source_event_seq: int
    category: str
    event_type: TimelineType
    subject_type: str
    subject_id: str
    attention: bool
    attention_state: str | None
    title: str
    summary: str | None
    actor_type: str | None
    actor_id: str | None
    run_id: uuid.UUID | None
    occurred_at: datetime
    projected_at: datetime


class TimelineLinks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    self: str
    next: str | None


class TimelinePageMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    has_more: bool
    next_cursor: str | None


class TimelinePageEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: tuple[TimelineItemView, ...]
    meta: TimelinePageMeta
    links: TimelineLinks


class TimelineDetailEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: TimelineItemView
    links: TimelineLinks
