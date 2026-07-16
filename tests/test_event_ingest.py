"""EventIngest: a (possibly off-loop) chorus EventBus callback lands in the mirror via a queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from chorus.events import Event, EventKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import EventIngest, EventMirror
from podium.db import tenant_session
from podium.events import list_run_events
from podium.runs import create_run
from podium.workspaces import create_workspace


class _FakeBus:
    """A stand-in chorus EventBus: subscribe returns an unsubscribe; emit calls every subscriber."""

    def __init__(self) -> None:
        self._subs: list[Callable[[Any], None]] = []

    def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        self._subs.append(callback)
        return lambda: self._subs.remove(callback)

    def emit(self, event: Any) -> None:
        for cb in list(self._subs):
            cb(event)


async def _run(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k"
        )
        return ws_id, company_id, run.id


async def _run_events(app: async_sessionmaker[AsyncSession], ws_id: str, run_id: str) -> list[str]:
    async with tenant_session(app, ws_id) as s:
        return [e.type for e in await list_run_events(s, run_id, after=0, limit=100)]


async def _wait_for(predicate: Callable[[], Any], *, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


def _event(kind: EventKind, task_id: str | None) -> Event:
    return Event(kind=kind, at=datetime.now(UTC), task_id=task_id, payload={"k": "v"})


async def test_ingest_persists_bus_events_through_the_mirror(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="root")
    bus = _FakeBus()
    ingest = EventIngest(bus, mirror)
    ingest.start()
    try:
        bus.emit(_event(EventKind.RUN_STARTED, "root"))
        bus.emit(_event(EventKind.RUN_TEXT, "root"))
        await _wait_for(lambda: _run_events(app_sessionmaker, ws_id, run_id))
        assert await _run_events(app_sessionmaker, ws_id, run_id) == ["run.started", "run.text"]
    finally:
        await ingest.stop()


async def test_ingest_routes_a_descendant_task_via_the_resolver(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="root")
    bus = _FakeBus()
    # A beat's event carries a child task id; the resolver maps it to the run's root task.
    ingest = EventIngest(bus, mirror, resolve_root=lambda tid: "root" if tid == "child" else None)
    ingest.start()
    try:
        bus.emit(_event(EventKind.RUN_TEXT, "child"))

        async def _has_one() -> bool:
            return bool(await _run_events(app_sessionmaker, ws_id, run_id))

        await _wait_for(_has_one)
    finally:
        await ingest.stop()
    assert await _run_events(app_sessionmaker, ws_id, run_id) == [
        "run.text"
    ]  # attributed to the run
