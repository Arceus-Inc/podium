"""The tenant-scoped session — M0's load-bearing primitive.

`tenant_session` is the ONE way tenant-scoped DB work happens: it opens a transaction and pins
`app.workspace_id` for that transaction only (`set_config(..., is_local => true)`), so RLS policies
(M1) and the conductor's DB sessions read the same GUC. The value is bound as a parameter — never
interpolated — because `SET LOCAL` cannot take a bind param but `set_config()` can.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WORKSPACE_GUC = "app.workspace_id"


@asynccontextmanager
async def tenant_session(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction with `app.workspace_id` pinned for that transaction.

    Commits on clean exit, rolls back on exception. Because the GUC is set with is_local=true it is
    gone when the transaction ends — safe to reuse the connection from the pool with no leak.
    """
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT set_config(:key, :val, true)"),
            {"key": WORKSPACE_GUC, "val": workspace_id},
        )
        yield session
