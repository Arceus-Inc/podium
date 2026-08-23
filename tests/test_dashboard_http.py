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
    # One SSE connection, client-side demux (OBS P4). The stream emits NAMED events, which
    # EventSource.onmessage silently drops — the reader is fetch-based, resumes via
    # Last-Event-ID, and sends the token as a header (never in the URL).
    assert "Last-Event-ID" in js.text
    assert "access_token" not in js.text

    css = await api.get("/dashboard/style.css")
    assert css.status_code == 200


async def test_shell_can_drive_a_run(api: httpx.AsyncClient) -> None:
    body = (await api.get("/dashboard")).text
    assert 'id="run-form"' in body  # the page submits runs itself, not just observes
    js = (await api.get("/dashboard/app.js")).text
    assert "idempotency_key" in js


async def test_dashboard_shell_names_the_sections(api: httpx.AsyncClient) -> None:
    body = (await api.get("/dashboard")).text
    for section in (
        "Overview",
        "Direction",
        "Org",
        "Work",
        "Delegation",
        "Runs",
        "Episodic",
        "Semantic",
        "Skills",
        "LLMOps",
        "Ops",
    ):
        assert section in body


def test_bootstrap_db_role_sql_is_idempotent_and_fail_closed() -> None:
    """The migrate service's role DDL: NOSUPERUSER NOBYPASSRLS (RLS is the tenancy wall)."""
    from podium.bootstrap_db import _ENSURE_ROLE

    assert "NOSUPERUSER" in _ENSURE_ROLE
    assert "NOBYPASSRLS" in _ENSURE_ROLE
    assert "IF NOT EXISTS" in _ENSURE_ROLE  # rerunning migrate is a no-op


async def test_readyz_verifies_engine_migrations(api: httpx.AsyncClient) -> None:
    """CP-6: readiness proves the DB AND that every shipped engine delta is applied — a deploy
    whose migrate step was skipped reads not-ready, never half-working."""
    response = await api.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ready"}


async def test_dev_bootstrap_registers_a_playground(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """One click, zero uuid-pasting: the dev door mints workspace + company + token. Gated —
    it uses the privileged control connection, so it must be explicitly enabled."""
    from podium.db import make_engine, make_sessionmaker

    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.bootstrap_sessionmaker = make_sessionmaker(make_engine(database_url))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        created = await client.post("/v1/dev/bootstrap", json={"name": "Wax Works"})
        assert created.status_code == 201
        body = created.json()
        assert set(body) == {"workspace_id", "company_id", "token"}

        # The minted token is live: the control doors accept it immediately.
        roster = await client.get(
            f"/v1/workspaces/{body['workspace_id']}/companies/{body['company_id']}/workforce",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        # 200 with empty roster (no engine ledger wired in this app instance is fine → 503),
        # but auth/visibility must pass — never 401/403/404.
        assert roster.status_code in (200, 503)


async def test_dev_bootstrap_is_absent_unless_enabled(api: httpx.AsyncClient) -> None:
    response = await api.post("/v1/dev/bootstrap", json={})
    assert response.status_code == 404  # fail-closed: no privileged door by default
