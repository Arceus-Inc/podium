"""The `runs` table — a directive execution, and the work queue the conductor claims from.

Status vocab follows the paperclip/LangGraph convention. `owner` + `lease_expires_at` back the
compare-and-swap lifecycle: a queued run is claimed atomically, finalized only by its owner, and a
crashed owner's expired lease is reclaimable (crash recovery, not a retry loop).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"))
    directive: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_fingerprint: Mapped[str] = mapped_column(String)
    # Chorus-minted root-task id — engine context, text until the M5.2 engine port.
    engine_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default=RunStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Delegation widening (CP-3): {execution_mode, lead, goal_id, max_team_size,
    # spend_limit_cents} — durable so a rehydrated conductor re-reads them.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
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
        UniqueConstraint("workspace_id", "id"),  # composite child FKs keep workspace ownership hard
        Index("ix_runs_workspace_id", "workspace_id"),
        Index("ix_runs_company_id_status", "company_id", "status"),  # scheduler dispatch
    )


class RunSessionCheckpointRow(Base):
    """Append-only pointers to Dream-owned snapshots and traces for one product run."""

    __tablename__ = "run_session_checkpoints"

    checkpoint_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(nullable=False)
    snapshot_ref: Mapped[str] = mapped_column(String, nullable=False)
    working_dir: Mapped[str | None] = mapped_column(String, nullable=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usage_delta_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_delta_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_delta_cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_delta_cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_delta_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    usage_total_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_total_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_total_cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_total_cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    trace_ref: Mapped[str] = mapped_column(String, nullable=False)
    trace_event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
        UniqueConstraint(
            "run_id",
            "session_id",
            "saved_at",
            name="uq_run_session_checkpoints_saved_at",
        ),
        UniqueConstraint(
            "run_id",
            "session_id",
            "sequence_no",
            name="uq_run_session_checkpoints_sequence_no",
        ),
        Index(
            "ix_run_session_checkpoints_run_session_checkpoint_id",
            "run_id",
            "session_id",
            "checkpoint_id",
        ),
    )
