"""runs.engine_task_id — the chorus root task, for event routing + rehydration

Revision ID: 0007_runs_engine_task_id
Revises: 0006_events
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_runs_engine_task_id"
down_revision: str | None = "0006_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("engine_task_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "engine_task_id")
