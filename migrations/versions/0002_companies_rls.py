"""companies table + FORCE RLS tenancy walls (podium_app role)

Revision ID: 0002_companies_rls
Revises: 0001_workspaces
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_companies_rls"
down_revision: str | None = "0001_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The non-superuser runtime role. Migrations run as owner/superuser; the app connects as this so
# FORCE RLS actually applies (superusers bypass RLS entirely). Password is set out of band / via
# trust locally — CREATE ROLE here only guarantees the role and its grants exist.
_CREATE_ROLE = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'podium_app') THEN
    CREATE ROLE podium_app LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;
"""

# The GUC is text; the policy casts it to uuid so the comparison is uuid = uuid (index-usable,
# DB-validated). Unset GUC → NULL → NULL::uuid → predicate NULL → zero rows (fail closed).
_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"


def upgrade() -> None:
    op.execute(_CREATE_ROLE)

    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("ledger_backend", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_companies_workspace_id_workspaces")
        ),
        sa.UniqueConstraint("workspace_id", "slug", name=op.f("uq_companies_workspace_id")),
    )
    op.create_index("ix_companies_workspace_id", "companies", ["workspace_id"])

    # Grants: podium_app reads its own workspace row (RLS-scoped) and fully manages its companies.
    # No INSERT/DELETE on workspaces — creating the tenant root is control-plane (superuser) only.
    op.execute("GRANT USAGE ON SCHEMA public TO podium_app")
    op.execute("GRANT SELECT ON workspaces TO podium_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON companies TO podium_app")

    # workspaces: a session sees/edits only its own row.
    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspaces_tenant_isolation ON workspaces "
        f"USING (id = {_WORKSPACE_GUC_UUID})"
    )

    # companies: read and write are both scoped to the session's workspace (WITH CHECK blocks
    # writing a row into another tenant). No GUC set => predicate is NULL => zero rows (fail closed).
    op.execute("ALTER TABLE companies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE companies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY companies_tenant_isolation ON companies "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS companies_tenant_isolation ON companies")
    op.execute("DROP POLICY IF EXISTS workspaces_tenant_isolation ON workspaces")
    op.execute("ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_companies_workspace_id", table_name="companies")
    op.drop_table("companies")  # its RLS + grants go with it
    op.execute("REVOKE SELECT ON workspaces FROM podium_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM podium_app")
    op.execute("DROP ROLE IF EXISTS podium_app")
