"""Task-scoped cancellation at the Podium → Chorus boundary."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import cast
from unittest.mock import patch

from chorus.ledger import TaskStatus

from podium.conductor._chorus_executor import ChorusRunExecutor, CompanyGraphHost
from podium.runs import RunStatus


@dataclass(frozen=True)
class _Task:
    id: str
    status: TaskStatus


@dataclass
class _Tasks:
    entries: tuple[_Task, ...]

    def get(self, task_id: str) -> _Task | None:
        return next((task for task in self.entries if task.id == task_id), None)


@dataclass(frozen=True)
class _Event:
    task_id: str | None


class _EventBus:
    def __init__(self) -> None:
        self._callbacks: list[Callable[[_Event], None]] = []
        self.subscription_count = 0
        self.unsubscription_count = 0
        self.on_subscribe: Callable[[], None] | None = None

    def subscribe(self, callback: Callable[[_Event], None]) -> Callable[[], None]:
        self._callbacks.append(callback)
        self.subscription_count += 1
        if self.on_subscribe is not None:
            self.on_subscribe()

        def unsubscribe() -> None:
            self._callbacks.remove(callback)
            self.unsubscription_count += 1

        return unsubscribe

    def emit(self, event: _Event) -> None:
        for callback in tuple(self._callbacks):
            callback(event)


@dataclass(frozen=True)
class _Ledger:
    tasks: _Tasks


class _Chorus:
    def __init__(self, ledger: _Ledger, event_bus: _EventBus | None = None) -> None:
        self._ledger = ledger
        self._event_bus = event_bus or _EventBus()
        self.cancelled_task_ids: list[str] = []

    def cancel_task(self, task_id: str) -> bool:
        self.cancelled_task_ids.append(task_id)
        return True


class _TerminalizingChorus(_Chorus):
    def __init__(self, ledger: _Ledger, terminal_task: _Task) -> None:
        super().__init__(ledger)
        self._terminal_task = terminal_task

    def cancel_task(self, task_id: str) -> bool:
        self.cancelled_task_ids.append(task_id)
        self._ledger.tasks.entries = (self._terminal_task,)
        return False


@dataclass(frozen=True)
class _Graph:
    org: _Chorus


@dataclass(frozen=True)
class _Runtime:
    graph: _Graph


class _Host:
    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self.attached_task_ids: list[str] = []

    async def ensure(self, company_id: uuid.UUID, workspace_id: uuid.UUID) -> _Runtime:
        return self._runtime

    async def attach_run(
        self,
        runtime: object,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        engine_task_id: str,
    ) -> None:
        self.attached_task_ids.append(engine_task_id)

    async def propose_next_direction(self, runtime: object) -> None:
        return None

    def write_direction_report(self, runtime: object, company_id: uuid.UUID) -> None:
        return None


def _executor(*tasks: _Task, max_ticks: int = 1) -> tuple[ChorusRunExecutor, _Chorus, _Host]:
    chorus = _Chorus(_Ledger(_Tasks(tasks)))
    host = _Host(_Runtime(_Graph(chorus)))
    return ChorusRunExecutor(cast(CompanyGraphHost, host), max_ticks=max_ticks), chorus, host


async def _canceled() -> bool:
    return True


async def _not_canceled() -> bool:
    return False


async def _timed_out_wait(awaitable: Coroutine[object, object, object], timeout: float) -> object:
    del timeout
    awaitable.close()
    raise TimeoutError


async def _immediate_wait(awaitable: Coroutine[object, object, object], timeout: float) -> object:
    del timeout
    awaitable.close()
    return object()


async def _wait_for_subscription(bus: _EventBus) -> None:
    for _ in range(10):
        if bus.subscription_count:
            return
        await asyncio.sleep(0)
    raise AssertionError("watcher did not subscribe")


async def test_product_cancellation_cancels_the_engine_task_before_returning_canceled() -> None:
    executor, chorus, host = _executor(_Task("target", TaskStatus.IN_PROGRESS))

    result = await executor.execute(
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        directive="d",
        is_canceled=_canceled,
        engine_task_id="target",
    )

    assert result.status is RunStatus.CANCELED
    assert chorus.cancelled_task_ids == ["target"]
    assert host.attached_task_ids == ["target"]
    assert chorus._event_bus.unsubscription_count == 1


async def test_timeout_cancels_the_engine_task_before_returning_timed_out() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS))

    with (
        patch("asyncio.wait_for", new=_timed_out_wait),
        patch(
            "podium.conductor._chorus_executor.monotonic",
            side_effect=(0.0, 0.0, 0.0, 1.0),
        ),
    ):
        result = await executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        )

    assert result.status is RunStatus.TIMED_OUT
    assert chorus.cancelled_task_ids == ["target"]
    assert chorus._event_bus.unsubscription_count == 1


async def test_cancellation_isolated_to_the_run_engine_task() -> None:
    executor, chorus, _host = _executor(
        _Task("target", TaskStatus.IN_PROGRESS), _Task("sibling", TaskStatus.IN_PROGRESS)
    )

    await executor.execute(
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        directive="d",
        is_canceled=_canceled,
        engine_task_id="target",
    )

    assert chorus.cancelled_task_ids == ["target"]


async def test_engine_terminal_outcome_wins_a_product_cancellation_race() -> None:
    running = _Task("target", TaskStatus.IN_PROGRESS)
    chorus = _TerminalizingChorus(_Ledger(_Tasks((running,))), _Task("target", TaskStatus.DONE))
    host = _Host(_Runtime(_Graph(chorus)))
    executor = ChorusRunExecutor(cast(CompanyGraphHost, host), max_ticks=1)

    result = await executor.execute(
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        directive="d",
        is_canceled=_canceled,
        engine_task_id="target",
    )

    assert result.status is RunStatus.SUCCEEDED
    assert chorus.cancelled_task_ids == ["target"]


async def test_correlated_event_wakes_terminal_task_without_waiting_for_poll_interval() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS))
    run = asyncio.create_task(
        executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        )
    )
    await _wait_for_subscription(chorus._event_bus)
    chorus._ledger.tasks.entries = (_Task("target", TaskStatus.DONE),)
    chorus._event_bus.emit(_Event(task_id="target"))

    result = await asyncio.wait_for(run, timeout=0.1)

    assert result.status is RunStatus.SUCCEEDED
    assert chorus._event_bus.unsubscription_count == 1


async def test_nonterminal_task_events_do_not_consume_the_timeout_budget() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS), max_ticks=1)
    run = asyncio.create_task(
        executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        )
    )
    await _wait_for_subscription(chorus._event_bus)
    chorus._event_bus.emit(_Event(task_id="target"))
    await asyncio.sleep(0)

    assert not run.done()
    chorus._ledger.tasks.entries = (_Task("target", TaskStatus.DONE),)
    chorus._event_bus.emit(_Event(task_id="target"))

    result = await asyncio.wait_for(run, timeout=0.1)
    assert result.status is RunStatus.SUCCEEDED


async def test_immediate_nonterminal_wakes_cannot_extend_the_deadline() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS), max_ticks=1)

    with (
        patch("asyncio.wait_for", new=_immediate_wait),
        patch(
            "podium.conductor._chorus_executor.monotonic",
            side_effect=(0.0, 0.0, 0.0, 0.5, 0.5, 1.0),
        ),
    ):
        result = await executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        )

    assert result.status is RunStatus.TIMED_OUT
    assert chorus.cancelled_task_ids == ["target"]


async def test_unrelated_events_do_not_wake_a_terminal_task() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS), max_ticks=0)
    run = asyncio.create_task(
        executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        )
    )
    await _wait_for_subscription(chorus._event_bus)
    chorus._ledger.tasks.entries = (_Task("target", TaskStatus.DONE),)
    chorus._event_bus.emit(_Event(task_id="sibling"))

    try:
        await asyncio.wait_for(asyncio.shield(run), timeout=0.05)
    except TimeoutError:
        pass
    else:
        raise AssertionError("an unrelated event completed the run")

    run.cancel()
    with suppress(asyncio.CancelledError):
        await run
    assert chorus._event_bus.unsubscription_count == 1


async def test_subscribe_before_first_terminal_read_closes_completion_race() -> None:
    tasks = _Tasks((_Task("target", TaskStatus.IN_PROGRESS),))
    bus = _EventBus()
    chorus = _Chorus(_Ledger(tasks), bus)
    host = _Host(_Runtime(_Graph(chorus)))
    executor = ChorusRunExecutor(cast(CompanyGraphHost, host), max_ticks=1)

    def complete_during_subscribe() -> None:
        tasks.entries = (_Task("target", TaskStatus.DONE),)
        bus.emit(_Event(task_id="target"))

    bus.on_subscribe = complete_during_subscribe
    result = await asyncio.wait_for(
        executor.execute(
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            directive="d",
            is_canceled=_not_canceled,
            engine_task_id="target",
        ),
        timeout=0.1,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert bus.unsubscription_count == 1
