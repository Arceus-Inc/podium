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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from chorus.ledger._models import TaskStatus
from horizon.model import Decision
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import mark_company_idle
from podium.conductor._executor import CancelCheck, ExecutionResult
from podium.conductor._ingest import EventIngest
from podium.conductor._mirror import EventMirror
from podium.conductor.company import CompanyConfig, CompanyGraph, build
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.runs import RunStatus, set_engine_task_id

logger = structlog.get_logger("podium.conductor")

_TERMINAL: dict[TaskStatus, RunStatus] = {
    TaskStatus.DONE: RunStatus.SUCCEEDED,
    TaskStatus.CANCELLED: RunStatus.CANCELED,
    TaskStatus.REJECTED: RunStatus.FAILED,
}


@dataclass
class _CompanyRuntime:
    graph: CompanyGraph
    assignee: str  # default delivery fallback = the CEO seat; no fake IC is pre-seeded
    ceo: str  # formation runs route here — the one employee with governance tools
    mirror: EventMirror
    ingest: EventIngest
    horizon_stop: Callable[[], None] | None = None  # unsubscribe handle for the feedback listener


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
                company_id=company_id,
                ledger_dsn=self._engine_ledger_dsn,
                # A real company runs many teams at once; the default (3) serialises an 18-person
                # org down to a trickle and starves delegated beats. Give the heartbeat room.
                max_concurrent_runs=16,
            )
        )
        # The CEO is the one always-present seat: formation routes here to propose the real
        # workforce, and an unassigned delivery run falls back here (the buck stops at the CEO)
        # until an approved org exists. No fake IC is pre-seeded — a hardcoded "ace" made the org
        # look staffed when it was not. Idempotent: a saga retry finds casey already hired.
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
        # offline-fix #1: subscribe horizon's outcome-feedback loop. Without this, the OutcomeListener
        # never binds, so goal health/score/priority never react to execution and the direction report
        # stays empty. start() returns the unsubscribe handle we release on teardown (matches horizon's
        # own company_loop_e2e, which calls horizon.start()).
        horizon_stop = graph.horizon.start()
        runtime = _CompanyRuntime(
            graph=graph,
            assignee=ceo.name,  # unassigned delivery stops at the CEO — no fake IC seeded
            ceo=ceo.name,
            mirror=mirror,
            ingest=ingest,
            horizon_stop=horizon_stop,
        )
        # The provisioning saga's happy edge: the graph built and the engine store is live, so the
        # company leaves `provisioning`. Any failure up to and INCLUDING the flip leaves the
        # company provisioning and the runtime uncached — the next ensure genuinely retries.
        try:
            async with tenant_session(self._app_sm, workspace_id) as session:
                await mark_company_idle(session, company_id)
        except BaseException:
            horizon_stop()
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

    def write_direction_report(self, runtime: _CompanyRuntime, company_id: uuid.UUID) -> None:
        """Land horizon's loop story where the cockpit door reads it (LoopReporter's consumer).

        Best-effort by design: a report that fails to write must never fail the run."""
        try:
            path = self._workdir / str(company_id) / "direction-report.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(runtime.graph.horizon.report(), encoding="utf-8")
        except Exception:  # a report that can't be written must never fail a run
            logger.warning("direction_report_write_failed", company_id=str(company_id))

    async def propose_next_direction(self, runtime: _CompanyRuntime) -> None:
        """Drive horizon's HUMAN-GATED generation funnel over the org's own outcome stream (F2).

        After a delivery run ends, horizon reflects on the landed outcomes — its ``InternalSource``
        reads the very same chorus ``OutcomeFeed`` the listener folds — scouts + analyses them, and may
        PROPOSE the next decision. Proposals are human-gated: they land in horizon's ``ProposalStore``
        and surface via the cockpit's direction door (``list_proposals`` / governance ``read_direction``);
        nothing reaches the live tree until an operator approves, exactly like the CEO workforce plan.

        Runs off the event loop (the reasoner call is blocking, the heartbeat shares this loop) and is
        best-effort — a proposal that can't be produced must never fail the run. Cheap when nothing is
        new: the evidence bus dedups, so a survey with no fresh outcomes makes no LLM call.
        """
        import asyncio

        from horizon.generation import InternalSource

        horizon = runtime.graph.horizon
        try:
            source = InternalSource(horizon._outcomes)
            await asyncio.to_thread(horizon.generate, [source])
        except Exception:  # proposing the next direction must never fail a run
            logger.warning("horizon_generate_failed")

    async def aclose(self) -> None:
        for runtime in self._runtimes.values():
            if runtime.horizon_stop is not None:
                runtime.horizon_stop()  # unbind the outcome-feedback listener
            await runtime.graph.org.stop()  # drain in-flight beats before the ledger goes away
            await runtime.ingest.stop()
            runtime.graph.close()  # the company's live Postgres connection


def _effective_directive(params: dict[str, Any], directive: str) -> str:
    """Formation runs are reframed as org-building tasks (live 2026-07-18: a raw founder objective
    sent as-is made the CEO build the whole product personally instead of proposing an org).

    The framing is a PROMPT and prompts are an employee concern, so the words live in the CEO
    employee (``chorus_employee.ceo.formation_directive``); the conductor only decides WHEN a run is
    a formation run and asks the employee for the incantation — no prompt text lives here."""
    if params.get("execution_mode") == "formation":
        from chorus_employee.ceo import formation_directive

        return formation_directive(directive)
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


