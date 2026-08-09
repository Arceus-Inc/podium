"""timeline projection persistence (RLS)

Revision ID: 0013_timeline_projection
Revises: 0012_runs_request_fingerprint
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_timeline_projection"
down_revision: str | None = "0012_runs_request_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = ("projection_cursors", "timeline_items")
_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"
_TIMELINE_TYPES = (
    "direction.decision_proposed",
    "direction.decision_approved",
    "direction.decision_revised",
    "direction.goal_added",
    "direction.goal_health_changed",
    "direction.priority_changed",
    "direction.goal_paused",
    "direction.goal_resumed",
    "work.task_created",
    "work.task_delegated",
    "work.task_blocked",
    "work.task_unblocked",
    "work.task_under_review",
    "work.task_verified",
    "work.task_rejected",
    "work.recovery_opened",
    "work.routine_fired",
    "deliverable.produced",
    "deliverable.verified",
    "deliverable.published",
    "deliverable.rejected",
    "people.hired",
    "people.paused",
    "people.resumed",
    "people.terminated",
    "people.budget_changed",
    "learning.opened",
    "learning.applied",
    "learning.rolled_back",
    "cost.threshold_reached",
    "cost.hard_stop",
    "system.stalled",
    "system.recovered",
)
_TIMELINE_CATEGORIES = "'direction', 'work', 'deliverable', 'people', 'learning', 'cost', 'system'"


def _enable_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO podium_app")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def upgrade() -> None:
    timeline_type = postgresql.ENUM(*_TIMELINE_TYPES, name="timeline_type", create_type=False)
    timeline_type.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "projection_cursors",
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("projector", sa.String(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("last_event_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("projector_version", sa.String(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("last_event_seq >= 0", name=op.f("ck_projection_cursors_last_event_seq_nonnegative")),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_projection_cursors_company_id_companies")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_projection_cursors_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("company_id", "projector", name=op.f("pk_projection_cursors")),
    )
    op.create_index("ix_projection_cursors_workspace_id", "projection_cursors", ["workspace_id"])

    op.create_table(
        "timeline_items",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("source_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("event_type", timeline_type, nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("attention", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("attention_state", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("actor_type", sa.String(), nullable=True),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"category IN ({_TIMELINE_CATEGORIES})",
            name=op.f("ck_timeline_items_category_v1"),
        ),
        sa.CheckConstraint(
            "event_type::text LIKE category || '.%'",
            name=op.f("ck_timeline_items_type_matches_category"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_timeline_items_company_id_companies")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_timeline_items_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_timeline_items_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["company_id", "source_event_seq"],
            ["events.company_id", "events.seq"],
            name="fk_timeline_items_source_event",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timeline_items")),
        sa.UniqueConstraint(
            "company_id", "source_event_seq", name=op.f("uq_timeline_items_company_id")
        ),
    )
    op.create_index("ix_timeline_items_workspace_id", "timeline_items", ["workspace_id"])
    op.create_index(
        "ix_timeline_items_company_id_occurred_at", "timeline_items", ["company_id", "occurred_at"]
    )
    op.create_index(
        "ix_timeline_items_company_id_category", "timeline_items", ["company_id", "category"]
    )
    op.create_index(
        "ix_timeline_items_company_id_attention", "timeline_items", ["company_id", "attention"]
    )

    for table in _TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("ix_timeline_items_company_id_attention", table_name="timeline_items")
    op.drop_index("ix_timeline_items_company_id_category", table_name="timeline_items")
    op.drop_index("ix_timeline_items_company_id_occurred_at", table_name="timeline_items")
    op.drop_index("ix_timeline_items_workspace_id", table_name="timeline_items")
    op.drop_table("timeline_items")
    postgresql.ENUM(name="timeline_type", create_type=False).drop(op.get_bind(), checkfirst=False)
    op.drop_index("ix_projection_cursors_workspace_id", table_name="projection_cursors")
    op.drop_table("projection_cursors")
