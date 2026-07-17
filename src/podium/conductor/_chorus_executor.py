"""The real executor: drive a live `CompanyGraph` (company.build) with the chorus heartbeat, and
mirror its EventBus into the product event log.

Per company the host holds one runtime: the graph, a per-company `EventMirror`, and an `EventIngest`
subscribed to the graph's EventBus. The executor submits the directive, records the chorus root task
(`engine_task_id` + `register_run`) so events route to the run, then pulses `tick()`+`drain()` until
the task is terminal. All model/LLM + event work rides the company's own ledger/bus; the product DB
is touched only for short mirror writes.

Podium ids are uuids; chorus ids (task/employee) are chorus-minted text until the M5.2 engine port.
The uuid→str conversions at `CompanyConfig`/workdir are that boundary, made explicit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chorus.ledger._models import TaskStatus
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from company import CompanyConfig, CompanyGraph, build
from podium.companies import mark_company_idle
from podium.conductor._executor import CancelCheck, ExecutionResult
from podium.conductor._ingest import EventIngest
from podium.conductor._mirror import EventMirror
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.runs import RunStatus, set_engine_task_id

_TERMINAL: dict[TaskStatus, RunStatus] = {
    TaskStatus.DONE: RunStatus.SUCCEEDED,
    TaskStatus.CANCELLED: RunStatus.CANCELED,
    TaskStatus.REJECTED: RunStatus.FAILED,
}


@dataclass
class _CompanyRuntime:
    graph: CompanyGraph
    assignee: str
    mirror: EventMirror
    ingest: EventIngest


class CompanyGraphHost:
    """One live runtime per company: graph + event mirror + bus ingest, built on first use."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        deployment: str,
        workdir: Path,
        app_sessionmaker: async_sessionmaker[AsyncSession],
        log_store: RunLogStore,
        engine_ledger_dsn: str = "",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._deployment = deployment
        self._workdir = workdir
        self._app_sm = app_sessionmaker
        self._log_store = log_store
        self._engine_ledger_dsn = engine_ledger_dsn
        self._runtimes: dict[uuid.UUID, _CompanyRuntime] = {}

    async def ensure(self, company_id: uuid.UUID, workspace_id: uuid.UUID) -> _CompanyRuntime:
        existing = self._runtimes.get(company_id)
        if existing is not None:
            return existing
        graph = build(
            CompanyConfig(
                api_key=self._api_key,
                base_url=self._base_url,
                deployment=self._deployment,
                workdir=self._workdir / str(company_id),  # chorus boundary: uuid → canonical text
                company_id=str(company_id),
                ledger_dsn=self._engine_ledger_dsn,
            )
        )
        # ponytail: one hardcoded worker to make runs executable; M4 provisioning sets the real
        # workforce from the company config. Idempotent: a saga retry (built, idle-flip failed)
        # finds the worker already hired in the engine store.
        worker = graph.org._ledger.employees.get("ace") or graph.org.hire(
            name="Ace", role="backend_engineer"
        )
        mirror = EventMirror(
            self._app_sm,
            company_id=company_id,
            workspace_id=workspace_id,
            log_store=self._log_store,
        )
        await mirror.rehydrate()  # pick up runs already in flight from a prior conductor
        ingest = EventIngest(graph.org._event_bus, mirror, resolve_root=_root_resolver(graph))
        ingest.start()
        runtime = _CompanyRuntime(graph=graph, assignee=worker.name, mirror=mirror, ingest=ingest)
        # The provisioning saga's happy edge: the graph built and the engine store is live, so the
        # company leaves `provisioning`. Any failure up to and INCLUDING the flip leaves the
        # company provisioning and the runtime uncached — the next ensure genuinely retries.
        try:
            async with tenant_session(self._app_sm, workspace_id) as session:
                await mark_company_idle(session, company_id)
        except BaseException:
            await ingest.stop()
            graph.close()
            raise
        self._runtimes[company_id] = runtime
        return runtime

    async def attach_run(
        self,
        runtime: _CompanyRuntime,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        engine_task_id: str,
    ) -> None:
        """Bind a podium run to its chorus root task — durably (the column) and in the mirror map."""
        async with tenant_session(self._app_sm, workspace_id) as session:
            await set_engine_task_id(session, run_id, engine_task_id)
        runtime.mirror.register_run(run_id=run_id, engine_task_id=engine_task_id)

    async def aclose(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.ingest.stop()
            runtime.graph.close()  # the company's live Postgres connection


def _submit_kwargs(params: dict[str, Any], *, default_assignee: str) -> dict[str, Any]:
    """Map durable run params onto org.submit kwargs — one run resource, mode discriminates."""
    if params.get("execution_mode") != "delegation":
        return {"assignee": default_assignee}
    from chorus.ledger import ExecutionMode

    kwargs: dict[str, Any] = {
        "assignee": str(params["lead"]),
        "execution_mode": ExecutionMode.DELEGATION,
        "goal_id": str(params["goal_id"]),
    }
    if params.get("max_team_size") is not None:
        kwargs["delegation_max_team_size"] = int(params["max_team_size"])
    if params.get("spend_limit_cents") is not None:
        kwargs["delegation_spend_limit_cents"] = int(params["spend_limit_cents"])
    return kwargs


def _root_resolver(graph: CompanyGraph) -> Any:
    """Map any chorus task id to its root (the run's engine_task_id) by walking parents in the ledger."""
    ledger = graph.org._ledger

    def resolve(task_id: str) -> str | None:
        task = ledger.tasks.get(task_id)
        while task is not None and task.parent_id is not None:
            parent = ledger.tasks.get(task.parent_id)
            if parent is None:
                break
            task = parent
        return task.id if task is not None else None

    return resolve


class ChorusRunExecutor:
    def __init__(self, host: CompanyGraphHost, *, max_ticks: int = 60) -> None:
        self._host = host
        self._max_ticks = max_ticks

    async def execute(
        self,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        company_id: uuid.UUID,
        directive: str,
        is_canceled: CancelCheck,
        params: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        runtime = await self._host.ensure(company_id, workspace_id)
        task = runtime.graph.org.submit(
            directive, **_submit_kwargs(params or {}, default_assignee=runtime.assignee)
        )
        await self._host.attach_run(
            runtime, run_id=run_id, workspace_id=workspace_id, engine_task_id=task.id
        )
        for _ in range(self._max_ticks):
            if await is_canceled():
                return ExecutionResult(status=RunStatus.CANCELED)
            await runtime.graph.org.tick()
            await runtime.graph.org.drain()
            current = runtime.graph.org._ledger.tasks.get(task.id)
            if current is not None and current.status in _TERMINAL:
                mapped = _TERMINAL[current.status]
                error = "task rejected" if mapped is RunStatus.FAILED else None
                return ExecutionResult(status=mapped, error=error)
        # ponytail: on timeout the chorus task is left in-progress (an orphan); chorus has no per-task
        # cancel today (only whole-heartbeat stop, which would kill sibling runs).
        return ExecutionResult(status=RunStatus.TIMED_OUT, error="exceeded tick budget")
