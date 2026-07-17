"""The load-bearing M0 test: the tenant GUC is set, injection-safe, and does not leak across txns."""

from __future__ import annotations

import uuid
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import tenant_session
from podium.workspaces import create_workspace


async def _current_ws(session: AsyncSession) -> str | None:
    row = await session.execute(text("SELECT current_setting('app.workspace_id', true)"))
    value = row.scalar_one()
    return value or None  # missing GUC comes back as '' with missing_ok=true


async def test_guc_is_set_inside_the_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = uuid4()
    async with tenant_session(sessionmaker, ws_id) as session:
        assert await _current_ws(session) == str(ws_id)  # canonical uuid text in the GUC


async def test_guc_does_not_leak_after_transaction(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_session(sessionmaker, uuid4()):
        pass
    # A fresh session (possibly the same pooled connection) must not see the previous tenant's GUC.
    async with sessionmaker() as session:
        assert await _current_ws(session) is None


async def test_workspace_id_is_bound_not_interpolated(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Defense-in-depth below the uuid type wall: deliberately smuggle a hostile *string* through
    # (the cast bypasses typing on purpose) to prove the set_config bind itself is injection-safe.
    hostile = cast(uuid.UUID, "ws'; DROP TABLE workspaces; --")
    async with tenant_session(sessionmaker, hostile) as session:
        assert await _current_ws(session) == str(hostile)
    async with sessionmaker() as session:
        count = (await session.execute(text("SELECT count(*) FROM workspaces"))).scalar_one()
        assert count == 0  # table still exists (a drop would raise), no injection


async def test_rolls_back_on_error(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(RuntimeError):
        async with tenant_session(sessionmaker, uuid4()) as session:
            await create_workspace(session, name="Boom", slug="boom")
            raise RuntimeError("boom")
    async with sessionmaker() as session:
        count = (await session.execute(text("SELECT count(*) FROM workspaces"))).scalar_one()
        assert count == 0  # the failed transaction wrote nothing
