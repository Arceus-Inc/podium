"""Async engine + session factory. Pool sizing is asyncpg-via-SQLAlchemy; tune per shard later."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine:
    return create_async_engine(
        database_url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
