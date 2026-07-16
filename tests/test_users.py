"""Users are tenant-isolated by RLS, and a user-linked API key resolves to a `user` actor."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key, resolve_actor
from podium.db import tenant_session
from podium.users import create_user, list_users
from podium.workspaces import create_workspace


async def _two_workspaces(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with admin() as s, s.begin():
        a = await create_workspace(s, name="A", slug="a")
        b = await create_workspace(s, name="B", slug="b")
        return a.id, b.id


async def test_users_are_tenant_isolated(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a_id, b_id = await _two_workspaces(sessionmaker)
    async with tenant_session(app_sessionmaker, a_id) as s:
        await create_user(s, workspace_id=a_id, email="a@x.com", name="A User")

    async with tenant_session(app_sessionmaker, a_id) as s:
        assert [u.email for u in await list_users(s)] == ["a@x.com"]
    async with tenant_session(app_sessionmaker, b_id) as s:
        assert list(await list_users(s)) == []


async def test_cross_tenant_user_insert_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a_id, b_id = await _two_workspaces(sessionmaker)
    with pytest.raises(ProgrammingError):
        async with tenant_session(app_sessionmaker, a_id) as s:
            await create_user(s, workspace_id=b_id, email="x@x.com", name="X")


async def test_user_linked_key_resolves_as_user_actor(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        user = await create_user(s, workspace_id=ws.id, email="u@x.com", name="U")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k", user_id=user.id)
        user_id = user.id

    async with sessionmaker() as s:
        actor = await resolve_actor(s, token)
    assert actor is not None
    assert actor.actor_type == "user"
    assert actor.actor_id == user_id
