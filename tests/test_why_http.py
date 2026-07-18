"""OM-2 — the why-chain door: every task answers "why am I doing this?".

Paperclip enforces parentage to the company goal ("researching ads → +100 signups → $1M MRR").
Chorus already stores ``parent_id`` and ``goal_id``; this door walks them so the cockpit can
render the chain: task → parent tasks → goal → parent goals."""

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


def _seed_chain(dsn: str, company_id: str) -> str:
    """Goal chain (mrr → signups) + task chain (ship page → write copy) — returns the leaf task."""
    import uuid

    from chorus.ledger import Goal, Ledger, Task

    def _id() -> str:
        return str(uuid.uuid4())

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        mrr = ledger.goals.create(Goal(id=_id(), title="Reach $1M MRR"))
        signups = ledger.goals.create(Goal(id=_id(), title="+100 signups", parent_id=mrr.id))
        ship = ledger.tasks.submit(
            Task(id=_id(), intent="Ship the landing page", goal_id=signups.id)
        )
        copy = ledger.tasks.submit(
            Task(id=_id(), intent="Write the hero copy", parent_id=ship.id, goal_id=signups.id)
        )
        return copy.id
    finally:
        ledger.close()


async def _mint(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return str(ws.id), str(company.id), token


async def test_why_walks_task_parents_then_goal_parents(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "why")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    leaf = _seed_chain(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    response = await api.get(f"{base}/tasks/{leaf}/why", headers=headers)
    assert response.status_code == 200
    chain = response.json()
    # Leaf-first: the task, its parent, then the goal lineage up to the company root.
    assert [(link["kind"], link["label"]) for link in chain] == [
        ("task", "Write the hero copy"),
        ("task", "Ship the landing page"),
        ("goal", "+100 signups"),
        ("goal", "Reach $1M MRR"),
    ]

    missing = await api.get(f"{base}/tasks/nope/why", headers=headers)
    assert missing.status_code == 404

    unauthed = await api.get(f"{base}/tasks/{leaf}/why")
    assert unauthed.status_code == 401
