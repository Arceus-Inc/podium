"""SSE stream: replay history, tail live events, resume across reconnect (no gaps/dupes) + auth.

The streaming behaviour is tested against the `event_stream` generator directly — httpx's ASGITransport
buffers responses, so it can't drive an infinite SSE stream. HTTP-level tests cover the immediate
paths (401 / cross-tenant 404).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import Broadcaster
from podium.events._stream import event_stream
from podium.main import create_app
from podium.runs import create_run
from podium.workspaces import create_workspace


class _StubRequest:
    """event_stream only ever calls request.is_disconnected(); a connected client never disconnects."""

    async def is_disconnected(self) -> bool:
        return False


@pytest_asyncio.fixture
async def broadcaster(database_url: str) -> AsyncIterator[Broadcaster]:
    bc = Broadcaster.from_url(database_url)
    await bc.start()
    try:
        yield bc
    finally:
        await bc.stop()


async def _setup(
    admin: async_sessionmaker[AsyncSession], app: async_sessionmaker[AsyncSession], *, n: int
) -> tuple[str, str, str, EventMirror]:
    """Company + run + key + `n` mirrored events. Returns (workspace_id, company_id, token, mirror)."""
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k"
        )
        run_id = run.id
    mirror = EventMirror(app, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="t")
    for i in range(n):
        await mirror.record(type="run.text", payload={"i": i}, task_id="t")
    return ws_id, company_id, token, mirror


async def _consume_ids(gen: AsyncIterator[str], count: int, *, timeout: float = 5.0) -> list[str]:
    ids: list[str] = []

    async def _pump() -> None:
        async for frame in gen:
            for line in frame.splitlines():
                if line.startswith("id:"):
                    ids.append(line[3:].strip())
            if len(ids) >= count:
                return

    await asyncio.wait_for(_pump(), timeout)
    return ids


def _stream(
    broadcaster: Broadcaster,
    sm: async_sessionmaker[AsyncSession],
    ws_id: str,
    company_id: str,
    cursor: int,
) -> AsyncIterator[str]:
    return event_stream(
        _StubRequest(),  # type: ignore[arg-type]
        company_id=company_id,
        workspace_id=ws_id,
        cursor=cursor,
        sessionmaker=sm,
        broadcaster=broadcaster,
    )


async def test_replays_history(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, _t, _m = await _setup(sessionmaker, app_sessionmaker, n=3)
    gen = _stream(broadcaster, app_sessionmaker, ws_id, company_id, 0)
    try:
        assert await _consume_ids(gen, 3) == ["1", "2", "3"]
    finally:
        await gen.aclose()


async def test_tails_a_live_event(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, _t, mirror = await _setup(sessionmaker, app_sessionmaker, n=0)
    gen = _stream(broadcaster, app_sessionmaker, ws_id, company_id, 0)
    try:
        reader = asyncio.create_task(_consume_ids(gen, 1))
        await asyncio.sleep(0.2)  # let replay (empty) finish and the tail begin
        await mirror.record(type="run.started", payload={}, task_id="t")
        assert await reader == ["1"]
    finally:
        await gen.aclose()


async def test_resumes_after_reconnect_without_gaps_or_dupes(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, _t, mirror = await _setup(sessionmaker, app_sessionmaker, n=3)
    gen1 = _stream(broadcaster, app_sessionmaker, ws_id, company_id, 0)
    try:
        assert await _consume_ids(gen1, 2) == ["1", "2"]  # read 1,2 then "disconnect"
    finally:
        await gen1.aclose()

    await mirror.record(type="run.done", payload={}, task_id="t")  # event 4 lands while away

    gen2 = _stream(broadcaster, app_sessionmaker, ws_id, company_id, 2)  # resume at 2
    try:
        assert await _consume_ids(gen2, 2) == ["3", "4"]  # 3 and 4, never re-deliver 1/2
    finally:
        await gen2.aclose()


# --- HTTP-level (immediate-return paths only) -----------------------------------------------------


@pytest_asyncio.fixture
async def api(
    broadcaster: Broadcaster, app_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.broadcaster = broadcaster
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_stream_requires_auth(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _ws, company_id, _t, _m = await _setup(sessionmaker, app_sessionmaker, n=1)
    assert (await api.get(f"/v1/companies/{company_id}/stream")).status_code == 401


async def test_stream_foreign_company_is_not_found(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _ws, company_id, _t, _m = await _setup(sessionmaker, app_sessionmaker, n=1)
    async with sessionmaker() as s, s.begin():
        other = await create_workspace(s, name="B", slug="b")
        _, other_token = await create_api_key(s, workspace_id=other.id, name="k")
    resp = await api.get(
        f"/v1/companies/{company_id}/stream", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404
