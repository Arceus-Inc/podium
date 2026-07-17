"""M5 §2.5: a company belongs to a user. The workspace stays the hard RLS wall; ownership is the
authorization filter WITHIN it — a user-bound key sees and acts on its own companies (plus
workspace-owned ones); a service key (no user) operates workspace-wide.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.main import create_app
from podium.users import create_user
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _workspace_with_two_users(
    admin: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    """(workspace_id, u1_token, u2_token, service_token) — two user keys + one service key."""
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        u1 = await create_user(s, workspace_id=ws.id, email="u1@a.io", name="U1")
        u2 = await create_user(s, workspace_id=ws.id, email="u2@a.io", name="U2")
        _, t1 = await create_api_key(s, workspace_id=ws.id, name="k1", user_id=u1.id)
        _, t2 = await create_api_key(s, workspace_id=ws.id, name="k2", user_id=u2.id)
        _, service = await create_api_key(s, workspace_id=ws.id, name="svc")
        return str(ws.id), t1, t2, service


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_created_company_is_owned_by_the_creating_user(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, t1, _t2, _svc = await _workspace_with_two_users(sessionmaker)
    created = await api.post(
        f"/v1/workspaces/{ws_id}/companies", json={"slug": "c1", "name": "C1"}, headers=_auth(t1)
    )
    assert created.status_code == 201, created.text
    assert created.json()["owner_user_id"] is not None


async def test_workspace_peer_cannot_see_anothers_company(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, t1, t2, _svc = await _workspace_with_two_users(sessionmaker)
    created = await api.post(
        f"/v1/workspaces/{ws_id}/companies", json={"slug": "c1", "name": "C1"}, headers=_auth(t1)
    )
    company_id = created.json()["id"]
    # U2: absent from list, 404 on direct get, 404 creating a run against it.
    listed = await api.get(f"/v1/workspaces/{ws_id}/companies", headers=_auth(t2))
    assert all(company["id"] != company_id for company in listed.json())
    assert (
        await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}", headers=_auth(t2))
    ).status_code == 404
    run = await api.post(
        f"/v1/companies/{company_id}/runs",
        json={"directive": "d", "idempotency_key": "k"},
        headers=_auth(t2),
    )
    assert run.status_code == 404
    # U1 keeps full access.
    assert (
        await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}", headers=_auth(t1))
    ).status_code == 200


async def test_service_key_operates_workspace_wide(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, t1, _t2, svc = await _workspace_with_two_users(sessionmaker)
    created = await api.post(
        f"/v1/workspaces/{ws_id}/companies", json={"slug": "c1", "name": "C1"}, headers=_auth(t1)
    )
    company_id = created.json()["id"]
    listed = await api.get(f"/v1/workspaces/{ws_id}/companies", headers=_auth(svc))
    assert any(company["id"] == company_id for company in listed.json())
    assert (
        await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}", headers=_auth(svc))
    ).status_code == 200
    # A service-created company is workspace-owned (no owner) — visible to every member.
    workspace_owned = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "shared", "name": "S"},
        headers=_auth(svc),
    )
    assert workspace_owned.json()["owner_user_id"] is None
    shared_id = workspace_owned.json()["id"]
    assert (
        await api.get(f"/v1/workspaces/{ws_id}/companies/{shared_id}", headers=_auth(t1))
    ).status_code == 200
