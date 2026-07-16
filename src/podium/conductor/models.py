"""The `commands` table — the api→conductor mailbox for out-of-band signals (cancel/stop/pause).

Run *starts* are the queued `runs` row itself; commands carry control signals against in-flight work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Command(Base):
    __tablename__ = "commands"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # cmd_<uuid4hex>
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    type: Mapped[str] = mapped_column(String)  # cancel | stop | pause
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_commands_company_id", "company_id"),  # pending-per-company scan
    )
