"""The minimal workspace create path lands a row with a DB-minted uuid and enforces slug uniqueness."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.workspaces import create_workspace


async def test_create_workspace_persists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        ws = await create_workspace(session, name="Acme", slug="acme")
        assert isinstance(ws.id, uuid.UUID)  # DB-minted uuidv7, returned through the flush
        assert ws.id.version == 7
    async with sessionmaker() as session:
        row = (await session.execute(text("SELECT name FROM workspaces WHERE slug = 'acme'"))).one()
        assert row.name == "Acme"


async def test_duplicate_slug_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        await create_workspace(session, name="Acme", slug="acme")
    with pytest.raises(IntegrityError):
        async with sessionmaker() as session, session.begin():
            await create_workspace(session, name="Acme Two", slug="acme")
