"""The `workspaces` table — the tenant root every future product table hangs off."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    __tablename__ = "workspaces"

    # DB-minted uuidv7 (PG18): time-ordered, globally unique. Fetched via INSERT..RETURNING on flush.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    slug: Mapped[str] = mapped_column(String, unique=True)  # URL-safe handle
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