def _ensure_root_goal(ledger: Any, directive: str) -> str | None:
    """Every company starts with its founder objective as the root goal (free-run #4).

    Idempotent: an existing active root wins. The objective's first sentence is the title —
    the why-chain's root is the founder's own words, never an invented label.
    """
    existing = _root_goal_id(ledger)
    if existing is not None:
        return existing
    import re
    import uuid as _uuid

    from chorus.ledger import Goal

    first_sentence = re.split(r"(?<=[.!?])\s", directive.strip(), maxsplit=1)[0]
    title = first_sentence[:200] if first_sentence else directive[:200]
    if not title:
        return None
    created = ledger.goals.create(Goal(id=str(_uuid.uuid4()), title=title))
    return str(created.id)


def _seed_horizon_direction(graph: CompanyGraph, *, root_goal_id: str, objective: str) -> None:
    """Mirror the company's root chorus goal into horizon as a live Decision + adopted Goal (F2).

    Without this, horizon's stores stay empty: its OutcomeListener has no strategy record to fold
    delivery verdicts into, its StrategyStore has no goals, and ``horizon.report()`` renders blank —
    the "what's next" brain is assembled but never driven. We do NOT duplicate the goal: chorus stays
    the source of truth for the skeleton; horizon *adopts* the existing goal id (keeping only its own
    strategy mirror) and records the horizon-native decision -> goal edge. The decision id is derived
    from the goal id so a later run re-seeds the SAME decision instead of minting a duplicate.
    Idempotent — safe to call on every run.
    """
    import re

    horizon = graph.horizon
    decision_id = f"dec-{root_goal_id}"
    if not any(state.decision.id == decision_id for state in horizon.state()):
        first_sentence = re.split(r"(?<=[.!?])\s", objective.strip(), maxsplit=1)[0]
        statement = (first_sentence or objective).strip()[:200]
        horizon.seed_decision(
            Decision(
                id=decision_id,
                statement=statement or "Founder objective",
                status="active",
                rationale="Founder objective — the company's root goal (chorus-owned).",
            )
        )
    horizon.adopt_goal(root_goal_id, decision_id=decision_id)


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
        engine_task_id: str | None = None,
    ) -> ExecutionResult:
        import asyncio
        import itertools

        runtime = await self._host.ensure(company_id, workspace_id)
        if engine_task_id is not None:
            # Reclaim (found live 2026-07-18): the run already submitted its engine root —
            # re-submitting mints a duplicate that can self-accept over an empty subtree.
            # Resume the watch on the recorded task instead.
            task_id = engine_task_id
        else:
            if (params or {}).get("execution_mode") == "formation":
                # Free-run #4: the founder objective becomes the root goal before the CEO ever
                # beats — the review has a tree, and every later run inherits the "why".
                _ensure_root_goal(runtime.graph.org._ledger, directive)
            task = runtime.graph.org.submit(
                _effective_directive(params or {}, directive),
                **_submit_kwargs(
                    params or {},
                    default_assignee=runtime.assignee,
                    ceo=runtime.ceo,
                    default_goal_id=_root_goal_id(runtime.graph.org._ledger),
                ),
            )
            task_id = task.id
            # F2: mirror the company's root goal into horizon so its loop has a live decision+goal
            # to fold outcomes into and its direction report populates. Idempotent + best-effort;
            # a reclaim (above) already ran on a graph whose horizon stores persist on disk.
            root_goal_id = _root_goal_id(runtime.graph.org._ledger)
            if root_goal_id is not None:
                try:
                    _seed_horizon_direction(
                        runtime.graph, root_goal_id=root_goal_id, objective=directive
                    )
                except Exception:  # seeding direction must never fail a run
                    logger.warning("horizon_seed_failed", company_id=str(company_id))
        await self._host.attach_run(
            runtime, run_id=run_id, workspace_id=workspace_id, engine_task_id=task_id
        )
        budget = range(self._max_ticks) if self._max_ticks > 0 else itertools.count()
        try:
            for _ in budget:
                if await is_canceled():
                    return ExecutionResult(status=RunStatus.CANCELED)
                current = runtime.graph.org._ledger.tasks.get(task_id)
                if current is not None and current.status in _TERMINAL:
                    mapped = _TERMINAL[current.status]
                    error = "task rejected" if mapped is RunStatus.FAILED else None
                    return ExecutionResult(status=mapped, error=error)
                await asyncio.sleep(1.0)
            # ponytail: on timeout the chorus task is left in-progress (an orphan); chorus has no
            # per-task cancel today (only whole-heartbeat stop, which would kill sibling runs).
            return ExecutionResult(status=RunStatus.TIMED_OUT, error="exceeded tick budget")
        finally:
            # A delivery outcome landed — let horizon reflect on it and (human-gated) propose what's
            # next. Formation runs serve no delivery goal, so they don't feed the direction funnel.
            if (params or {}).get("execution_mode") != "formation":
                await self._host.propose_next_direction(runtime)
            # However the run ends, the loop's story lands where the cockpit door reads it.
            self._host.write_direction_report(runtime, company_id)
