"""users table (RLS) + api_keys.user_id link

Revision ID: 0004_users
Revises: 0003_api_keys
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_users"
down_revision: str | None = "0003_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_users_workspace_id_workspaces")
        ),
        sa.UniqueConstraint("workspace_id", "email", name=op.f("uq_users_workspace_id")),
    )
    op.create_index("ix_users_workspace_id", "users", ["workspace_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON users TO podium_app")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_tenant_isolation ON users "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )

    # Link a key to a user (nullable — service keys have no user).
    op.add_column("api_keys", sa.Column("user_id", postgresql.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_api_keys_user_id_users"), "api_keys", "users", ["user_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_api_keys_user_id_users"), "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "user_id")
    op.execute("DROP POLICY IF EXISTS users_tenant_isolation ON users")
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON users FROM podium_app")
    op.drop_index("ix_users_workspace_id", table_name="users")
    op.drop_table("users")
