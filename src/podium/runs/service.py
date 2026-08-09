"""Run lifecycle — every transition is a guarded (compare-and-swap) UPDATE, never read-modify-write.

Callers pass a `tenant_session`; RLS scopes every statement to the run's workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from dream import RunTrace, SessionHandle
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from podium.runs.models import TERMINAL_STATUSES, Run, RunSessionCheckpointRow, RunStatus

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


class CheckpointSessionMismatchError(ValueError):
    """A Dream trace belongs to a different session than the snapshot handle."""


class CheckpointReplayConflictError(ValueError):
    """A checkpoint identity was replayed with different durable fields."""


@dataclass(frozen=True)
class DurableArtifactRef:
    """An immutable object-store or content-addressed reference owned outside Podium."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("durable artifact reference must not be blank")


@dataclass(frozen=True)
class RunSessionCheckpoint:
    """Podium's immutable pointer to one Dream-owned session checkpoint."""

    checkpoint_id: int
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    session_id: str
    sequence_no: int
    snapshot_schema_version: int
    snapshot_ref: str
    working_dir: str | None
    saved_at: datetime
    usage_delta_input_tokens: int
    usage_delta_output_tokens: int
    usage_delta_cache_read_tokens: int
    usage_delta_cache_write_tokens: int
    usage_delta_cost_usd: float
    usage_total_input_tokens: int
    usage_total_output_tokens: int
    usage_total_cache_read_tokens: int
    usage_total_cache_write_tokens: int
    usage_total_cost_usd: float
    trace_ref: str
    trace_event_count: int


def _now() -> datetime:
    return datetime.now(UTC)


def _checkpoint_from_row(row: RunSessionCheckpointRow) -> RunSessionCheckpoint:
    return RunSessionCheckpoint(
        checkpoint_id=row.checkpoint_id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        session_id=row.session_id,
        sequence_no=row.sequence_no,
        snapshot_schema_version=row.snapshot_schema_version,
        snapshot_ref=row.snapshot_ref,
        working_dir=row.working_dir,
        saved_at=row.saved_at,
        usage_delta_input_tokens=row.usage_delta_input_tokens,
        usage_delta_output_tokens=row.usage_delta_output_tokens,
        usage_delta_cache_read_tokens=row.usage_delta_cache_read_tokens,
        usage_delta_cache_write_tokens=row.usage_delta_cache_write_tokens,
        usage_delta_cost_usd=row.usage_delta_cost_usd,
        usage_total_input_tokens=row.usage_total_input_tokens,
        usage_total_output_tokens=row.usage_total_output_tokens,
        usage_total_cache_read_tokens=row.usage_total_cache_read_tokens,
        usage_total_cache_write_tokens=row.usage_total_cache_write_tokens,
        usage_total_cost_usd=row.usage_total_cost_usd,
        trace_ref=row.trace_ref,
        trace_event_count=row.trace_event_count,
    )


def _same_checkpoint(
    row: RunSessionCheckpointRow,
    *,
    handle: SessionHandle,
    trace: RunTrace,
    snapshot_ref: DurableArtifactRef,
    trace_ref: DurableArtifactRef,
) -> bool:
    delta = handle.usage_delta
    total = handle.usage_total
    return (
        row.snapshot_schema_version == handle.schema_version
        and row.snapshot_ref == snapshot_ref.value
        and row.working_dir == handle.working_dir
        and row.usage_delta_input_tokens == delta.input_tokens
        and row.usage_delta_output_tokens == delta.output_tokens
        and row.usage_delta_cache_read_tokens == delta.cache_read_tokens
        and row.usage_delta_cache_write_tokens == delta.cache_write_tokens
        and row.usage_delta_cost_usd == delta.cost_usd
        and row.usage_total_input_tokens == total.input_tokens
        and row.usage_total_output_tokens == total.output_tokens
        and row.usage_total_cache_read_tokens == total.cache_read_tokens
        and row.usage_total_cache_write_tokens == total.cache_write_tokens
        and row.usage_total_cost_usd == total.cost_usd
        and row.trace_ref == trace_ref.value
        and row.trace_event_count == len(trace.events)
    )


