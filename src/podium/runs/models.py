"""The `runs` table — a directive execution, and the work queue the conductor claims from.

Status vocab follows the paperclip/LangGraph convention. `owner` + `lease_expires_at` back the
compare-and-swap lifecycle: a queued run is claimed atomically, finalized only by its owner, and a
crashed owner's expired lease is reclaimable (crash recovery, not a retry loop).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.TIMED_OUT}
)


def _now() -> datetime:
    return datetime.now(UTC)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # run_<uuid4hex>
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    directive: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=RunStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    log_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # blob pointer, filled in M3
    owner: Mapped[str | None] = mapped_column(String, nullable=True)  # conductor worker holding it
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key"),  # idempotent create per company
        Index("ix_runs_workspace_id", "workspace_id"),
        Index("ix_runs_company_id_status", "company_id", "status"),  # scheduler dispatch
    )
