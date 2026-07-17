"""companies.owner_user_id — a company belongs to a user (M5 §2.5)

Ownership is an authorization filter WITHIN the workspace (owner-or-service-key), not a second
RLS axis: the workspace stays the hard tenancy wall; NULL owner = workspace-owned (created by a
service key), visible to every member.

Revision ID: 0009_company_owner
Revises: 0008_engine_tables
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_company_owner"
down_revision: str | None = "0008_engine_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("owner_user_id", postgresql.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_companies_owner_user_id_users"), "companies", "users", ["owner_user_id"], ["id"]
    )
    op.create_index("ix_companies_owner_user_id", "companies", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_companies_owner_user_id", table_name="companies")
    op.drop_constraint(op.f("fk_companies_owner_user_id_users"), "companies", type_="foreignkey")
    op.drop_column("companies", "owner_user_id")
