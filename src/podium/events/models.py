"""The `events` table — the append-only per-company event log the dashboard replays and tails.

`seq` is a per-company monotonic cursor assigned by the single-writer EventMirror; `(company_id, seq)`
is the primary key and the stream/paging cursor. `run_id` is NULL for company-level events.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


class Event(Base):
    __tablename__ = "events"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True)  # per-company monotonic cursor
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(String)  # chorus EventKind value, e.g. 'run.text'
    # Chorus-minted employee id — engine context, text until the M5.2 engine port.
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_events_run_id_seq", "run_id", "seq"),)  # run-scoped paging
