"""Runs HTTP: idempotent create (202), read, cancel — all auth + decide + RLS scoped."""

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
    app.state.sessionmaker = app_sessionmaker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _setup(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    """Workspace A with a company and a key; plus company B in another workspace."""
    async with admin() as s, s.begin():
        a = await create_workspace(s, name="A", slug="a")
        b = await create_workspace(s, name="B", slug="b")
        a_company = await create_company(s, workspace_id=a.id, slug="ac", name="A Co")
        b_company = await create_company(s, workspace_id=b.id, slug="bc", name="B Co")
        _, token_a = await create_api_key(s, workspace_id=a.id, name="A key")
        return token_a, a_company.id, b_company.id


async def test_create_run_is_idempotent_over_http(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"directive": "ship it", "idempotency_key": "k1"}
    first = await api.post(f"/v1/companies/{a_company}/runs", json=body, headers=headers)
    second = await api.post(f"/v1/companies/{a_company}/runs", json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "queued"
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]  # same key → same run


async def test_get_run_returns_status(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers=headers,
    )
    run_id = created.json()["id"]
    got = await api.get(f"/v1/companies/{a_company}/runs/{run_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["status"] == "queued"


async def test_cancel_moves_run_to_canceling(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers=headers,
    )
    run_id = created.json()["id"]
    cancel = await api.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceling"


async def test_run_creation_on_foreign_company_is_not_found(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _a, b_company = await _setup(sessionmaker)
    # A's key targeting B's company — RLS hides B's company from A's session → 404, no run made.
    resp = await api.post(
        f"/v1/companies/{b_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_runs_require_authentication(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _token, a_company, _b = await _setup(sessionmaker)
    resp = await api.get(f"/v1/companies/{a_company}/runs/run_whatever")
    assert resp.status_code == 401
