"""runs.params — delegation widening on the one run resource (CP-3, M4 §3.3)

One run resource; ``execution_mode`` discriminates. Delegation parameters (lead, goal, team
size, spend limit) are stored durably on the run so the conductor threads them into
``org.submit`` and a rehydrated conductor re-reads them — never re-derived from the directive.

Revision ID: 0011_runs_params
Revises: 0010_events_trace
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_runs_params"
down_revision: str | None = "0010_events_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("runs", "params")
