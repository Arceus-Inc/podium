"""The mirror splits big text: an excerpt in the event row, the full transcript in the log store."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import list_run_events
from podium.logs import RunLogStore
from podium.runs import create_run, get_run
from podium.workspaces import create_workspace


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


async def test_large_text_is_split_to_store_and_excerpt(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    store = RunLogStore(tmp_path)
    mirror = EventMirror(
        app_sessionmaker,
        company_id=company_id,
        workspace_id=ws_id,
        log_store=store,
        excerpt_chars=50,
    )
    mirror.register_run(run_id=run_id, engine_task_id="t")
    big = "x" * 500
    await mirror.record(type="run.text", payload={"text": big, "role": "gen"}, task_id="t")

    async with tenant_session(app_sessionmaker, ws_id) as s:
        rows = await list_run_events(s, run_id, after=0, limit=10)
        run = await get_run(s, run_id)
    assert rows[0].payload["text"] == "x" * 50  # excerpt in the row
    assert rows[0].payload["text_truncated"] is True
    assert store.read(run_id) == big  # full transcript in the file
    assert run is not None and run.log_ref == store.ref(run_id)  # pointer set on the run


async def test_short_text_stays_in_the_row_no_log(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    store = RunLogStore(tmp_path)
    mirror = EventMirror(
        app_sessionmaker,
        company_id=company_id,
        workspace_id=ws_id,
        log_store=store,
        excerpt_chars=50,
    )
    mirror.register_run(run_id=run_id, engine_task_id="t")
    await mirror.record(type="run.text", payload={"text": "brief"}, task_id="t")

    async with tenant_session(app_sessionmaker, ws_id) as s:
        rows = await list_run_events(s, run_id, after=0, limit=10)
        run = await get_run(s, run_id)
    assert rows[0].payload == {"text": "brief"}  # untouched
    assert store.exists(run_id) is False
    assert run is not None and run.log_ref is None
