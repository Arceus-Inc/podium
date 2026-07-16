"""The minimal workspace create path lands a row with a minted id and enforces slug uniqueness."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import tenant_session
from podium.workspaces import create_workspace


async def test_create_workspace_persists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_session(sessionmaker, "ws_bootstrap") as session:
        ws = await create_workspace(session, name="Acme", slug="acme")
        assert ws.id.startswith("ws_")
    async with sessionmaker() as session:
        row = (await session.execute(text("SELECT name FROM workspaces WHERE slug = 'acme'"))).one()
        assert row.name == "Acme"


async def test_duplicate_slug_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_session(sessionmaker, "ws_bootstrap") as session:
        await create_workspace(session, name="Acme", slug="acme")
    with pytest.raises(IntegrityError):
        async with tenant_session(sessionmaker, "ws_bootstrap") as session:
            await create_workspace(session, name="Acme Two", slug="acme")
