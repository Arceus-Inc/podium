"""Immutable, tenant-owned entity lineage edges.

Revision ID: 0014_entity_edges
Revises: 0013_timeline_projection
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_entity_edges"
down_revision: str | None = "0013_timeline_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_GUC_UUID = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"
_PREDICATES = (
    "PRODUCED",
    "EVALUATES",
    "PARENT_OF",
    "DEPENDS_ON",
    "SERVES",
    "DECIDED_BY",
    "SUPPORTED_BY",
    "LEARNED_FROM",
    "APPLIED",
    "SUPERSEDES",
    "COST",
)


def upgrade() -> None:
    # These candidate keys make ownership a database-enforced property of every edge reference.
    op.create_unique_constraint("uq_companies_id_workspace_id", "companies", ["id", "workspace_id"])
    op.create_unique_constraint(
        "uq_runs_id_company_id_workspace_id", "runs", ["id", "company_id", "workspace_id"]
    )

    predicate = postgresql.ENUM(*_PREDICATES, name="entity_edge_predicate", create_type=False)
    predicate.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "entity_edges",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(), nullable=False),
        sa.Column("company_id", postgresql.UUID(), nullable=False),
        sa.Column("src_type", sa.String(), nullable=False),
        sa.Column("src_id", sa.String(), nullable=False),
        sa.Column("predicate", predicate, nullable=False),
        sa.Column("dst_type", sa.String(), nullable=False),
        sa.Column("dst_id", sa.String(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("employee_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "src_type <> '' AND src_id <> '' AND dst_type <> '' AND dst_id <> ''",
            name=op.f("ck_entity_edges_nonempty_entities"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "workspace_id"],
            ["companies.id", "companies.workspace_id"],
            name="fk_entity_edges_company_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "company_id", "workspace_id"],
            ["runs.id", "runs.company_id", "runs.workspace_id"],
            name="fk_entity_edges_run_company_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "employee_id"],
            ["employee.company_id", "employee.id"],
            name="fk_entity_edges_employee_company",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_edges")),
        sa.UniqueConstraint(
            "company_id",
            "src_type",
            "src_id",
            "predicate",
            "dst_type",
            "dst_id",
            name=op.f("uq_entity_edges_company_id"),
        ),
    )
    op.create_index(
        "ix_entity_edges_company_workspace_source_predicate",
        "entity_edges",
        ["company_id", "workspace_id", "src_type", "src_id", "predicate"],
    )
    op.execute("GRANT SELECT, INSERT ON entity_edges TO podium_app")
    op.execute("ALTER TABLE entity_edges ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entity_edges FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY entity_edges_tenant_isolation ON entity_edges "
        f"USING (workspace_id = {_WORKSPACE_GUC_UUID}) "
        f"WITH CHECK (workspace_id = {_WORKSPACE_GUC_UUID})"
    )
    op.execute(
        """
        CREATE FUNCTION entity_edges_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'entity_edges is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER entity_edges_immutable "
        "BEFORE UPDATE OR DELETE ON entity_edges "
        "FOR EACH ROW EXECUTE FUNCTION entity_edges_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entity_edges_immutable ON entity_edges")
    op.execute("DROP FUNCTION IF EXISTS entity_edges_reject_mutation()")
    op.execute("DROP POLICY IF EXISTS entity_edges_tenant_isolation ON entity_edges")
    op.drop_index("ix_entity_edges_company_workspace_source_predicate", table_name="entity_edges")
    op.drop_table("entity_edges")
    postgresql.ENUM(name="entity_edge_predicate", create_type=False).drop(
        op.get_bind(), checkfirst=False
    )
    op.drop_constraint("uq_runs_id_company_id_workspace_id", "runs", type_="unique")
    op.drop_constraint("uq_companies_id_workspace_id", "companies", type_="unique")
