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

from podium.companies import mark_company_idle
from podium.conductor._executor import CancelCheck, ExecutionResult
from podium.conductor._ingest import EventIngest
from podium.conductor._mirror import EventMirror
from podium.conductor.company import CompanyConfig, CompanyGraph, build
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
    ceo: str  # formation runs route here — the one employee with governance tools
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
        # ponytail: one hardcoded worker to make runs executable; the CEO's approved workforce
        # plan materializes the real org. Idempotent: a saga retry (built, idle-flip failed)
        # finds both already hired in the engine store.
        worker = graph.org._ledger.employees.get("ace") or graph.org.hire(
            name="Ace", role="backend_engineer"
        )
        ceo = graph.org._ledger.employees.get("casey") or graph.org.hire(name="Casey", role="ceo")
        mirror = EventMirror(
            self._app_sm,
            company_id=company_id,
            workspace_id=workspace_id,
            log_store=self._log_store,
        )
        await mirror.rehydrate()  # pick up runs already in flight from a prior conductor
        ingest = EventIngest(graph.org._event_bus, mirror, resolve_root=_root_resolver(graph))
        ingest.start()
        graph.org.start()  # the always-on heartbeat: wakes and routines pulse until aclose
        runtime = _CompanyRuntime(
            graph=graph, assignee=worker.name, ceo=ceo.name, mirror=mirror, ingest=ingest
        )
        # The provisioning saga's happy edge: the graph built and the engine store is live, so the
        # company leaves `provisioning`. Any failure up to and INCLUDING the flip leaves the
        # company provisioning and the runtime uncached — the next ensure genuinely retries.
        try:
            async with tenant_session(self._app_sm, workspace_id) as session:
                await mark_company_idle(session, company_id)
        except BaseException:
            await graph.org.stop()
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
            await runtime.graph.org.stop()  # drain in-flight beats before the ledger goes away
            await runtime.ingest.stop()
            runtime.graph.close()  # the company's live Postgres connection


_FORMATION_CONTRACT = (
    "This is a FORMATION directive: form the permanent organization for the objective below — "
    "do NOT build the product yourself and do NOT write code. Call workforce_catalog_read "
    "first, then submit exactly one complete typed workforce plan via workforce_plan_propose: "
    "name each hire's profession from the catalog, its reporting line, and its "
    "responsibilities — 2-3 concrete 'when I'm relevant' ownership statements (e.g. 'owns the "
    "parser module'), which leads later use to pick assignees — and grant bounded "
    "management authority — any lead expected to delegate work needs can_lead=true, "
    "max_delegation_depth >= 1, and a max_team_size covering itself plus its reports. Keep "
    "every budget allocation bounded. The plan stays pending for a human decision; never claim "
    "anyone was hired. Then stop.\n\n## Objective\n"
)


def _effective_directive(params: dict[str, Any], directive: str) -> str:
    """Formation runs carry the engine's formation contract server-side (live 2026-07-18: a
    raw founder objective sent as-is made the CEO build the whole product personally instead
    of proposing an org — the product owns the incantation, not the founder)."""
    if params.get("execution_mode") == "formation":
        return _FORMATION_CONTRACT + directive
    return directive


def _submit_kwargs(
    params: dict[str, Any],
    *,
    default_assignee: str,
    ceo: str,
    default_goal_id: str | None = None,
) -> dict[str, Any]:
    """Map durable run params onto org.submit kwargs — one run resource, mode discriminates.

    params examples: {} (delivery via the default worker) · {"assignee": "bex"} ·
    {"execution_mode": "formation"} · {"execution_mode": "delegation", "lead": "backend_lead",
    "goal_id": "<goal uuid>", "max_team_size": 3, "spend_limit_cents": 500000}.

    OM-2 why-chain: a goal-less delivery run is parented to ``default_goal_id`` (the company's
    root goal) so every task answers "why am I doing this?"; formation serves no delivery goal.
    """
    mode = params.get("execution_mode")
    if mode == "formation":
        # Founder intent → the CEO. Its harness carries the ledger-bound workforce tools; the
        # typed proposal it leaves stays pending until a human hits the /plans doors.
        return {"assignee": ceo}
    if mode != "delegation":
        delivery: dict[str, Any] = {"assignee": str(params.get("assignee") or default_assignee)}
        goal_id = params.get("goal_id") or default_goal_id
        if goal_id is not None:
            delivery["goal_id"] = str(goal_id)
        return delivery
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


def _root_goal_id(ledger: Any) -> str | None:
    """The company's first active root goal — the default "why" for goal-less delivery runs."""
    for goal in ledger.goals.children(None):
        if goal.status == "active":
            return str(goal.id)
    return None


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
    """Submit onto the always-on heartbeat and watch the root task to a terminal state.

    ``max_ticks`` is the watch budget in ~1s polls; ``0`` means watch forever (infinite pulses —
    the company-OS mode). The heartbeat itself always runs until the host closes."""

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
        import asyncio
        import itertools

        runtime = await self._host.ensure(company_id, workspace_id)
        task = runtime.graph.org.submit(
            _effective_directive(params or {}, directive),
            **_submit_kwargs(
                params or {},
                default_assignee=runtime.assignee,
                ceo=runtime.ceo,
                default_goal_id=_root_goal_id(runtime.graph.org._ledger),
            ),
        )
        await self._host.attach_run(
            runtime, run_id=run_id, workspace_id=workspace_id, engine_task_id=task.id
        )
        budget = range(self._max_ticks) if self._max_ticks > 0 else itertools.count()
        for _ in budget:
            if await is_canceled():
                return ExecutionResult(status=RunStatus.CANCELED)
            current = runtime.graph.org._ledger.tasks.get(task.id)
            if current is not None and current.status in _TERMINAL:
                mapped = _TERMINAL[current.status]
                error = "task rejected" if mapped is RunStatus.FAILED else None
                return ExecutionResult(status=mapped, error=error)
            await asyncio.sleep(1.0)
        # ponytail: on timeout the chorus task is left in-progress (an orphan); chorus has no per-task
        # cancel today (only whole-heartbeat stop, which would kill sibling runs).
        return ExecutionResult(status=RunStatus.TIMED_OUT, error="exceeded tick budget")
