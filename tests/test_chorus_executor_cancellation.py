"""Task-scoped cancellation at the Podium → Chorus boundary."""

from __future__ import annotations

import uuid
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
class _Ledger:
    tasks: _Tasks


class _Chorus:
    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger
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


async def _no_sleep(_: float) -> None:
    return None


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


async def test_timeout_cancels_the_engine_task_before_returning_timed_out() -> None:
    executor, chorus, _host = _executor(_Task("target", TaskStatus.IN_PROGRESS))

    with patch("asyncio.sleep", new=_no_sleep):
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
