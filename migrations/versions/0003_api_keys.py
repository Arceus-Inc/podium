"""api_keys auth-bootstrap table (no RLS)

Revision ID: 0003_api_keys
Revises: 0002_companies_rls
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_api_keys"
down_revision: str | None = "0002_companies_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("company_id", sa.String(), nullable=True),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_api_keys_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_api_keys_company_id_companies")
        ),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])
    # No RLS: keys are resolved by their unguessable hash before tenant context exists. The app role
    # only READS them (lookup) — issuing keys is a control-plane (superuser) operation, so the runtime
    # role has no write surface on this table.
    op.execute("GRANT SELECT ON api_keys TO podium_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON api_keys FROM podium_app")
    op.drop_index("ix_api_keys_workspace_id", table_name="api_keys")
    op.drop_table("api_keys")
