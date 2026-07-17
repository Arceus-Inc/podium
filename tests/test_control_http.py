"""CP-2 — the direction doors: /goals over the control plane (auth → decide → visibility → plane).

The read plane serves the engine's goal tree through the CompanyControlPlane (CP-0); the router
is a thin governed mapping — no engine type escapes, the company must be visible to the actor,
and a foreign workspace's token sees 404, never data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.main import create_app
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_company(
    admin: async_sessionmaker[AsyncSession], *, slug: str = "c"
) -> tuple[object, object, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return ws.id, company.id, token


def _seed_goals(database_url: str, company_id: object) -> tuple[str, str]:
    from chorus.ids import mint_id
    from chorus.ledger import Goal, GoalLevel, Ledger

    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    root_id, child_id = mint_id(), mint_id()
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.goals.create(Goal(id=root_id, title="Win launch week"))
        ledger.goals.create(
            Goal(id=child_id, title="Ship the page", level=GoalLevel.TEAM, parent_id=root_id)
        )
    finally:
        ledger.close()
    return root_id, child_id


async def test_goals_door_serves_the_tree(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _seed_company(sessionmaker)
    root_id, child_id = _seed_goals(database_url, company_id)

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/goals",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    tree = response.json()
    assert len(tree) == 1
    assert tree[0]["id"] == root_id
    assert tree[0]["title"] == "Win launch week"
    assert [child["id"] for child in tree[0]["children"]] == [child_id]


async def test_goals_door_requires_auth(
    sessionmaker: async_sessionmaker[AsyncSession], api: httpx.AsyncClient
) -> None:
    ws_id, company_id, _ = await _seed_company(sessionmaker)
    response = await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}/goals")
    assert response.status_code == 401


async def test_foreign_workspace_is_refused_at_decide(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_a, company_a, _ = await _seed_company(sessionmaker, slug="a")
    _seed_goals(database_url, company_a)
    _, _, token_b = await _seed_company(sessionmaker, slug="b")

    response = await api.get(
        f"/v1/workspaces/{ws_a}/companies/{company_a}/goals",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    # House convention for workspace-prefixed routes: a foreign workspace path is 403 at
    # decide() (leaks nothing — the path names the workspace, not the company); within the
    # right workspace, an invisible company is 404 via RLS/ownership.
    assert response.status_code == 403
