"""engine_task_id is written on submit and read back to rebuild routing when a company is re-hosted."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.runs import RunStatus, claim_queued_run, create_run, finalize_run, set_engine_task_id
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


async def test_fresh_mirror_rehydrates_routing_from_the_column(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert await set_engine_task_id(s, run_id, "task_root") is True

    # A brand-new mirror (empty in-memory map) — as if the conductor just re-hosted the company.
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    await mirror.rehydrate()
    event = await mirror.record(type="run.text", payload={}, task_id="task_root")
    assert event.run_id == run_id  # routed from the rehydrated map, no register_run call


async def test_rehydration_skips_terminal_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id, run_id = await _run(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        await set_engine_task_id(s, run_id, "task_root")
        await claim_queued_run(s, run_id, owner="w", lease_seconds=60)
        await finalize_run(s, run_id, owner="w", status=RunStatus.SUCCEEDED)

    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=ws_id)
    await mirror.rehydrate()
    event = await mirror.record(type="run.text", payload={}, task_id="task_root")
    assert event.run_id is None  # the run is done — its task no longer routes
