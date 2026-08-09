"""runs.request_fingerprint — collision-safe idempotent run creation

Revision ID: 0012_runs_request_fingerprint
Revises: 0011_runs_params
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_runs_request_fingerprint"
down_revision: str | None = "0011_runs_params"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("request_fingerprint", sa.String(), nullable=False, server_default=""),
    )
    op.alter_column("runs", "request_fingerprint", server_default=None)


def downgrade() -> None:
    op.drop_column("runs", "request_fingerprint")
