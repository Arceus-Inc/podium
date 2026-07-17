"""runs + commands tables (RLS) — the conductor work queue and mailbox

Revision ID: 0005_runs_commands
Revises: 0004_users
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_runs_commands"
down_revision: str | None = "0004_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = ("runs", "commands")
_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("directive", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column("log_ref", sa.String(), nullable=True),
        sa.Column("owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_runs_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_runs_company_id_companies")
        ),
        sa.UniqueConstraint("company_id", "idempotency_key", name=op.f("uq_runs_company_id")),
    )
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"])
    op.create_index("ix_runs_company_id_status", "runs", ["company_id", "status"])

    op.create_table(
        "commands",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commands")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_commands_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_commands_company_id_companies")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_commands_run_id_runs")),
    )
    op.create_index("ix_commands_company_id", "commands", ["company_id"])

    for table in _TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO podium_app")
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("ix_commands_company_id", table_name="commands")
    op.drop_table("commands")
    op.drop_index("ix_runs_company_id_status", table_name="runs")
    op.drop_index("ix_runs_workspace_id", table_name="runs")
    op.drop_table("runs")