async def save_run_session_checkpoint(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    handle: SessionHandle,
    trace: RunTrace,
    snapshot_ref: DurableArtifactRef,
    trace_ref: DurableArtifactRef,
) -> RunSessionCheckpoint:
    """Append one Dream checkpoint, returning the existing row only for an exact retry."""
    if handle.session_id != trace.session_id:
        raise CheckpointSessionMismatchError(
            f"handle session {handle.session_id!r} does not match trace session {trace.session_id!r}"
        )

    # Serialize one session's append stream. The lock is held to transaction end,
    # so sequence order is also commit order for this run/session.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended(f"{run_id}:{handle.session_id}", 0))
        )
    )
    existing = (
        await session.execute(
            select(RunSessionCheckpointRow).where(
                RunSessionCheckpointRow.run_id == run_id,
                RunSessionCheckpointRow.session_id == handle.session_id,
                RunSessionCheckpointRow.saved_at == handle.saved_at,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not _same_checkpoint(
            existing,
            handle=handle,
            trace=trace,
            snapshot_ref=snapshot_ref,
            trace_ref=trace_ref,
        ):
            raise CheckpointReplayConflictError(
                f"conflicting checkpoint replay for run {run_id} session {handle.session_id!r}"
            )
        return _checkpoint_from_row(existing)

    next_sequence = (
        await session.execute(
            select(func.coalesce(func.max(RunSessionCheckpointRow.sequence_no), 0) + 1).where(
                RunSessionCheckpointRow.run_id == run_id,
                RunSessionCheckpointRow.session_id == handle.session_id,
            )
        )
    ).scalar_one()
    delta = handle.usage_delta
    total = handle.usage_total
    row = (
        await session.execute(
            pg_insert(RunSessionCheckpointRow)
            .values(
                workspace_id=workspace_id,
                run_id=run_id,
                session_id=handle.session_id,
                sequence_no=next_sequence,
                snapshot_schema_version=handle.schema_version,
                snapshot_ref=snapshot_ref.value,
                working_dir=handle.working_dir,
                saved_at=handle.saved_at,
                usage_delta_input_tokens=delta.input_tokens,
                usage_delta_output_tokens=delta.output_tokens,
                usage_delta_cache_read_tokens=delta.cache_read_tokens,
                usage_delta_cache_write_tokens=delta.cache_write_tokens,
                usage_delta_cost_usd=delta.cost_usd,
                usage_total_input_tokens=total.input_tokens,
                usage_total_output_tokens=total.output_tokens,
                usage_total_cache_read_tokens=total.cache_read_tokens,
                usage_total_cache_write_tokens=total.cache_write_tokens,
                usage_total_cost_usd=total.cost_usd,
                trace_ref=trace_ref.value,
                trace_event_count=len(trace.events),
            )
            .returning(RunSessionCheckpointRow)
        )
    ).scalar_one()
    return _checkpoint_from_row(row)


async def list_run_session_checkpoints(
    session: AsyncSession, *, run_id: uuid.UUID
) -> list[RunSessionCheckpoint]:
    """Return a run's checkpoints in append order; RLS scopes the read to its workspace."""
    rows = (
        (
            await session.execute(
                select(RunSessionCheckpointRow)
                .where(RunSessionCheckpointRow.run_id == run_id)
                .order_by(
                    RunSessionCheckpointRow.session_id,
                    RunSessionCheckpointRow.sequence_no,
                )
            )
        )
        .scalars()
        .all()
    )
    return [_checkpoint_from_row(row) for row in rows]


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


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    return await session.get(Run, run_id)


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
            Run.id,
            Run.workspace_id,
            Run.company_id,
            Run.directive,
            Run.params,
            Run.engine_task_id,
        )
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.created_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RunRef(
            id=r[0],
            workspace_id=r[1],
            company_id=r[2],
            directive=r[3],
            params=r[4] or {},
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
