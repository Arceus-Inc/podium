"""The `users` table — workspace-scoped members. Tenant-isolated by RLS like companies."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # usr_<uuid4hex>
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    email: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("workspace_id", "email"),  # email unique within a workspace
        Index("ix_users_workspace_id", "workspace_id"),
    )
