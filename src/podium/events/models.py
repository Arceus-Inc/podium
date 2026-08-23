"""The `events` table — the append-only per-company event log the dashboard replays and tails.

`seq` is a per-company monotonic cursor allocated in the database; `(company_id, seq)` is the primary
key and shared stream cursor. `raw` holds diagnostics and `product` holds normalized output.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base

EventPayload: TypeAlias = dict[str, JsonValue]


class EventChannel(StrEnum):
    RAW = "raw"
    PRODUCT = "product"


class Event(Base):
    __tablename__ = "events"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)  # per-company monotonic cursor
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("uuidv7()"), nullable=False
    )
    channel: Mapped[EventChannel] = mapped_column(String, default=EventChannel.RAW, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String)  # chorus EventKind value, e.g. 'run.text'
    # The engine lineage root (uuid text in chorus; the id runs.engine_task_id maps to a run).
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The beat's own task — a child in a delegation tree keeps its own lane id.
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Employee slug — the actor's lane key (OBS P2).
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[EventPayload] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("channel IN ('raw', 'product')", name="ck_events_channel"),
        ForeignKeyConstraint(
            ["company_id", "workspace_id"],
            ["companies.id", "companies.workspace_id"],
            name="fk_events_company_workspace",
        ),
        ForeignKeyConstraint(
            ["run_id", "company_id", "workspace_id"],
            ["runs.id", "runs.company_id", "runs.workspace_id"],
            name="fk_events_run_company_workspace",
        ),
        UniqueConstraint("company_id", "event_id", name="uq_events_company_id_event_id"),
        Index("ix_events_run_id_seq", "run_id", "seq"),
        Index("ix_events_company_channel_seq", "company_id", "channel", "seq"),
    )
