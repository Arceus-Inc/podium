"""Sliding-window limiter logic (with an injected clock) and the HTTP 429 it produces."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import SlidingWindowRateLimiter, create_api_key
from podium.main import create_app
from podium.workspaces import create_workspace


def test_allows_up_to_limit_then_blocks() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60, clock=lambda: 0.0)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False  # third within the window


def test_window_slides_and_frees_capacity() -> None:
    now = {"t": 0.0}
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=10, clock=lambda: now["t"])
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    now["t"] = 11.0  # first hit falls out of the window
    assert limiter.allow("k") is True


def test_keys_are_independent() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, clock=lambda: 0.0)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True  # different key, own budget
    assert limiter.allow("a") is False


async def test_http_returns_429_over_limit(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id = ws.id

    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.rate_limiter = SlidingWindowRateLimiter(
        max_requests=1, window_seconds=60, clock=lambda: 0.0
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        first = await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
        second = await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429
