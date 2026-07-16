"""The `companies` table — an engine instance, scoped to a workspace. Config-only in M1a."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # cmp_<uuid4hex>, minted in service
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))  # tenant scope
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(
        String, default="provisioning"
    )  # provisioning|idle|running|stopped
    ledger_backend: Mapped[str] = mapped_column(String, default="sqlite")  # sqlite|postgres (M5)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),  # slug unique within a workspace
        Index("ix_companies_workspace_id", "workspace_id"),  # tenant-prefixed lookups
    )
