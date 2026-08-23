"""Per-company event mirror — raw diagnostic writer for the company's durable event log."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import tenant_session
from podium.events import Event, EventPayload, append_event
from podium.logs import RunLogStore, excerpt_payload
from podium.runs import active_engine_tasks, set_log_ref
from podium.timeline.projector import TimelineProjectionLag, TimelineProjector


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    """Engine ids are canonical uuid text in production; a non-uuid (test slug, legacy row)
    still routes by string but is not stored in the uuid column."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class EventMirror:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        company_id: uuid.UUID,
        workspace_id: uuid.UUID,
        log_store: RunLogStore | None = None,
        excerpt_chars: int = 2000,
    ) -> None:
        self._sm = sessionmaker
        self._company_id = company_id
        self._workspace_id = workspace_id
        self._log_store = log_store
        self._excerpt_chars = excerpt_chars
        # ponytail: one short id per run that has produced logs — bounded by runs on this company
        # graph's lifetime (negligible), and only an optimization (set_log_ref is guarded anyway).
        self._logged_runs: set[uuid.UUID] = set()
        # Chorus-minted task ids (text, engine context) → podium run ids (uuid).
        self._task_to_run: dict[str, uuid.UUID] = {}
        self._timeline_projector = TimelineProjector(
            sessionmaker,
            company_id=company_id,
            workspace_id=workspace_id,
        )
        self._lock = (
            asyncio.Lock()
        )  # serialise this mirror's routing and log-ref bookkeeping

    def register_run(self, *, run_id: uuid.UUID, engine_task_id: str) -> None:
        """Tell the mirror which podium run a chorus root task belongs to (for event routing)."""
        self._task_to_run[engine_task_id] = run_id

    @property
    def timeline_lag(self) -> TimelineProjectionLag:
        """The latest timeline catch-up state for this company."""
        return self._timeline_projector.lag

    async def rehydrate(self) -> None:
        """Rebuild the routing map from `runs.engine_task_id` — call on (re)host so events for a run
        that was in flight at restart are still attributed instead of falling to company-level."""
        async with self._lock:
            async with tenant_session(self._sm, self._workspace_id) as session:
                for run_id, engine_task_id in await active_engine_tasks(session, self._company_id):
                    self._task_to_run[engine_task_id] = run_id
            await self._timeline_projector.catch_up()

    async def record(
        self,
        *,
        type: str,
        payload: EventPayload,
        task_id: str | None = None,
        trace_id: str | None = None,
        employee_id: str | None = None,
        at: datetime | None = None,
    ) -> Event:
        """Persist one event with the next seq and wake the stream — atomically."""
        async with self._lock:
            async with tenant_session(self._sm, self._workspace_id) as session:
                # The trace (lineage root) is the run anchor; the beat's own task is the fallback
                # for pre-spine emitters that only name themselves.
                route_key = trace_id or task_id
                run_id = self._task_to_run.get(route_key) if route_key is not None else None
                # Blobs out: a big transcript goes to the log file; only an excerpt lands in the row.
                stored_payload = payload
                full_text: str | None = None
                if self._log_store is not None and run_id is not None:
                    stored_payload, full_text = excerpt_payload(
                        payload, max_chars=self._excerpt_chars
                    )
                event = await append_event(
                    session,
                    company_id=self._company_id,
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    type=type,
                    trace_id=_uuid_or_none(trace_id),
                    task_id=task_id,
                    employee_id=employee_id,
                    payload=stored_payload,
                    created_at=at or datetime.now(UTC),
                )
                if (
                    full_text is not None
                    and run_id is not None
                    and self._log_store is not None
                    and run_id not in self._logged_runs  # skip the write once we've pointed the run
                ):
                    # Guarded (log_ref IS NULL) too, so eviction/retry can't double-set.
                    await set_log_ref(session, run_id, self._log_store.ref(run_id))
                    self._logged_runs.add(run_id)
            # The allocator and NOTIFY are transaction-scoped in append_event. A failed commit leaves
            # no reserved sequence and no wake for another process to observe.
            if full_text is not None and run_id is not None and self._log_store is not None:
                self._log_store.append(run_id, full_text)
            # Projection begins after the raw event transaction commits. A corrupt raw event can
            # halt its materialized view, but can never undo the durable company event.
            await self._timeline_projector.catch_up()
            return event
