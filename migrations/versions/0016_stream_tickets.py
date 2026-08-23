"""single-use stream tickets (RLS)

Revision ID: 0016_stream_tickets
Revises: 0015_event_channels
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_stream_tickets"
down_revision: str | None = "0015_event_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def upgrade() -> None:
    op.create_table(
        "stream_tickets",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(), nullable=False),
        sa.Column("ticket_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('service', 'user')", name="ck_stream_tickets_actor_type"
        ),
        sa.CheckConstraint(
            "char_length(ticket_hash) = 64", name="ck_stream_tickets_ticket_hash_length"
        ),
        sa.CheckConstraint(
            "expires_at = created_at + interval '60 seconds'",
            name="ck_stream_tickets_exact_ttl",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_stream_tickets_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "workspace_id"],
            ["companies.id", "companies.workspace_id"],
            name="fk_stream_tickets_company_id_workspace_id_companies",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stream_tickets")),
        sa.UniqueConstraint("ticket_hash", name="uq_stream_tickets_ticket_hash"),
        sa.UniqueConstraint(
            "id",
            "company_id",
            "workspace_id",
            name="uq_stream_tickets_id_company_id_workspace_id",
        ),
    )
    op.create_index("ix_stream_tickets_workspace_id", "stream_tickets", ["workspace_id"])
    op.create_index(
        "ix_stream_tickets_workspace_company_expires_at",
        "stream_tickets",
        ["workspace_id", "company_id", "expires_at"],
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON stream_tickets TO podium_app")
    op.execute("ALTER TABLE stream_tickets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stream_tickets FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY stream_tickets_tenant_isolation ON stream_tickets "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS stream_tickets_tenant_isolation ON stream_tickets")
    op.drop_index("ix_stream_tickets_workspace_company_expires_at", table_name="stream_tickets")
    op.drop_index("ix_stream_tickets_workspace_id", table_name="stream_tickets")
    op.drop_table("stream_tickets")
