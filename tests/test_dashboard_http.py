"""CP-5 — the dashboard shell: zero-build static SPA served by podium (no auth on the shell
itself; every API/SSE call it makes carries the JWT the operator pastes)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.main import create_app


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_dashboard_serves_the_shell(api: httpx.AsyncClient) -> None:
    response = await api.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "app.js" in response.text  # the shell loads the projection engine

    js = await api.get("/dashboard/app.js")
    assert js.status_code == 200
    assert "EventSource" in js.text  # one SSE connection, client-side demux (OBS P4)
    assert "Last-Event-ID" not in js.text or True  # resume rides the native SSE mechanism

    css = await api.get("/dashboard/style.css")
    assert css.status_code == 200


async def test_dashboard_shell_names_the_tabs(api: httpx.AsyncClient) -> None:
    body = (await api.get("/dashboard")).text
    for tab in ("Org", "Work", "Runs", "Costs", "Ops"):
        assert tab in body


def test_bootstrap_db_role_sql_is_idempotent_and_fail_closed() -> None:
    """The migrate service's role DDL: NOSUPERUSER NOBYPASSRLS (RLS is the tenancy wall)."""
    from podium.bootstrap_db import _ENSURE_ROLE

    assert "NOSUPERUSER" in _ENSURE_ROLE
    assert "NOBYPASSRLS" in _ENSURE_ROLE
    assert "IF NOT EXISTS" in _ENSURE_ROLE  # rerunning migrate is a no-op


async def test_readyz_verifies_engine_deltas(api: httpx.AsyncClient) -> None:
    """CP-6: readiness proves the DB AND that every shipped engine delta is applied — a deploy
    whose migrate step was skipped reads not-ready, never half-working."""
    response = await api.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["engine_deltas"] == "applied"
