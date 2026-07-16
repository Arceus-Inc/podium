"""Conductor orchestration (hermetic): claim → run via an injected executor → finalize; reclaim.

No chorus, no model — a fake executor stands in so the compare-and-swap orchestration is what's
under test. The real ChorusRunExecutor is covered by the skippable integration exit test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.conductor import Conductor, ExecutionResult
from podium.db import tenant_session
from podium.runs import RunStatus, claim_queued_run, create_run, get_run, request_cancel
from podium.workspaces import create_workspace

CancelCheck = Callable[[], Awaitable[bool]]


class _FakeExecutor:
    """Returns a fixed result, optionally raising, optionally running a hook (e.g. to cancel)."""

    def __init__(
        self,
        *,
        result: ExecutionResult | None = None,
        raises: Exception | None = None,
        on_execute: Callable[[], Awaitable[None]] | None = None,
        honor_cancel: bool = False,
    ) -> None:
        self._result = result or ExecutionResult(status=RunStatus.SUCCEEDED)
        self._raises = raises
        self._on_execute = on_execute
        self._honor_cancel = honor_cancel

    async def execute(
        self, *, workspace_id: str, company_id: str, directive: str, is_canceled: CancelCheck
    ) -> ExecutionResult:
        if self._on_execute is not None:
            await self._on_execute()
        if self._honor_cancel and await is_canceled():
            return ExecutionResult(status=RunStatus.CANCELED)
        if self._raises is not None:
            raise self._raises
        return self._result


async def _queue_a_run(
    admin: async_sessionmaker[AsyncSession], app: async_sessionmaker[AsyncSession]
) -> tuple[str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id
    async with tenant_session(app, ws_id) as s:
        run, _ = await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="do it", idempotency_key="k1"
        )
        return ws_id, run.id


def _conductor(
    control: async_sessionmaker[AsyncSession],
    app: async_sessionmaker[AsyncSession],
    executor: _FakeExecutor,
) -> Conductor:
    return Conductor(
        control_sessionmaker=control,
        app_sessionmaker=app,
        executor=executor,
        worker_id="conductor-test",
        lease_seconds=300,
    )


async def test_dispatches_queued_run_to_success(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)
    dispatched = await _conductor(sessionmaker, app_sessionmaker, _FakeExecutor()).dispatch_once()
    assert dispatched == 1
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED
    assert run.owner is None  # lease cleared on finalize


async def test_records_failure_from_executor(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)
    executor = _FakeExecutor(result=ExecutionResult(status=RunStatus.FAILED, error="boom"))
    await _conductor(sessionmaker, app_sessionmaker, executor).dispatch_once()
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error == "boom"


async def test_executor_exception_becomes_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)
    executor = _FakeExecutor(raises=RuntimeError("kaboom"))
    await _conductor(sessionmaker, app_sessionmaker, executor).dispatch_once()
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error is not None and "kaboom" in run.error


async def test_honors_cancel_requested_mid_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)

    async def flip_to_canceling() -> None:
        async with tenant_session(app_sessionmaker, ws_id) as s:
            await request_cancel(s, run_id)

    executor = _FakeExecutor(on_execute=flip_to_canceling, honor_cancel=True)
    await _conductor(sessionmaker, app_sessionmaker, executor).dispatch_once()
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert run.status == RunStatus.CANCELED


async def test_reclaims_expired_lease_then_redispatches(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)
    # Simulate a crashed worker: claimed with an already-expired lease.
    async with tenant_session(app_sessionmaker, ws_id) as s:
        await claim_queued_run(s, run_id, owner="dead-worker", lease_seconds=-100)

    await _conductor(sessionmaker, app_sessionmaker, _FakeExecutor()).dispatch_once()
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run = await get_run(s, run_id)
    assert run is not None
    assert (
        run.status == RunStatus.SUCCEEDED
    )  # reclaimed from the dead owner, then run to completion
    assert run.owner is None


async def test_second_dispatch_finds_no_work(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _queue_a_run(sessionmaker, app_sessionmaker)
    conductor = _conductor(sessionmaker, app_sessionmaker, _FakeExecutor())
    assert await conductor.dispatch_once() == 1
    assert await conductor.dispatch_once() == 0  # nothing left queued
