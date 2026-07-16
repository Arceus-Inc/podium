"""Run lifecycle is compare-and-swap: idempotent create, single-owner claim, owner-checked finalize."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.db import tenant_session
from podium.runs import (
    RunStatus,
    claim_queued_run,
    create_run,
    finalize_run,
    get_run,
    request_cancel,
)
from podium.workspaces import create_workspace


async def _workspace_with_company(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        return ws.id, company.id


async def test_create_run_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id = await _workspace_with_company(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run, created = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="ship it", idempotency_key="k1"
        )
        again, created_again = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="ship it", idempotency_key="k1"
        )
    assert created is True
    assert created_again is False
    assert run.id == again.id
    assert run.status == RunStatus.QUEUED


async def test_claim_is_compare_and_swap(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id = await _workspace_with_company(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k1"
        )
        run_id = run.id
    async with tenant_session(app_sessionmaker, ws_id) as s:
        first = await claim_queued_run(s, run_id, owner="worker-1", lease_seconds=60)
        second = await claim_queued_run(s, run_id, owner="worker-2", lease_seconds=60)
    assert first is True
    assert second is False  # a 409 is a real owner — stop, don't retry
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.RUNNING
    assert run.owner == "worker-1"


async def test_finalize_requires_the_owning_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id = await _workspace_with_company(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k1"
        )
        run_id = run.id
        await claim_queued_run(s, run_id, owner="worker-1", lease_seconds=60)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        stolen = await finalize_run(s, run_id, owner="worker-2", status=RunStatus.SUCCEEDED)
        owned = await finalize_run(s, run_id, owner="worker-1", status=RunStatus.SUCCEEDED)
    assert stolen is False
    assert owned is True
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED
    assert run.owner is None  # lock cleared on finalize


async def test_cancel_moves_running_run_to_canceling_and_is_a_noop_when_terminal(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, company_id = await _workspace_with_company(sessionmaker)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key="k1"
        )
        run_id = run.id
        await claim_queued_run(s, run_id, owner="w", lease_seconds=60)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert await request_cancel(s, run_id) is True
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
        assert run is not None and run.status == RunStatus.CANCELING
        await finalize_run(s, run_id, owner="w", status=RunStatus.CANCELED)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert await request_cancel(s, run_id) is False  # already terminal
