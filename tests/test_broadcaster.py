"""Broadcaster: a real Postgres LISTEN fans a mirror's NOTIFY out to the right company's subscribers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import Broadcaster
from podium.runs import create_run
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def broadcaster(database_url: str) -> AsyncIterator[Broadcaster]:
    bc = Broadcaster.from_url(database_url)
    await bc.start()
    try:
        yield bc
    finally:
        await bc.stop()


async def _company(admin: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name=slug, slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k"
        )
        return ws_id, company_id, run.id


async def test_notify_reaches_the_subscriber(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _company(sessionmaker, "a")
    sub = broadcaster.subscribe(company_id)
    try:
        mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
        mirror.register_run(run_id=run_id, engine_task_id="t")
        await mirror.record(type="run.text", payload={"x": 1}, task_id="t")

        wake = await asyncio.wait_for(sub.get(), timeout=5)
        assert wake.company_id == company_id
        assert wake.seq == 1
        assert wake.type == "run.text"
    finally:
        broadcaster.unsubscribe(sub)


async def test_fan_out_is_scoped_to_the_company(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_a, company_a, run_a = await _company(sessionmaker, "a")
    _ws_b, company_b, _run_b = await _company(sessionmaker, "b")
    sub_b = broadcaster.subscribe(company_b)  # subscribed to B only
    try:
        mirror = EventMirror(app_sessionmaker, company_id=company_a, workspace_id=ws_a)
        mirror.register_run(run_id=run_a, engine_task_id="t")
        await mirror.record(type="run.text", payload={}, task_id="t")  # A's event

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub_b.get(), timeout=1)  # B must not hear A
    finally:
        broadcaster.unsubscribe(sub_b)


async def test_overflow_marks_subscriber_not_raises(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _company(sessionmaker, "a")
    sub = broadcaster.subscribe(company_id, maxsize=2)
    try:
        mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
        mirror.register_run(run_id=run_id, engine_task_id="t")
        for _ in range(5):  # more than the queue holds, never drained
            await mirror.record(type="run.text", payload={}, task_id="t")
        await asyncio.sleep(0.2)  # let notifies arrive
        assert sub.overflowed is True  # flagged for disconnect, no exception
    finally:
        broadcaster.unsubscribe(sub)
