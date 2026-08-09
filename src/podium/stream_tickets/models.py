"""Single-use SSE stream tickets: opaque secrets scoped to one workspace/company/actor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class StreamTicket(Base):
    __tablename__ = "stream_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ticket_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        CheckConstraint("actor_type IN ('service', 'user')", name="ck_stream_tickets_actor_type"),
        CheckConstraint(
            "char_length(ticket_hash) = 64", name="ck_stream_tickets_ticket_hash_length"
        ),
        CheckConstraint(
            "expires_at = created_at + interval '60 seconds'",
            name="ck_stream_tickets_exact_ttl",
        ),
        ForeignKeyConstraint(
            ["company_id", "workspace_id"],
            ["companies.id", "companies.workspace_id"],
            name="fk_stream_tickets_company_id_workspace_id_companies",
        ),
        UniqueConstraint("ticket_hash", name="uq_stream_tickets_ticket_hash"),
        UniqueConstraint(
            "id",
            "company_id",
            "workspace_id",
            name="uq_stream_tickets_id_company_id_workspace_id",
        ),
        Index("ix_stream_tickets_workspace_id", "workspace_id"),
        Index(
            "ix_stream_tickets_workspace_company_expires_at",
            "workspace_id",
            "company_id",
            "expires_at",
        ),
    )
