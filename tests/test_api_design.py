"""API contract: structured error envelope, 409 on conflict, Retry-After on 429, Location on 201."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import SlidingWindowRateLimiter, create_api_key
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


async def _ws_key(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return ws.id, token


async def test_duplicate_company_slug_is_409_conflict(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"slug": "acme", "name": "Acme"}
    assert (
        await api.post(f"/v1/workspaces/{ws_id}/companies", json=body, headers=headers)
    ).status_code == 201
    conflict = await api.post(f"/v1/workspaces/{ws_id}/companies", json=body, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


async def test_not_found_uses_error_envelope(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{uuid4()}",  # well-formed but absent -> 404
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert isinstance(error["message"], str)


async def test_validation_error_envelope_has_field_details(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme"},  # missing "name"
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any(d["field"] == "name" for d in error["details"])


async def test_created_company_returns_location_header(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme", "name": "Acme"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.headers["Location"] == f"/v1/workspaces/{ws_id}/companies/{resp.json()['id']}"


async def test_company_response_omits_internal_config(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme", "name": "Acme", "config": {"secret": "shh"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert "config" not in resp.json()  # internal config never echoed


async def test_rate_limited_response_sets_retry_after(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.rate_limiter = SlidingWindowRateLimiter(
        max_requests=1, window_seconds=60, clock=lambda: 0.0
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
        limited = await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert limited.headers["Retry-After"] == "60"
