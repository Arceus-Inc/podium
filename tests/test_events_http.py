"""Events cursor-paging endpoint: ordered by seq, `after` cursor, `limit`, auth + RLS scoped."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.main import create_app
from podium.runs import create_run
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


async def _seed(
    admin: async_sessionmaker[AsyncSession],
    app: async_sessionmaker[AsyncSession],
    *,
    n_events: int,
) -> tuple[str, str, str, str]:
    """Workspace A with a company, run, key, and `n_events` mirrored events. Returns ids + token."""
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k1"
        )
        run_id = run.id

    mirror = EventMirror(app, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="t")
    for i in range(n_events):
        await mirror.record(type="run.text", payload={"i": i}, task_id="t")
    return run_id, token, company_id, ws_id


async def test_lists_events_in_seq_order(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, token, company_id, workspace_id = await _seed(sessionmaker, app_sessionmaker, n_events=3)
    resp = await api.get(f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert [e["seq"] for e in body["data"]] == [1, 2, 3]
    assert body["meta"]["has_next"] is False
    canonical = await api.get(
        f"/v1/workspaces/{workspace_id}/companies/{company_id}/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert canonical.json() == body


async def test_after_cursor_and_limit(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, token, _c, _workspace_id = await _seed(sessionmaker, app_sessionmaker, n_events=5)
    headers = {"Authorization": f"Bearer {token}"}
    page = await api.get(f"/v1/runs/{run_id}/events?after=1&limit=2", headers=headers)
    body = page.json()
    assert [e["seq"] for e in body["data"]] == [2, 3]
    assert body["meta"]["has_next"] is True
    assert body["meta"]["next_after"] == 3


async def test_events_are_tenant_scoped(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, _token, _c, _workspace_id = await _seed(sessionmaker, app_sessionmaker, n_events=1)
    # A different workspace's key cannot see run A's events (run not visible → 404).
    async with sessionmaker() as s, s.begin():
        other = await create_workspace(s, name="B", slug="b")
        _, other_token = await create_api_key(s, workspace_id=other.id, name="k")
    resp = await api.get(
        f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


async def test_events_hide_another_users_company_in_the_same_workspace(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        owner = await create_user(session, workspace_id=workspace.id, email="owner@a.io", name="Owner")
        peer = await create_user(session, workspace_id=workspace.id, email="peer@a.io", name="Peer")
        company = await create_company(
            session,
            workspace_id=workspace.id,
            slug="owned",
            name="Owned",
            owner_user_id=owner.id,
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer-key", user_id=peer.id
        )
    async with tenant_session(sessionmaker, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="d",
            idempotency_key="k1",
        )

    for url in (
        f"/v1/runs/{run.id}/events",
        f"/v1/workspaces/{workspace.id}/companies/{company.id}/runs/{run.id}/events",
    ):
        assert (await api.get(url, headers={"Authorization": f"Bearer {peer_token}"})).status_code == 404


async def test_events_hide_another_company_from_a_scoped_service_key(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        allowed_company = await create_company(session, workspace_id=workspace.id, slug="allowed", name="Allowed")
        target_company = await create_company(session, workspace_id=workspace.id, slug="target", name="Target")
        _, token = await create_api_key(
            session, workspace_id=workspace.id, company_id=allowed_company.id, name="scoped-key"
        )
    async with tenant_session(sessionmaker, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=target_company.id,
            directive="d",
            idempotency_key="k1",
        )

    for url in (
        f"/v1/runs/{run.id}/events",
        f"/v1/workspaces/{workspace.id}/companies/{target_company.id}/runs/{run.id}/events",
    ):
        assert (await api.get(url, headers={"Authorization": f"Bearer {token}"})).status_code == 404


async def test_events_require_auth(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, _token, _c, _workspace_id = await _seed(sessionmaker, app_sessionmaker, n_events=1)
    assert (await api.get(f"/v1/runs/{run_id}/events")).status_code == 401
