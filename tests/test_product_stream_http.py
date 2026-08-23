"""Canonical normalized product-event SSE stream."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from podium.auth import create_api_key
from podium.companies import create_company
from podium.db import tenant_session
from podium.events import Broadcaster, append_event, append_product_event
from podium.events.models import EventChannel
from podium.main import create_app
from podium.product_events import (
    ProductEvent,
    ProductEventActor,
    ProductEventDraft,
    ProductEventSubject,
    ProductEventType,
)
from podium.product_events._stream import product_event_stream, resolve_product_stream_actor
from podium.stream_tickets import mint_stream_ticket
from podium.workspaces import create_workspace


class _StubRequest:
    async def is_disconnected(self) -> bool:
        return False


def _draft(event_type: ProductEventType = "timeline.item.updated") -> ProductEventDraft:
    return ProductEventDraft(
        type=event_type,
        occurred_at=datetime.now(UTC),
        actor=ProductEventActor(type="service", id="test"),
        subject=ProductEventSubject(type="timeline_item", id="item-1", version=1),
        changed_fields=("title",),
        data={"title": "A title"},
    )


def _request(*, headers: dict[str, str] | None = None, query_string: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (key.lower().encode("ascii"), value.encode("utf-8"))
            for key, value in (headers or {}).items()
        ],
        "query_string": query_string.encode("ascii"),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest_asyncio.fixture
async def broadcaster(database_url: str) -> AsyncIterator[Broadcaster]:
    bc = Broadcaster.from_url(database_url)
    await bc.start()
    try:
        yield bc
    finally:
        await bc.stop()


@pytest_asyncio.fixture
async def api(
    broadcaster: Broadcaster, app_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.broadcaster = broadcaster
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_company(
    admin: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        company = await create_company(session, workspace_id=workspace.id, slug="c", name="C")
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service")
    return workspace.id, company.id, token


async def _consume_frames(
    gen: AsyncIterator[str], count: int, *, timeout: float = 5.0
) -> list[tuple[str, str, ProductEvent]]:
    frames: list[tuple[str, str, ProductEvent]] = []

    async def pump() -> None:
        async for frame in gen:
            frame_id = ""
            event_type = ""
            data = ""
            for line in frame.splitlines():
                if line.startswith("id:"):
                    frame_id = line[3:].strip()
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
            if frame_id:
                frames.append((frame_id, event_type, ProductEvent.model_validate_json(data)))
            if len(frames) >= count:
                return

    await asyncio.wait_for(pump(), timeout)
    return frames


def _stream(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    cursor: int,
) -> AsyncIterator[str]:
    return product_event_stream(
        _StubRequest(),  # type: ignore[arg-type]
        company_id=company_id,
        workspace_id=workspace_id,
        cursor=cursor,
        sessionmaker=sessionmaker,
        broadcaster=broadcaster,
    )


async def test_product_stream_replays_and_resumes_without_gaps_or_duplicates(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, _token = await _seed_company(sessionmaker)
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        first = await append_product_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            draft=_draft("timeline.item.created"),
        )
        await append_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            run_id=None,
            type="run.text",
            employee_id=None,
            payload={"raw": True},
            created_at=datetime.now(UTC),
            channel=EventChannel.RAW,
        )
        second = await append_product_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            draft=_draft("timeline.item.updated"),
        )
    gen1 = _stream(
        broadcaster,
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        cursor=0,
    )
    try:
        first_frames = await _consume_frames(gen1, 2)
    finally:
        await cast(AsyncGenerator[str, None], gen1).aclose()
    assert [frame_id for frame_id, _event, _data in first_frames] == [
        str(first.seq),
        str(second.seq),
    ]
    assert [event for _frame_id, event, _data in first_frames] == [
        "timeline.item.created",
        "timeline.item.updated",
    ]

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await append_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            run_id=None,
            type="run.done",
            employee_id=None,
            payload={},
            created_at=datetime.now(UTC),
            channel=EventChannel.RAW,
        )
        resumed = await append_product_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            draft=_draft("goal.updated"),
        )
    gen2 = _stream(
        broadcaster,
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        cursor=second.seq,
    )
    try:
        resumed_frames = await _consume_frames(gen2, 1)
    finally:
        await cast(AsyncGenerator[str, None], gen2).aclose()

    assert resumed_frames[0][0] == str(resumed.seq)
    assert resumed_frames[0][1] == "goal.updated"
    assert resumed_frames[0][2].seq == resumed.seq


async def test_product_stream_tails_live_product_events_and_ignores_raw(
    broadcaster: Broadcaster,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, _token = await _seed_company(sessionmaker)
    gen = _stream(
        broadcaster,
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
        cursor=0,
    )
    try:
        reader = asyncio.create_task(_consume_frames(gen, 1))
        await asyncio.sleep(0.2)
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await append_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                run_id=None,
                type="run.text",
                employee_id=None,
                payload={"raw": True},
                created_at=datetime.now(UTC),
                channel=EventChannel.RAW,
            )
        await asyncio.sleep(0.2)
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            product = await append_product_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                draft=_draft("task.updated"),
            )
        frames = await reader
    finally:
        await cast(AsyncGenerator[str, None], gen).aclose()

    assert frames[0][0] == str(product.seq)
    assert frames[0][1] == "task.updated"
    assert frames[0][2].type == "task.updated"


async def test_product_stream_ticket_redeems_once_then_reuse_is_unauthorized(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token = await _seed_company(sessionmaker)
    owner_id = await resolve_product_stream_actor(
        _request(headers={"Authorization": f"Bearer {owner_token}"}),
        sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
    )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session,
            workspace_id=workspace_id,
            company_id=company_id,
            actor=owner_id,
        )
    actor = await resolve_product_stream_actor(
        _request(query_string=f"ticket={minted.ticket}"),
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
    )
    assert actor.actor_id == owner_id.actor_id
    with pytest.raises(HTTPException, match="unauthorized") as excinfo:
        await resolve_product_stream_actor(
            _request(query_string=f"ticket={minted.ticket}"),
            app_sessionmaker,
            workspace_id=workspace_id,
            company_id=company_id,
        )
    assert excinfo.value.status_code == 401


async def test_product_stream_auth_rejects_reuse_cross_tenant_and_ambiguous_credentials(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, token = await _seed_company(sessionmaker)
    bearer_actor = await resolve_product_stream_actor(
        _request(headers={"Authorization": f"Bearer {token}"}),
        sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
    )
    async with sessionmaker() as session, session.begin():
        other_workspace = await create_workspace(session, name="B", slug="b")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session,
            workspace_id=workspace_id,
            company_id=company_id,
            actor=bearer_actor,
        )

    await resolve_product_stream_actor(
        _request(query_string=f"ticket={minted.ticket}"),
        app_sessionmaker,
        workspace_id=workspace_id,
        company_id=company_id,
    )

    for request in (
        _request(query_string=f"ticket={minted.ticket}"),
        _request(
            query_string=f"ticket={minted.ticket}",
            headers={"Authorization": f"Bearer {token}"},
        ),
        _request(query_string=f"ticket={minted.ticket}&access_token={token}"),
    ):
        try:
            await resolve_product_stream_actor(
                request,
                app_sessionmaker,
                workspace_id=workspace_id,
                company_id=company_id,
            )
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("expected unauthorized")

    try:
        await resolve_product_stream_actor(
            _request(query_string=f"ticket={minted.ticket}"),
            app_sessionmaker,
            workspace_id=other_workspace.id,
            company_id=company_id,
        )
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected unauthorized")


async def test_product_stream_http_rejects_ambiguous_and_query_access_token_credentials(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, token = await _seed_company(sessionmaker)
    url = f"/v1/workspaces/{workspace_id}/companies/{company_id}/stream"

    assert (await api.get(f"{url}?access_token={token}")).status_code == 401
    assert (
        await api.get(
            f"{url}?ticket=abc123_def456_ghi789_jkl012mno345pqrs",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).status_code == 401


async def test_raw_diagnostic_stream_requires_bearer_header_not_query_token(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, token = await _seed_company(sessionmaker)
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await append_event(
            session,
            company_id=company_id,
            workspace_id=workspace_id,
            run_id=None,
            type="run.text",
            employee_id=None,
            payload={"raw": True},
            created_at=datetime.now(UTC),
            channel=EventChannel.RAW,
        )
    assert (
        await api.get(f"/v1/companies/{company_id}/stream?access_token={token}")
    ).status_code == 401
