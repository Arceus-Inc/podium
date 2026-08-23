"""Relational storage for the product timeline projection."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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


class TimelineType(StrEnum):
    DIRECTION_DECISION_PROPOSED = "direction.decision_proposed"
    DIRECTION_DECISION_APPROVED = "direction.decision_approved"
    DIRECTION_DECISION_REVISED = "direction.decision_revised"
    DIRECTION_GOAL_ADDED = "direction.goal_added"
    DIRECTION_GOAL_HEALTH_CHANGED = "direction.goal_health_changed"
    DIRECTION_PRIORITY_CHANGED = "direction.priority_changed"
    DIRECTION_GOAL_PAUSED = "direction.goal_paused"
    DIRECTION_GOAL_RESUMED = "direction.goal_resumed"
    WORK_TASK_CREATED = "work.task_created"
    WORK_TASK_DELEGATED = "work.task_delegated"
    WORK_TASK_BLOCKED = "work.task_blocked"
    WORK_TASK_UNBLOCKED = "work.task_unblocked"
    WORK_TASK_UNDER_REVIEW = "work.task_under_review"
    WORK_TASK_VERIFIED = "work.task_verified"
    WORK_TASK_REJECTED = "work.task_rejected"
    WORK_RECOVERY_OPENED = "work.recovery_opened"
    WORK_ROUTINE_FIRED = "work.routine_fired"
    DELIVERABLE_PRODUCED = "deliverable.produced"
    DELIVERABLE_VERIFIED = "deliverable.verified"
    DELIVERABLE_PUBLISHED = "deliverable.published"
    DELIVERABLE_REJECTED = "deliverable.rejected"
    PEOPLE_HIRED = "people.hired"
    PEOPLE_PAUSED = "people.paused"
    PEOPLE_RESUMED = "people.resumed"
    PEOPLE_TERMINATED = "people.terminated"
    PEOPLE_BUDGET_CHANGED = "people.budget_changed"
    LEARNING_OPENED = "learning.opened"
    LEARNING_APPLIED = "learning.applied"
    LEARNING_ROLLED_BACK = "learning.rolled_back"
    COST_THRESHOLD_REACHED = "cost.threshold_reached"
    COST_HARD_STOP = "cost.hard_stop"
    SYSTEM_STALLED = "system.stalled"
    SYSTEM_RECOVERED = "system.recovered"

    @property
    def category(self) -> str:
        return self.value.partition(".")[0]


def _timeline_type_values(enum_class: type[TimelineType]) -> list[str]:
    return [member.value for member in enum_class]


class ProjectionCursor(Base):
    """The last event durably handled by one company/projector pair."""

    __tablename__ = "projection_cursors"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), primary_key=True
    )
    projector: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    last_event_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    projector_version: Mapped[str] = mapped_column(String)
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("last_event_seq >= 0", name="last_event_seq_nonnegative"),
        Index("ix_projection_cursors_workspace_id", "workspace_id"),
    )


class TimelineItem(Base):
    """One product-facing interpretation of a durable source event."""

    __tablename__ = "timeline_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"))
    source_event_seq: Mapped[int] = mapped_column(BigInteger)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    category: Mapped[str] = mapped_column(String)
    event_type: Mapped[TimelineType] = mapped_column(
        Enum(TimelineType, name="timeline_type", values_callable=_timeline_type_values)
    )
    subject_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[str] = mapped_column(String)
    attention: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attention_state: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_type: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "category IN ('direction', 'work', 'deliverable', 'people', 'learning', 'cost', 'system')",
            name="category_v1",
        ),
        CheckConstraint("event_type::text LIKE category || '.%'", name="type_matches_category"),
        ForeignKeyConstraint(
            ["company_id", "source_event_seq"],
            ["events.company_id", "events.seq"],
            name="fk_timeline_items_source_event",
        ),
        UniqueConstraint("company_id", "source_event_seq"),
        Index("ix_timeline_items_workspace_id", "workspace_id"),
        Index("ix_timeline_items_company_id_occurred_at", "company_id", "occurred_at"),
        Index("ix_timeline_items_company_id_category", "company_id", "category"),
        Index("ix_timeline_items_company_id_attention", "company_id", "attention"),
    )
