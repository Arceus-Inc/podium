"""Conductor orchestration (hermetic): claim → run via an injected executor → finalize; reclaim.

No chorus, no model — a fake executor stands in so the compare-and-swap orchestration is what's
under test. The real ChorusRunExecutor is covered by the skippable integration exit test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
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
        self,
        *,
        run_id: str,
        workspace_id: str,
        company_id: str,
        directive: str,
        is_canceled: CancelCheck,
        params=None,
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


async def _dispatch_and_drain(conductor: Conductor) -> int:
    claimed = await conductor.dispatch_once()
    await conductor.drain()
    return claimed


async def test_long_run_no_longer_starves_later_queued_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Live 2026-07-18: a long delegation run held dispatch_once's gather open, so every
    later run stayed queued until it finished. Claiming must spawn execution in the
    background and keep polling."""
    import asyncio

    gate = asyncio.Event()

    class _GatedExecutor(_FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def execute(self, **kwargs: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                await gate.wait()  # the "30-minute" delegation run
            return ExecutionResult(status=RunStatus.SUCCEEDED)

    ws_id, first_run = await _queue_a_run(sessionmaker, app_sessionmaker)
    conductor = _conductor(sessionmaker, app_sessionmaker, _GatedExecutor())
    assert await conductor.dispatch_once() == 1  # claims + spawns; must NOT block on the gate

    async with tenant_session(app_sessionmaker, ws_id) as s:
        second, _ = await create_run(
            s,
            workspace_id=ws_id,
            company_id=(await get_run(s, first_run)).company_id,
            directive="later run",
            idempotency_key="k2",
        )
    assert await conductor.dispatch_once() == 1  # the later run is claimed while #1 still runs
    await asyncio.sleep(0.05)  # let the unblocked second task finish
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert (await get_run(s, second.id)).status == RunStatus.SUCCEEDED
        assert (await get_run(s, first_run)).status == RunStatus.RUNNING  # still in flight

    gate.set()
    await conductor.drain()
    async with tenant_session(app_sessionmaker, ws_id) as s:
        assert (await get_run(s, first_run)).status == RunStatus.SUCCEEDED


async def test_dispatches_queued_run_to_success(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, run_id = await _queue_a_run(sessionmaker, app_sessionmaker)
    dispatched = await _dispatch_and_drain(
        _conductor(sessionmaker, app_sessionmaker, _FakeExecutor())
    )
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
    await _dispatch_and_drain(_conductor(sessionmaker, app_sessionmaker, executor))
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
    await _dispatch_and_drain(_conductor(sessionmaker, app_sessionmaker, executor))
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
    await _dispatch_and_drain(_conductor(sessionmaker, app_sessionmaker, executor))
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

    await _dispatch_and_drain(_conductor(sessionmaker, app_sessionmaker, _FakeExecutor()))
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
    assert await _dispatch_and_drain(conductor) == 1
    assert await _dispatch_and_drain(conductor) == 0  # nothing left queued


async def test_dispatches_a_whole_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Verifies batch completion (every queued run finalized), not wall-clock parallelism.
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(s, workspace_id=ws.id, slug="c", name="C")
        ws_id, company_id = ws.id, company.id
    run_ids = []
    for i in range(3):
        async with tenant_session(app_sessionmaker, ws_id) as s:
            run, _ = await create_run(
                s, workspace_id=ws_id, company_id=company_id, directive="d", idempotency_key=f"k{i}"
            )
            run_ids.append(run.id)

    assert (
        await _dispatch_and_drain(_conductor(sessionmaker, app_sessionmaker, _FakeExecutor())) == 3
    )
    async with tenant_session(app_sessionmaker, ws_id) as s:
        for run_id in run_ids:
            run = await get_run(s, run_id)
            assert run is not None and run.status == RunStatus.SUCCEEDED


def test_submit_kwargs_maps_delegation_params() -> None:
    """CP-3: the pure param→submit mapping (delivery default vs delegation discriminated)."""
    from chorus.ledger import ExecutionMode

    from podium.conductor._chorus_executor import _submit_kwargs

    assert _submit_kwargs({}, default_assignee="ace", ceo="casey") == {"assignee": "ace"}
    assert _submit_kwargs({"execution_mode": "delivery"}, default_assignee="ace", ceo="casey") == {
        "assignee": "ace"
    }
    # Delivery runs can be directed at any employee of the formed org (default worker otherwise).
    assert _submit_kwargs(
        {"assignee": "new_backend_eng_2"}, default_assignee="ace", ceo="casey"
    ) == {"assignee": "new_backend_eng_2"}
    # CO1: founder intent routes to the CEO — the only employee with governance tools.
    assert _submit_kwargs({"execution_mode": "formation"}, default_assignee="ace", ceo="casey") == {
        "assignee": "casey"
    }

    kwargs = _submit_kwargs(
        {
            "execution_mode": "delegation",
            "lead": "lea",
            "goal_id": "g1",
            "max_team_size": 3,
            "spend_limit_cents": 5000,
        },
        default_assignee="ace",
        ceo="casey",
    )
    assert kwargs == {
        "assignee": "lea",
        "execution_mode": ExecutionMode.DELEGATION,
        "goal_id": "g1",
        "delegation_max_team_size": 3,
        "delegation_spend_limit_cents": 5000,
    }


def test_formation_directive_carries_the_formation_contract() -> None:
    """Live 2026-07-18: 'make an AI notetaker app' in formation mode reached the CEO raw and
    she built the product herself. The product injects the formation contract server-side."""
    from podium.conductor._chorus_executor import _effective_directive

    wrapped = _effective_directive({"execution_mode": "formation"}, "make an AI notetaker app")
    assert wrapped.startswith("This is a FORMATION directive")
    assert "workforce_plan_propose" in wrapped
    assert "max_delegation_depth >= 1" in wrapped
    assert wrapped.endswith("## Objective\nmake an AI notetaker app")
    # Delivery and delegation directives pass through untouched.
    assert _effective_directive({}, "build the thing") == "build the thing"
    assert _effective_directive({"execution_mode": "delegation"}, "deliver it") == "deliver it"


def test_executor_max_ticks_comes_from_settings() -> None:
    """Live e2e finding: a real directive became an engine marathon and outlived the hardcoded
    60-tick budget. The budget is an operational knob, not a constant."""
    from podium.conductor._host import build_conductor
    from podium.settings import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://postgres@localhost:1/x",
        conductor_max_ticks=240,
    )
    conductor, _close = build_conductor(settings)
    assert conductor._executor._max_ticks == 240
