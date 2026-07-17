"""M1a exit proof: `companies` is isolated by workspace via FORCE RLS, enforced on the app role.

Every test here runs writes/reads as the non-superuser `podium_app` (`app_sessionmaker`) so RLS
actually bites; superuser setup happens only through the control-plane `sessionmaker`.
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company, list_companies
from podium.db import tenant_session
from podium.workspaces import create_workspace


async def _two_workspaces(
    admin: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin() as session, session.begin():
        a = await create_workspace(session, name="Alpha", slug="alpha")
        b = await create_workspace(session, name="Beta", slug="beta")
        return a.id, b.id


async def test_company_reads_are_scoped_to_the_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a_id, b_id = await _two_workspaces(sessionmaker)
    async with tenant_session(app_sessionmaker, a_id) as s:
        await create_company(s, workspace_id=a_id, slug="c", name="A Co")
    async with tenant_session(app_sessionmaker, b_id) as s:
        await create_company(s, workspace_id=b_id, slug="c", name="B Co")

    async with tenant_session(app_sessionmaker, a_id) as s:
        rows = await list_companies(s)
    assert [c.workspace_id for c in rows] == [a_id]  # tenant A sees only its own company


async def test_cross_tenant_insert_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a_id, b_id = await _two_workspaces(sessionmaker)
    # Scoped to A, but trying to write a row belonging to B — RLS WITH CHECK must refuse it.
    with pytest.raises(ProgrammingError):
        async with tenant_session(app_sessionmaker, a_id) as s:
            await create_company(s, workspace_id=b_id, slug="x", name="X")


async def test_session_without_workspace_context_sees_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    (a_id, _) = await _two_workspaces(sessionmaker)
    async with tenant_session(app_sessionmaker, a_id) as s:
        await create_company(s, workspace_id=a_id, slug="c", name="A Co")
    # No app.workspace_id set → the policy predicate is NULL → zero rows (fail closed).
    async with app_sessionmaker() as s:
        assert list(await list_companies(s)) == []


async def test_app_role_cannot_create_workspaces(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Creating the tenant root is control-plane only; podium_app has no INSERT on workspaces.
    with pytest.raises(ProgrammingError):
        async with tenant_session(app_sessionmaker, uuid4()) as s:
            await create_workspace(s, name="Nope", slug="nope")
