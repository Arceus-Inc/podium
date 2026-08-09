"""End-to-end proofs for the ordered durable timeline projector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401 -- register every model before ORM flushes
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import Event, append_event, list_company_events
from podium.timeline import (
    ProjectionVersionMismatch,
    TimelineType,
    project_timeline_event,
)
from podium.timeline.projector import (
    TIMELINE_PROJECTOR,
    TimelineProjector,
    UnknownTimelineEventKind,
)
from podium.timeline.repository import get_cursor, list_items
from podium.timeline.service_types import TimelineItemDraft
from podium.workspaces import create_workspace


async def _company(
    admin: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=f"{slug}-company", name=slug
        )
    return workspace.id, company.id


async def _append(
    app: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    seq: int,
    type: str,
    payload: dict[str, object],
    task_id: str | None = "task-1",
) -> None:
    async with tenant_session(app, workspace_id) as session:
        await append_event(
            session,
            company_id=company_id,
            seq=seq,
            workspace_id=workspace_id,
            run_id=None,
            type=type,
            employee_id="ada",
            task_id=task_id,
            payload=payload,
            created_at=datetime.now(UTC),
        )


async def _projected_snapshot(
    app: async_sessionmaker[AsyncSession], *, workspace_id: uuid.UUID, company_id: uuid.UUID
) -> tuple[int, list[tuple[int, TimelineType, str, bool, str | None]]]:
    async with tenant_session(app, workspace_id) as session:
        cursor = await get_cursor(session, company_id=company_id, projector=TIMELINE_PROJECTOR)
        rows = await list_items(session, company_id=company_id, limit=100)
    return (
        cursor.last_event_seq if cursor is not None else 0,
        [
            (row.source_event_seq, row.event_type, row.title, row.attention, row.attention_state)
            for row in rows
        ],
    )


async def test_rehydrate_catches_a_restart_gap_and_burst_with_explicit_exclusions(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="restart-burst")
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=1,
        type="task.created",
        payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
    )
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=2,
        type="run.text",
        payload={"text": "untrusted"},
    )
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=3,
        type="budget.hard_stop",
        payload={"gate": "dispatch"},
    )

    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)
    await mirror.rehydrate()

    cursor, rows = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )
    assert mirror.timeline_lag.is_healthy
    assert mirror.timeline_lag.lag == 0
    assert cursor == 3
    assert [(seq, event_type) for seq, event_type, *_ in rows] == [
        (1, TimelineType.WORK_TASK_CREATED),
        (3, TimelineType.COST_HARD_STOP),
    ]


async def test_invalid_event_halts_without_rolling_back_raw_commit_and_recovers_after_repair(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="halt-recovery")
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)
    await mirror.record(
        type="task.created",
        task_id="task-1",
        payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
    )
    invalid = await mirror.record(
        type="task.created",
        task_id="task-1",
        payload={"intent_excerpt": "missing typed fields"},
    )
    later = await mirror.record(type="run.text", task_id="task-1", payload={"text": "later"})

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        raw_rows = await list_company_events(session, company_id, after=0, limit=10)
    assert [row.seq for row in raw_rows] == [1, 2, 3]
    assert invalid.seq == 2
    assert later.seq == 3
    assert mirror.timeline_lag.halted_event_seq == 2
    assert mirror.timeline_lag.lag == 2

    # An operator repairs the source incident out-of-band. The projector never skips it.
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Event)
            .where(Event.company_id == company_id, Event.seq == invalid.seq)
            .values(
                payload={
                    "intent_excerpt": "Repair",
                    "priority": "high",
                    "execution_mode": "delivery",
                }
            )
        )
    await mirror.rehydrate()

    cursor, rows = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )
    assert mirror.timeline_lag.is_healthy
    assert cursor == 3
    assert [row[0] for row in rows] == [1, 2]


async def test_record_returns_after_an_ordinary_projector_failure_and_reports_durable_lag(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="ordinary-projector-failure")
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)

    with patch(
        "podium.timeline.projector.project_timeline_event",
        side_effect=RuntimeError("timeline database is unavailable"),
    ):
        event = await mirror.record(
            type="task.created",
            task_id="task-1",
            payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
        )

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        raw_rows = await list_company_events(session, company_id, after=0, limit=10)
    assert [row.seq for row in raw_rows] == [1]
    assert event.seq == 1
    assert mirror.timeline_lag.last_projected_seq == 0
    assert mirror.timeline_lag.latest_event_seq == 1
    assert mirror.timeline_lag.halted_event_seq == 1
    assert mirror.timeline_lag.failure_type == RuntimeError.__name__


async def test_failed_rebuild_keeps_the_previous_durable_projection(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="atomic-rebuild")
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)
    await mirror.record(
        type="task.created",
        task_id="task-1",
        payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
    )
    previous = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=2,
        type="task.created",
        payload={"intent_excerpt": "missing typed fields"},
    )

    rebuilt = await TimelineProjector(
        app_sessionmaker, company_id=company_id, workspace_id=workspace_id
    ).rebuild()
    after_failure = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )

    assert not rebuilt.is_healthy
    assert rebuilt.last_projected_seq == 1
    assert rebuilt.latest_event_seq == 2
    assert rebuilt.halted_event_seq == 2
    assert after_failure == previous


async def test_unknown_durable_kind_halts_at_its_sequence(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="unknown-kind")
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=1,
        type="not.a.chorus.event",
        payload={},
    )

    projector = TimelineProjector(
        app_sessionmaker, company_id=company_id, workspace_id=workspace_id
    )
    lag = await projector.catch_up()

    assert lag.halted_event_seq == 1
    assert lag.failure_type == UnknownTimelineEventKind.__name__
    assert lag.lag == 1


async def test_projector_version_mismatch_halts_even_when_current(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="version-halt")
    await _append(
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        seq=1,
        type="task.created",
        payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
    )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector=TIMELINE_PROJECTOR,
            projector_version="other",
            expected_prior_seq=0,
            item=TimelineItemDraft(
                source_event_seq=1,
                category="work",
                event_type=TimelineType.WORK_TASK_CREATED,
                subject_type="task",
                subject_id="task-1",
                title="Task created",
                occurred_at=datetime.now(UTC),
            ),
        )

    lag = await TimelineProjector(
        app_sessionmaker, company_id=company_id, workspace_id=workspace_id
    ).catch_up()

    assert lag.failure_type == ProjectionVersionMismatch.__name__
    assert not lag.is_healthy
    assert lag.last_projected_seq == 1


async def test_rebuild_from_raw_events_matches_incremental_projection(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="rebuild")
    mirror = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)
    await mirror.record(
        type="task.created",
        task_id="task-1",
        payload={"intent_excerpt": "Ship", "priority": "high", "execution_mode": "delivery"},
    )
    await mirror.record(type="run.text", task_id="task-1", payload={"text": "excluded"})
    await mirror.record(type="budget.hard_stop", task_id="task-1", payload={"gate": "dispatch"})
    incremental = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )

    rebuilt = await TimelineProjector(
        app_sessionmaker, company_id=company_id, workspace_id=workspace_id
    ).rebuild()
    replayed = await _projected_snapshot(
        app_sessionmaker, workspace_id=workspace_id, company_id=company_id
    )

    assert rebuilt.is_healthy
    assert rebuilt.lag == 0
    assert replayed == incremental
