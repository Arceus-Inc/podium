"""Alembic environment — async engine, URL from PODIUM_DATABASE_URL, naming conventions via Base."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

import podium.db.metadata  # noqa: F401  -- imports every domain's models onto Base.metadata
from podium.db import Base

config = context.config
if url := os.environ.get("PODIUM_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", url)

target_metadata = Base.metadata


def _sync_engine_deltas(connection) -> None:  # type: ignore[no-untyped-def]
    """Apply every pending chorus engine delta after podium's own migrations.

    chorus owns ALL engine DDL: the baseline lands via 0008; every later change ships as an
    immutable delta in ``chorus.ledger.load_migrations()``. This hook — not per-delta podium
    revisions — applies whatever is pending as the schema owner, records the applied-set row so
    ``Ledger.open`` probes and skips, and grants podium_app exactly the new tables. It reruns on
    every ``alembic upgrade``, so a new chorus delta needs ZERO podium code.
    """
    from chorus.ledger import load_migrations
    from sqlalchemy import text

    if connection.execute(text("SELECT to_regclass('chorus_schema_migrations')")).scalar() is None:
        return  # pre-0008 database (e.g. partial upgrade) — the baseline isn't in yet
    applied = {
        row[0]
        for row in connection.execute(text("SELECT id FROM chorus_schema_migrations")).fetchall()
    }
    for migration in load_migrations():
        if migration.id in applied:
            continue
        for statement in migration.statements():
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO chorus_schema_migrations (id, checksum, applied_at) "
                "VALUES (:id, :checksum, now())"
            ),
            {"id": migration.id, "checksum": migration.checksum},
        )
        for table in migration.table_names():
            connection.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO podium_app")
            )


def _run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
        _sync_engine_deltas(connection)


async def _run_async() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), poolclass=NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async())
