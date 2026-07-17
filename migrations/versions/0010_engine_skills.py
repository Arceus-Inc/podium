"""engine skills — chorus's 0002_skills delta (skill + skill_revision) in the shared database

First use of the documented pattern from 0008: chorus ships engine schema changes as immutable
deltas in ``chorus.ledger.load_migrations()``; podium applies each as the schema owner (the
runtime role has no DDL), records the applied-set row so ``Ledger.open`` probes and skips, and
grants podium_app exactly the new tables. The SkillStore moves off its per-company SQLite file
because it is the one lattice-side store where the DB is the source of truth; episodic memory
stays a workdir-local SQLite file by design (see the M5 design doc).

Revision ID: 0010_engine_skills
Revises: 0009_company_owner
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from chorus.ledger import load_migrations

revision: str = "0010_engine_skills"
down_revision: str | None = "0009_company_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATION_ID = "0002_skills"
_NEW_TABLES = ("skill", "skill_revision")


def upgrade() -> None:
    migration = next(m for m in load_migrations() if m.id == _MIGRATION_ID)
    bind = op.get_bind()
    applied = bind.execute(
        sa.text("SELECT checksum FROM chorus_schema_migrations WHERE id = :id"),
        {"id": migration.id},
    ).scalar()
    if applied is None:
        for statement in migration.statements():
            op.execute(statement)
        op.execute(
            sa.text(
                "INSERT INTO chorus_schema_migrations (id, checksum, applied_at) "
                "VALUES (:id, :checksum, now())"
            ).bindparams(id=migration.id, checksum=migration.checksum)
        )
    # Exact-table grants, same rule as 0008: never a blanket schema grant.
    for table in _NEW_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO podium_app")


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute(
        sa.text("DELETE FROM chorus_schema_migrations WHERE id = :id").bindparams(
            id=_MIGRATION_ID
        )
    )
