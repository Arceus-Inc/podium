"""workspaces table — the tenant root

Revision ID: 0001_workspaces
Revises:
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_workspaces"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        # uuidv7(): PG18-native, time-ordered — B-tree-friendly, globally unique, DB-minted.
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )


def downgrade() -> None:
    op.drop_table("workspaces")
