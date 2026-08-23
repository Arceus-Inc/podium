"""append-only Dream session checkpoint pointers per product run

Revision ID: 0012_run_session_checkpoints
Revises: 0011_runs_params
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_run_session_checkpoints"
down_revision: str | None = "0011_runs_params"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def upgrade() -> None:
    op.create_unique_constraint(op.f("uq_runs_workspace_id"), "runs", ["workspace_id", "id"])
    op.create_table(
        "run_session_checkpoints",
        sa.Column("checkpoint_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_ref", sa.String(), nullable=False),
        sa.Column("working_dir", sa.String(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usage_delta_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_delta_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_delta_cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_delta_cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_delta_cost_usd", sa.Float(), nullable=False),
        sa.Column("usage_total_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_total_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_total_cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_total_cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_total_cost_usd", sa.Float(), nullable=False),
        sa.Column("trace_ref", sa.String(), nullable=False),
        sa.Column("trace_event_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("checkpoint_id", name=op.f("pk_run_session_checkpoints")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_run_session_checkpoints_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name=op.f("fk_run_session_checkpoints_workspace_id_runs"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "session_id",
            "saved_at",
            name="uq_run_session_checkpoints_saved_at",
        ),
        sa.UniqueConstraint(
            "run_id",
            "session_id",
            "sequence_no",
            name="uq_run_session_checkpoints_sequence_no",
        ),
    )
    op.create_index(
        "ix_run_session_checkpoints_run_session_checkpoint_id",
        "run_session_checkpoints",
        ["run_id", "session_id", "checkpoint_id"],
    )
    op.execute("GRANT SELECT, INSERT ON run_session_checkpoints TO podium_app")
    op.execute("ALTER TABLE run_session_checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_session_checkpoints FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY run_session_checkpoints_read ON run_session_checkpoints FOR SELECT "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID})"
    )
    op.execute(
        "CREATE POLICY run_session_checkpoints_append ON run_session_checkpoints FOR INSERT "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS run_session_checkpoints_append ON run_session_checkpoints")
    op.execute("DROP POLICY IF EXISTS run_session_checkpoints_read ON run_session_checkpoints")
    op.drop_index(
        "ix_run_session_checkpoints_run_session_checkpoint_id",
        table_name="run_session_checkpoints",
    )
    op.drop_table("run_session_checkpoints")
    op.drop_constraint(op.f("uq_runs_workspace_id"), "runs", type_="unique")
