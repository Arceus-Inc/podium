"""Durable, tenant-owned entity lineage edges."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from podium.db import Base


class EntityPredicate(StrEnum):
    PRODUCED = "PRODUCED"
    EVALUATES = "EVALUATES"
    PARENT_OF = "PARENT_OF"
    DEPENDS_ON = "DEPENDS_ON"
    SERVES = "SERVES"
    DECIDED_BY = "DECIDED_BY"
    SUPPORTED_BY = "SUPPORTED_BY"
    LEARNED_FROM = "LEARNED_FROM"
    APPLIED = "APPLIED"
    SUPERSEDES = "SUPERSEDES"
    COST = "COST"


def _predicate_values(enum_class: type[EntityPredicate]) -> list[str]:
    return [member.value for member in enum_class]


class EntityEdge(Base):
    """One immutable relationship from a source entity to a destination entity."""

    __tablename__ = "entity_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    src_type: Mapped[str] = mapped_column(String)
    src_id: Mapped[str] = mapped_column(String)
    predicate: Mapped[EntityPredicate] = mapped_column(
        Enum(EntityPredicate, name="entity_edge_predicate", values_callable=_predicate_values)
    )
    dst_type: Mapped[str] = mapped_column(String)
    dst_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Chorus owns the `employee` table outside Base.metadata; migration 0014 owns this composite FK.
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "src_type <> '' AND src_id <> '' AND dst_type <> '' AND dst_id <> ''",
            name="nonempty_entities",
        ),
        ForeignKeyConstraint(
            ["company_id", "workspace_id"],
            ["companies.id", "companies.workspace_id"],
            name="fk_entity_edges_company_workspace",
        ),
        ForeignKeyConstraint(
            ["run_id", "company_id", "workspace_id"],
            ["runs.id", "runs.company_id", "runs.workspace_id"],
            name="fk_entity_edges_run_company_workspace",
        ),
        UniqueConstraint("company_id", "src_type", "src_id", "predicate", "dst_type", "dst_id"),
        Index(
            "ix_entity_edges_company_workspace_source_predicate",
            "company_id",
            "workspace_id",
            "src_type",
            "src_id",
            "predicate",
        ),
    )
