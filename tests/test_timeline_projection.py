"""Real-Postgres proofs for the timeline projection storage contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register all FK targets before ORM flushes
from podium.companies import create_company
from podium.db import tenant_session
from podium.events.service import append_event
from podium.timeline import (
    OccurredAtMustBeUTC,
    ProjectionCursor,
    ProjectionCursorMismatch,
    ProjectionStatus,
    ProjectionVersionMismatch,
    SourceEventNotFound,
    TimelineExclusion,
    TimelineItem,
    TimelineItemDraft,
    TimelineType,
    project_timeline_event,
)
from podium.timeline.repository import get_cursor, list_items
from podium.workspaces import create_workspace


async def _seed_company(
    admin: async_sessionmaker[AsyncSession], app: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=f"{slug}-company", name=slug
        )
    async with tenant_session(app, workspace.id) as session:
        await append_event(
            session,
            company_id=company.id,
            seq=1,
            workspace_id=workspace.id,
            run_id=None,
            type="run.started",
            employee_id="lead",
            payload={"kind": "start"},
            created_at=datetime.now(UTC),
        )
    return workspace.id, company.id


def _item(
    seq: int,
    *,
    category: str = "work",
    event_type: TimelineType = TimelineType.WORK_TASK_CREATED,
    occurred_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
) -> TimelineItemDraft:
    return TimelineItemDraft(
        source_event_seq=seq,
        category=category,
        event_type=event_type,
        subject_type="task",
        subject_id="task-1",
        title="Task created",
        summary="The task was created.",
        actor_type="employee",
        actor_id="lead",
        occurred_at=occurred_at or datetime.now(UTC),
        run_id=run_id,
    )


async def test_timeline_migration_enables_and_forces_rls(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname IN ('projection_cursors', 'timeline_items')"
                )
            )
        ).all()
        assert {(str(name), rls, force) for name, rls, force in rows} == {
            ("projection_cursors", True, True),
            ("timeline_items", True, True),
        }
        indexes = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'timeline_items' "
                    "AND indexname IN ("
                    "'ix_timeline_items_company_id_occurred_at', "
                    "'ix_timeline_items_company_id_category', "
                    "'ix_timeline_items_company_id_attention')"
                )
            )
        ).scalars()
        assert set(indexes) == {
            "ix_timeline_items_company_id_occurred_at",
            "ix_timeline_items_company_id_category",
            "ix_timeline_items_company_id_attention",
        }
        assert (
            await session.execute(text("SELECT typname FROM pg_type WHERE typname = 'timeline_type'"))
        ).scalar_one() == "timeline_type"


async def test_projection_inserts_item_and_advances_cursor_atomically(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="alpha")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        result = await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
        cursor = await get_cursor(session, company_id=company_id, projector="timeline-v1")
        rows = await list_items(session, company_id=company_id, limit=10)

    assert result.status is ProjectionStatus.APPLIED
    assert result.item is not None
    assert result.item.id is not None
    assert result.item.event_type is TimelineType.WORK_TASK_CREATED
    assert cursor is not None
    assert cursor.last_event_seq == 1
    assert [row.source_event_seq for row in rows] == [1]


async def test_exclusion_advances_cursor_without_creating_an_item(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="beta")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        result = await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            exclusion=TimelineExclusion(source_event_seq=1),
        )
        cursor = await get_cursor(session, company_id=company_id, projector="timeline-v1")
        rows = await list_items(session, company_id=company_id, limit=10)

    assert result.status is ProjectionStatus.APPLIED
    assert result.item is None
    assert cursor is not None
    assert cursor.last_event_seq == 1
    assert rows == []


async def test_duplicate_replay_is_a_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="gamma")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await append_event(
            session,
            company_id=company_id,
            seq=2,
            workspace_id=workspace_id,
            run_id=None,
            type="run.text",
            employee_id="lead",
            payload={"kind": "text"},
            created_at=datetime.now(UTC),
        )
        await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=1,
            item=_item(2),
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        replay = await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
        rows = await list_items(session, company_id=company_id, limit=10)

    assert replay.status is ProjectionStatus.REPLAYED
    assert replay.item is None
    assert [row.source_event_seq for row in rows] == [1, 2]


async def test_gap_failure_rolls_back_without_an_item_or_cursor_advance(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="delta")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await append_event(
            session,
            company_id=company_id,
            seq=2,
            workspace_id=workspace_id,
            run_id=None,
            type="run.text",
            employee_id="lead",
            payload={"kind": "text"},
            created_at=datetime.now(UTC),
        )
        await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
        with pytest.raises(ProjectionCursorMismatch):
            await project_timeline_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector="timeline-v1",
                projector_version="1",
                expected_prior_seq=0,
                item=_item(2),
            )
        cursor = await get_cursor(session, company_id=company_id, projector="timeline-v1")
        rows = await list_items(session, company_id=company_id, limit=10)

    assert cursor is not None
    assert cursor.last_event_seq == 1
    assert [row.source_event_seq for row in rows] == [1]


async def test_projector_version_mismatch_fails_without_mutation(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="version")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await project_timeline_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
        with pytest.raises(ProjectionVersionMismatch):
            await project_timeline_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector="timeline-v1",
                projector_version="2",
                expected_prior_seq=0,
                item=_item(1),
            )
        cursor = await get_cursor(session, company_id=company_id, projector="timeline-v1")
        rows = await list_items(session, company_id=company_id, limit=10)

    assert cursor is not None
    assert cursor.projector_version == "1"
    assert [row.source_event_seq for row in rows] == [1]


@pytest.mark.parametrize(
    "occurred_at",
    [
        datetime(2026, 8, 9),
        datetime(2026, 8, 9, tzinfo=timezone(timedelta(hours=1))),
    ],
)
async def test_projection_rejects_naive_or_non_utc_occurred_at(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    occurred_at: datetime,
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="timestamps")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        with pytest.raises(OccurredAtMustBeUTC):
            await project_timeline_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector="timeline-v1",
                projector_version="1",
                expected_prior_seq=0,
                item=_item(1, occurred_at=occurred_at),
            )
        assert await get_cursor(session, company_id=company_id, projector="timeline-v1") is None
        assert await list_items(session, company_id=company_id, limit=10) == []


async def test_projection_rejects_a_category_that_disagrees_with_its_type(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="vocabulary")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        with pytest.raises(ValueError, match="category"):
            _item(1, category="direction")
        assert await get_cursor(session, company_id=company_id, projector="timeline-v1") is None


async def test_item_insert_failure_rolls_back_the_cursor_cas(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _seed_company(sessionmaker, app_sessionmaker, slug="atomicity")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        with pytest.raises(IntegrityError):
            await project_timeline_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector="timeline-v1",
                projector_version="1",
                expected_prior_seq=0,
                item=_item(1, run_id=uuid.uuid4()),
            )
        assert await get_cursor(session, company_id=company_id, projector="timeline-v1") is None
        assert await list_items(session, company_id=company_id, limit=10) == []


async def test_timeline_projection_is_isolated_by_workspace_rls(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    alpha_workspace, alpha_company = await _seed_company(sessionmaker, app_sessionmaker, slug="epsilon")
    beta_workspace, _ = await _seed_company(sessionmaker, app_sessionmaker, slug="zeta")
    async with tenant_session(app_sessionmaker, alpha_workspace) as session:
        await project_timeline_event(
            session,
            company_id=alpha_company,
            workspace_id=alpha_workspace,
            projector="timeline-v1",
            projector_version="1",
            expected_prior_seq=0,
            item=_item(1),
        )
    async with tenant_session(app_sessionmaker, beta_workspace) as session:
        assert await list_items(session, company_id=alpha_company, limit=10) == []
        with pytest.raises(SourceEventNotFound):
            await project_timeline_event(
                session,
                company_id=alpha_company,
                workspace_id=alpha_workspace,
                projector="timeline-v1",
                projector_version="1",
                expected_prior_seq=0,
                item=_item(1),
            )
        assert (await session.execute(select(TimelineItem))).scalars().all() == []
        assert (await session.execute(select(ProjectionCursor))).scalars().all() == []
