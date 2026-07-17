"""engine tables — chorus's Postgres-native ledger schema in the shared database (M5)

The engine state (tasks/wakes/employees/runs/goals/…) lives beside the product tables: one
database, two schema families. chorus OWNS this DDL — `baseline()` returns its Postgres-native
statements (uuid ids, timestamptz, jsonb, boolean, company_id + FORCE RLS by `app.company_id`);
podium's job here is orchestration: apply it as the schema owner, record the baseline so
`PostgresLedger.open` probes and skips DDL, and grant the runtime role exactly the engine tables.

Revision ID: 0008_engine_tables
Revises: 0007_runs_engine_task_id
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from chorus.ledger import baseline, ledger_table_names

revision: str = "0008_engine_tables"
down_revision: str | None = "0007_runs_engine_task_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS chorus_schema_migrations (
    id         text PRIMARY KEY,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL
)
"""


def upgrade() -> None:
    baseline_id, checksum, statements = baseline()
    for statement in statements:
        op.execute(statement)
    op.execute(_SCHEMA_MIGRATIONS_DDL)
    op.execute(
        sa.text(
            "INSERT INTO chorus_schema_migrations (id, checksum, applied_at) "
            "VALUES (:id, :checksum, now())"
        ).bindparams(id=baseline_id, checksum=checksum)
    )
    # Grant the runtime role exactly the engine tables — never a blanket schema grant that would
    # widen its privileges on the co-resident product tables. FORCE RLS (in chorus's DDL) scopes
    # every one of these to the session's company.
    for table in ledger_table_names():
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO podium_app")
    op.execute("GRANT SELECT ON chorus_schema_migrations TO podium_app")  # the open() probe


def downgrade() -> None:
    for table in reversed(ledger_table_names()):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TABLE IF EXISTS chorus_schema_migrations")
