"""events trace envelope — trace_id + task_id columns (CP-1, OBS P2)

Every event names its actor and its work unit: `trace_id` is the engine lineage root (the id
`runs.engine_task_id` maps to a run), `task_id` is the beat's own task — a child task's events
stay lane-separable without re-deriving lineage. Both nullable: pre-CP-1 rows and company-level
events (no task) simply carry NULL.

Revision ID: 0010_events_trace
Revises: 0009_company_owner
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_events_trace"
down_revision: str | None = "0009_company_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("trace_id", sa.Uuid(), nullable=True))
    op.add_column("events", sa.Column("task_id", sa.String(), nullable=True))
    # Trace-scoped paging: one trace's events in seq order (the run-trace view's query).
    op.create_index("ix_events_trace_seq", "events", ["company_id", "trace_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_events_trace_seq", table_name="events")
    op.drop_column("events", "task_id")
    op.drop_column("events", "trace_id")
