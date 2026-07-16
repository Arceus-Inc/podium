"""events table (per-company seq, RLS) + runs.engine_task_id

Revision ID: 0006_events
Revises: 0005_runs_commands
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_events"
down_revision: str | None = "0005_runs_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("company_id", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("employee_id", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("company_id", "seq", name=op.f("pk_events")),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_events_company_id_companies")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_events_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_events_run_id_runs")),
    )
    op.create_index("ix_events_run_id_seq", "events", ["run_id", "seq"])

    op.execute("GRANT SELECT, INSERT ON events TO podium_app")  # append-only: no UPDATE/DELETE
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY events_tenant_isolation ON events "
        "USING (workspace_id = current_setting('app.workspace_id', true)) "
        "WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS events_tenant_isolation ON events")
    op.drop_index("ix_events_run_id_seq", table_name="events")
    op.drop_table("events")
