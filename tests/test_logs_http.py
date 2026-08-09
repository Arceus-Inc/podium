"""GET /v1/runs/{id}/logs — streams the durable transcript, auth + RLS scoped, 404 when absent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.main import create_app
from podium.runs import create_run, set_log_ref
from podium.users import create_user
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, RunLogStore]]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    store = RunLogStore(tmp_path)
    app.state.log_store = store
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, store


async def _run_with_key(
    admin: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k"
        )
        return ws_id, company_id, run.id, token


async def test_logs_returns_the_transcript(
    api: tuple[httpx.AsyncClient, RunLogStore],
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, store = api
    ws_id, company_id, run_id, token = await _run_with_key(sessionmaker)
    mirror = EventMirror(
        app_sessionmaker,
        company_id=company_id,
        workspace_id=ws_id,
        log_store=store,
        excerpt_chars=5,
    )
    mirror.register_run(run_id=run_id, engine_task_id="t")
    await mirror.record(type="run.text", payload={"text": "a full transcript →"}, task_id="t")

    resp = await client.get(f"/v1/runs/{run_id}/logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "a full transcript →"
    assert len(resp.headers["x-log-sha256"]) == 64


async def test_logs_404_when_run_has_none(
    api: tuple[httpx.AsyncClient, RunLogStore],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, _store = api
    _ws, _c, run_id, token = await _run_with_key(sessionmaker)  # a run with no transcript
    resp = await client.get(f"/v1/runs/{run_id}/logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_logs_require_auth(
    api: tuple[httpx.AsyncClient, RunLogStore],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, _store = api
    _ws, _c, run_id, _token = await _run_with_key(sessionmaker)
    assert (await client.get(f"/v1/runs/{run_id}/logs")).status_code == 401


async def test_logs_cross_tenant_404(
    api: tuple[httpx.AsyncClient, RunLogStore],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, _store = api
    _ws, _c, run_id, _token = await _run_with_key(sessionmaker)
    async with sessionmaker() as s, s.begin():
        other = await create_workspace(s, name="B", slug="b")
        _, other_token = await create_api_key(s, workspace_id=other.id, name="k")
    resp = await client.get(
        f"/v1/runs/{run_id}/logs", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


async def test_logs_are_hidden_from_workspace_peer(
    api: tuple[httpx.AsyncClient, RunLogStore], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, store = api
    async with sessionmaker() as s, s.begin():
        workspace = await create_workspace(s, name="A", slug="a")
        owner = await create_user(s, workspace_id=workspace.id, email="owner@a.io", name="Owner")
        peer = await create_user(s, workspace_id=workspace.id, email="peer@a.io", name="Peer")
        company = await create_company(
            s, workspace_id=workspace.id, owner_user_id=owner.id, slug="c", name="C"
        )
        _, peer_token = await create_api_key(
            s, workspace_id=workspace.id, name="peer", user_id=peer.id
        )
    async with tenant_session(sessionmaker, workspace.id) as s:
        run, _ = await create_run(
            s, workspace_id=workspace.id, company_id=company.id, directive="d", idempotency_key="k"
        )
        await set_log_ref(s, run.id, store.ref(run.id))
    store.append(run.id, "private transcript")
    response = await client.get(
        f"/v1/runs/{run.id}/logs", headers={"Authorization": f"Bearer {peer_token}"}
    )
    assert response.status_code == 404
