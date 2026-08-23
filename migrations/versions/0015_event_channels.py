"""Durable raw and normalized product event channels.

Revision ID: 0015_event_channels
Revises: 0014_entity_edges
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_event_channels"
down_revision: str | None = "0014_entity_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("channel", sa.String(), server_default=sa.text("'raw'"), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("event_id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=True),
    )
    op.execute("UPDATE events SET channel = 'raw' WHERE channel IS NULL")
    op.execute("UPDATE events SET event_id = uuidv7() WHERE event_id IS NULL")
    op.alter_column("events", "channel", nullable=False)
    op.alter_column("events", "event_id", nullable=False)
    op.create_check_constraint("ck_events_channel", "events", "channel IN ('raw', 'product')")
    op.create_unique_constraint("uq_events_company_id_event_id", "events", ["company_id", "event_id"])

    op.drop_constraint(op.f("fk_events_company_id_companies"), "events", type_="foreignkey")
    op.drop_constraint(op.f("fk_events_workspace_id_workspaces"), "events", type_="foreignkey")
    op.drop_constraint(op.f("fk_events_run_id_runs"), "events", type_="foreignkey")
    op.create_foreign_key(
        "fk_events_company_workspace",
        "events",
        "companies",
        ["company_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_events_run_company_workspace",
        "events",
        "runs",
        ["run_id", "company_id", "workspace_id"],
        ["id", "company_id", "workspace_id"],
    )
    op.create_index("ix_events_company_channel_seq", "events", ["company_id", "channel", "seq"])

    op.execute("GRANT SELECT, INSERT ON events TO podium_app")
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS events_tenant_isolation ON events")
    op.execute(
        "CREATE POLICY events_tenant_isolation ON events "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS events_tenant_isolation ON events")
    op.drop_index("ix_events_company_channel_seq", table_name="events")
    op.drop_constraint("fk_events_run_company_workspace", "events", type_="foreignkey")
    op.drop_constraint("fk_events_company_workspace", "events", type_="foreignkey")
    op.create_foreign_key(op.f("fk_events_run_id_runs"), "events", "runs", ["run_id"], ["id"])
    op.create_foreign_key(
        op.f("fk_events_workspace_id_workspaces"), "events", "workspaces", ["workspace_id"], ["id"]
    )
    op.create_foreign_key(op.f("fk_events_company_id_companies"), "events", "companies", ["company_id"], ["id"])
    op.drop_constraint("uq_events_company_id_event_id", "events", type_="unique")
    op.drop_constraint("ck_events_channel", "events", type_="check")
    op.drop_column("events", "event_id")
    op.drop_column("events", "channel")
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY events_tenant_isolation ON events "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )
