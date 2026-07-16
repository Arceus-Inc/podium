"""End-to-end M1 exit proof over HTTP: cross-tenant access fails at BOTH decide() and RLS.

Setup is control-plane (superuser `sessionmaker`); the app under test runs as `podium_app`
(`app_sessionmaker`) so RLS is live behind the authorization check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.main import create_app
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker  # run the app as the RLS-subject role
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _setup(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str, str, str]:
    """Two workspaces, an API key for A, and a company that belongs to B. Returns ids + A's token."""
    async with admin() as session, session.begin():
        a = await create_workspace(session, name="A", slug="a")
        b = await create_workspace(session, name="B", slug="b")
        _, token_a = await create_api_key(session, workspace_id=a.id, name="A key")
        b_company = await create_company(session, workspace_id=b.id, slug="bco", name="B Co")
        return a.id, b.id, token_a, b_company.id


async def test_missing_credential_is_rejected(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a_id, *_ = await _setup(sessionmaker)
    resp = await api.get(f"/v1/workspaces/{a_id}/companies")
    assert resp.status_code == 401


async def test_invalid_credential_is_rejected(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a_id, *_ = await _setup(sessionmaker)
    resp = await api.get(
        f"/v1/workspaces/{a_id}/companies", headers={"Authorization": "Bearer bogus"}
    )
    assert resp.status_code == 401


async def test_create_and_list_within_own_tenant(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a_id, _b, token_a, _bco = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token_a}"}
    created = await api.post(
        f"/v1/workspaces/{a_id}/companies", json={"slug": "aco", "name": "A Co"}, headers=headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["workspace_id"] == a_id

    listed = await api.get(f"/v1/workspaces/{a_id}/companies", headers=headers)
    assert listed.status_code == 200
    assert [c["slug"] for c in listed.json()] == ["aco"]


async def test_cross_tenant_path_denied_by_decide(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _a, b_id, token_a, b_company = await _setup(sessionmaker)
    # A's key pointing at B's workspace — decide() denies before any query runs.
    resp = await api.get(
        f"/v1/workspaces/{b_id}/companies/{b_company}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 403


async def test_rls_backstops_foreign_id_on_own_path(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a_id, _b, token_a, b_company = await _setup(sessionmaker)
    # Path says A (decide passes) but the id belongs to B — RLS hides it → 404, not a leak.
    resp = await api.get(
        f"/v1/workspaces/{a_id}/companies/{b_company}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404
