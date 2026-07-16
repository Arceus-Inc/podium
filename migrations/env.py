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


def _run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


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
