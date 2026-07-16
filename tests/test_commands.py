"""The conductor command mailbox: enqueue once, consume once (idempotent consumption)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import enqueue_command, mark_consumed
from podium.db import tenant_session
from podium.workspaces import create_workspace


async def test_command_is_consumed_exactly_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id

    async with tenant_session(app_sessionmaker, ws_id) as s:
        command = await enqueue_command(s, workspace_id=ws_id, company_id=company_id, type="cancel")
        command_id = command.id

    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert await mark_consumed(s, command_id) is True
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert await mark_consumed(s, command_id) is False  # already consumed
