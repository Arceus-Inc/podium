"""The per-company EventMirror: single-writer monotonic seq, task→run routing, seed from max."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import Event, list_run_events
from podium.runs import create_run
from podium.workspaces import create_workspace


async def _company_with_run(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    """Returns (workspace_id, company_id, run_id)."""
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(admin, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k1"
        )
        return ws_id, company_id, run.id


async def test_mirror_assigns_monotonic_seq_and_routes_by_task(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _company_with_run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="task_root")

    await mirror.record(type="run.started", payload={}, task_id="task_root")
    await mirror.record(type="run.text", payload={"text": "hi"}, task_id="task_root")
    await mirror.record(type="routine.fired", payload={}, task_id=None)  # no task → company-level

    async with tenant_session(app_sessionmaker, ws_id) as s:
        rows = await list_run_events(s, run_id, after=0, limit=100)
    assert [e.seq for e in rows] == [1, 2]  # only the two routed to the run
    assert [e.type for e in rows] == ["run.started", "run.text"]
    assert all(e.run_id == run_id for e in rows)


async def test_unicode_payload_is_stored(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Real model output is Unicode; the JSONB payload must round-trip (regression: SQL_ASCII cluster).
    ws_id, company_id, run_id = await _company_with_run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    mirror.register_run(run_id=run_id, engine_task_id="t")
    await mirror.record(type="run.text", payload={"text": "arrow → café 日本語"}, task_id="t")
    async with tenant_session(app_sessionmaker, ws_id) as s:
        rows = await list_run_events(s, run_id, after=0, limit=10)
    assert rows[0].payload["text"] == "arrow → café 日本語"


async def test_unrouted_event_is_company_level(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, _run_id = await _company_with_run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    event = await mirror.record(type="routine.fired", payload={}, task_id="unknown")
    assert event.run_id is None  # unknown task → not attributed to a run
    assert event.seq == 1


async def test_seq_seeds_from_existing_max(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, _run_id = await _company_with_run(sessionmaker)
    # A prior mirror already wrote up to seq 5.
    async with sessionmaker() as s, s.begin():
        s.add(
            Event(
                company_id=company_id,
                seq=5,
                workspace_id=ws_id,
                run_id=None,
                type="x",
                employee_id=None,
                payload={},
                created_at=datetime.now(UTC),
            )
        )
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    event = await mirror.record(type="run.started", payload={}, task_id="unknown")
    assert event.seq == 6  # continues after the persisted max, not from 1


async def test_trace_and_task_land_on_the_row_and_route_the_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CP-1: the envelope names the lane — trace_id (lineage root) routes the run even when the
    beat's own task is a CHILD the mirror never registered; both ids land on the row."""
    from uuid import uuid4

    ws_id, company_id, run_id = await _company_with_run(sessionmaker)
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    root, child = str(uuid4()), str(uuid4())
    mirror.register_run(run_id=run_id, engine_task_id=root)

    await mirror.record(
        type="run.text", payload={"text": "hi"}, task_id=child, trace_id=root, employee_id="ada"
    )

    async with tenant_session(app_sessionmaker, ws_id) as s:
        rows = await list_run_events(s, run_id, after=0, limit=10)
    assert len(rows) == 1  # routed by trace root, not the unregistered child id
    assert str(rows[0].trace_id) == root
    assert rows[0].task_id == child
    assert rows[0].employee_id == "ada"
