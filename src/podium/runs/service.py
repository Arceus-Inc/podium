"""Run lifecycle — every transition is a guarded (compare-and-swap) UPDATE, never read-modify-write.

Callers pass a `tenant_session`; RLS scopes every statement to the run's workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from podium.companies import get_company
from podium.runs.models import TERMINAL_STATUSES, Run, RunStatus

_CONDUCTOR_CHANNEL = "podium_conductor"


@dataclass(frozen=True)
class RunRef:
    """A queued run's identity — enough for the conductor to claim and dispatch it."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    company_id: uuid.UUID
    directive: str
    params: dict[str, object] = field(default_factory=dict)
    # Set on a run that already submitted its engine root — a reclaim resumes the watch on it
    # instead of re-submitting (found live 2026-07-18: a restart minted a duplicate root).
    engine_task_id: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


async def create_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    directive: str,
    idempotency_key: str,
    params: dict[str, object] | None = None,
) -> tuple[Run, bool]:
    """Insert a queued run, or return the existing one for a repeated key. Returns (run, created).

    The id is DB-minted (uuidv7 server default) and comes back through RETURNING.
    """
    now = _now()
    stmt = (
        pg_insert(Run)
        .values(
            workspace_id=workspace_id,
            company_id=company_id,
            directive=directive,
            idempotency_key=idempotency_key,
            params=params or {},
            status=RunStatus.QUEUED,
            counts={},
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["company_id", "idempotency_key"])
        .returning(Run.id)
    )
    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    if inserted_id is None:
        existing = (
            await session.execute(
                select(Run).where(
                    Run.company_id == company_id, Run.idempotency_key == idempotency_key
                )
            )
        ).scalar_one()
        return existing, False
    # Wake the conductor in the same transaction that created the work.
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": _CONDUCTOR_CHANNEL, "payload": str(company_id)},
    )
    run = await session.get(Run, inserted_id)
    assert run is not None
    return run, True


async def get_run(
    session: AsyncSession, run_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> Run | None:
    """Fetch a run within the tenant session, optionally enforcing company visibility."""
    run = await session.get(Run, run_id)
    if run is None or await get_company(session, run.company_id, user_id=user_id) is None:
        return None
    return run


async def list_runs(session: AsyncSession, company_id: uuid.UUID) -> Sequence[Run]:
    stmt = select(Run).where(Run.company_id == company_id).order_by(Run.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


async def claim_queued_run(
    session: AsyncSession, run_id: uuid.UUID, *, owner: str, lease_seconds: int
) -> bool:
    """Atomically move queued→running and take the lease. False = already owned (a real 409)."""
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.status == RunStatus.QUEUED)
        .values(
            status=RunStatus.RUNNING,
            owner=owner,
            lease_expires_at=_now() + timedelta(seconds=lease_seconds),
            updated_at=_now(),
        )
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def finalize_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    owner: str,
    status: RunStatus,
    error: str | None = None,
) -> bool:
    """Move a running/canceling run to a terminal status and clear the lock — owner must match."""
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.owner == owner,
            Run.status.in_([RunStatus.RUNNING, RunStatus.CANCELING]),
        )
        .values(status=status, error=error, owner=None, lease_expires_at=None, updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def renew_lease(
    session: AsyncSession, run_id: uuid.UUID, *, owner: str, lease_seconds: int
) -> bool:
    """Extend the lease on a run this worker still owns and is still running. False if it lost it."""
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.owner == owner, Run.status == RunStatus.RUNNING)
        .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds), updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def request_cancel(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Move a queued/running run to `canceling`. False if it is already terminal (or canceling)."""
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
        .values(status=RunStatus.CANCELING, updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --- conductor discovery (run cross-tenant on a privileged session) -------------------------------


async def queued_run_refs(session: AsyncSession, *, limit: int) -> list[RunRef]:
    """Queued runs awaiting a worker, oldest first. Cross-tenant — for the conductor's control-plane."""
    stmt = (
        select(
            Run.id, Run.workspace_id, Run.company_id, Run.directive, Run.params,
            Run.engine_task_id,
        )
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.created_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RunRef(
            id=r[0], workspace_id=r[1], company_id=r[2], directive=r[3], params=r[4] or {},
            engine_task_id=r[5],
        )
        for r in rows
    ]


async def expired_lease_refs(session: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(run_id, workspace_id) of running runs whose lease has lapsed — a crashed owner to reclaim."""
    stmt = select(Run.id, Run.workspace_id).where(
        Run.status == RunStatus.RUNNING, Run.lease_expires_at < _now()
    )
    return [(r[0], r[1]) for r in (await session.execute(stmt)).all()]


async def set_log_ref(session: AsyncSession, run_id: uuid.UUID, log_ref: str) -> bool:
    """Point a run at its durable log file — guarded so it's set exactly once (idempotent on retry)."""
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.log_ref.is_(None))
        .values(log_ref=log_ref, updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_engine_task_id(session: AsyncSession, run_id: uuid.UUID, engine_task_id: str) -> bool:
    """Record the chorus root task for a run (written on submit). RLS scopes it to the tenant."""
    stmt = (
        update(Run)
        .where(Run.id == run_id)
        .values(engine_task_id=engine_task_id, updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def active_engine_tasks(
    session: AsyncSession, company_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """(run_id, engine_task_id) for a company's non-terminal runs — the mirror's rehydration source."""
    stmt = select(Run.id, Run.engine_task_id).where(
        Run.company_id == company_id,
        Run.engine_task_id.is_not(None),
        Run.status.not_in(TERMINAL_STATUSES),
    )
    return [(r[0], r[1]) for r in (await session.execute(stmt)).all()]


async def reclaim_run(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Return an expired-lease run to `queued` (crash recovery). Guarded so a live owner is untouched."""
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.status == RunStatus.RUNNING,
            Run.lease_expires_at < _now(),
        )
        .values(status=RunStatus.QUEUED, owner=None, lease_expires_at=None, updated_at=_now())
        .returning(Run.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def runs_by_status(session: AsyncSession, company_id: uuid.UUID) -> dict[str, int]:
    """Run lifecycle counts for one company (the overview's product-DB half)."""
    stmt = select(Run.status, func.count()).where(Run.company_id == company_id).group_by(Run.status)
    rows = (await session.execute(stmt)).all()
    return {str(status): int(count) for status, count in rows}


async def rollup_run_counts(session: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    """Fold the run's mirrored events into ``runs.counts`` (CP-4, OBS §5).

    A projection of the spine, computed once at finalize: event volume, llm calls with token
    sums, and tool activity. Idempotent — recomputing from the same events yields the same fold.
    """
    from podium.events.models import Event

    rows = (
        await session.execute(select(Event.type, Event.payload).where(Event.run_id == run_id))
    ).all()
    counts = {
        "events": len(rows),
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 0,
        "tool_errors": 0,
    }
    for event_type, payload in rows:
        if event_type == "llm.call":
            counts["llm_calls"] += 1
            counts["input_tokens"] += int(payload.get("input_tokens", 0))
            counts["output_tokens"] += int(payload.get("output_tokens", 0))
        elif event_type == "run.tool_use":
            counts["tool_calls"] += 1
        elif event_type == "run.tool_result" and payload.get("is_error"):
            counts["tool_errors"] += 1
    await session.execute(
        update(Run).where(Run.id == run_id).values(counts=counts, updated_at=_now())
    )
    return counts
