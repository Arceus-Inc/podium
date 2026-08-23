"""Real-Postgres proofs for the shared raw/product event log."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401 -- register composite foreign-key targets
from podium.companies import create_company
from podium.conductor import EventMirror
from podium.db import tenant_session
from podium.events import (
    Event,
    EventChannel,
    append_event,
    append_product_event,
    list_company_events,
    list_product_events,
)
from podium.product_events import (
    PRODUCT_EVENT_TYPES,
    ProductEventActor,
    ProductEventDraft,
    ProductEventSubject,
    ProductEventType,
)
from podium.timeline.projector import TimelineProjector
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


def _draft(event_type: ProductEventType = "timeline.item.updated") -> ProductEventDraft:
    return ProductEventDraft(
        type=event_type,
        occurred_at=datetime.now(UTC),
        actor=ProductEventActor(type="service", id="test"),
        subject=ProductEventSubject(type="timeline_item", id="item-1", version=1),
        changed_fields=("title",),
        data={"title": "A title"},
    )


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
async def test_append_rejects_nested_non_finite_json_before_flush(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    non_finite: float,
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-finite-json")

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        with pytest.raises(ValidationError, match="finite_number"):
            await append_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                run_id=None,
                type="run.text",
                employee_id=None,
                payload={"nested": [{"value": non_finite}]},
                created_at=datetime.now(UTC),
            )
        rows = await list_company_events(session, company_id, after=0, limit=10)

    assert rows == []


async def test_append_accepts_valid_nested_json(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-valid-json")
    payload = {"nested": [{"value": 1.25, "flags": [True, None], "label": "ok"}]}

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        event = await append_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            run_id=None,
            type="run.text",
            employee_id=None,
            payload=payload,
            created_at=datetime.now(UTC),
        )

    assert event.seq == 1
    assert event.payload == payload


async def test_event_migration_has_uuidv7_channel_and_composite_ownership(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-schema")
    other_workspace_id, _ = await _company(sessionmaker, slug="event-other")
    async with sessionmaker() as session, session.begin():
        row = Event(
            company_id=company_id,
            seq=1,
            workspace_id=workspace_id,
            run_id=None,
            type="run.started",
            employee_id=None,
            payload={},
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        assert row.channel == EventChannel.RAW
        assert row.event_id.version == 7

    async with sessionmaker() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    Event(
                        company_id=company_id,
                        seq=2,
                        workspace_id=other_workspace_id,
                        run_id=None,
                        type="run.started",
                        employee_id=None,
                        payload={},
                        created_at=datetime.now(UTC),
                    )
                )

        constraints = set(
            (
                await session.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'events'::regclass")
                )
            ).scalars()
        )
        channel_check = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'events'::regclass AND contype = 'c'"
            )
        )
        policy = await session.scalar(
            text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'events'")
        )

    async with tenant_session(app_sessionmaker, other_workspace_id) as session:
        hidden = await list_company_events(session, company_id, after=0, limit=10)

    assert {
        "uq_events_company_id_event_id",
        "fk_events_company_workspace",
        "fk_events_run_company_workspace",
    } <= constraints
    assert channel_check is not None and "channel" in channel_check
    assert policy is True
    assert hidden == []


async def test_product_envelope_round_trips_and_channel_filters(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-roundtrip")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        raw = await append_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            run_id=None,
            type="run.started",
            employee_id=None,
            payload={},
            created_at=datetime.now(UTC),
        )
        written = await append_product_event(
            session, company_id=company_id, workspace_id=workspace_id, draft=_draft()
        )
        raw_rows = await list_company_events(
            session, company_id, after=0, limit=10, channel=EventChannel.RAW
        )
        product_rows = await list_product_events(session, company_id, after=0, limit=10)

    assert raw.seq == 1
    assert written.seq == 2
    assert uuid.UUID(written.event_id).version == 7
    assert product_rows == (written,)
    assert [event.seq for event in raw_rows] == [raw.seq]


async def test_mixed_mirror_and_product_writers_have_one_contiguous_sequence(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-concurrent")
    first = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)
    second = EventMirror(app_sessionmaker, company_id=company_id, workspace_id=workspace_id)

    async def product(index: int) -> None:
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await append_product_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                draft=_draft("task.updated" if index % 2 else "goal.updated"),
            )

    await asyncio.gather(
        *(first.record(type="run.text", payload={"writer": "first", "index": index}) for index in range(4)),
        *(second.record(type="run.text", payload={"writer": "second", "index": index}) for index in range(4)),
        *(product(index) for index in range(4)),
    )

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        all_rows = await list_company_events(
            session, company_id, after=0, limit=100, channel=None
        )

    assert [row.seq for row in all_rows] == list(range(1, 13))
    assert len({row.event_id for row in all_rows}) == 12


async def test_rolled_back_append_reuses_its_sequence_without_a_gap(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-rollback")
    with pytest.raises(RuntimeError, match="rollback"):
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await append_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                run_id=None,
                type="run.started",
                employee_id=None,
                payload={},
                created_at=datetime.now(UTC),
            )
            raise RuntimeError("rollback")

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        committed = await append_product_event(
            session, company_id=company_id, workspace_id=workspace_id, draft=_draft()
        )
    assert committed.seq == 1


async def test_timeline_projector_excludes_every_normalized_product_event(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id = await _company(sessionmaker, slug="event-projector")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        for event_type in PRODUCT_EVENT_TYPES:
            await append_product_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                draft=_draft(event_type),
            )

    lag = await TimelineProjector(
        app_sessionmaker, company_id=company_id, workspace_id=workspace_id
    ).catch_up()
    assert lag.is_healthy
    assert lag.last_projected_seq == len(PRODUCT_EVENT_TYPES)
