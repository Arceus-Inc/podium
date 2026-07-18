"""/healthz is a static liveness probe; /readyz must actually reach the DB (200 up, 503 down)."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import make_engine, make_sessionmaker
from podium.main import create_app


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_healthz_is_static() -> None:
    app = create_app()
    async with _client(app) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_ok_when_db_reachable(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app()
    app.state.sessionmaker = sessionmaker
    async with _client(app) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "engine_deltas": "applied"}


async def test_readyz_503_when_db_down() -> None:
    app = create_app()
    dead = make_engine("postgresql+asyncpg://postgres@127.0.0.1:1/postgres")
    app.state.sessionmaker = make_sessionmaker(dead)
    async with _client(app) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}
    await dead.dispose()
