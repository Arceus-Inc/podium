"""HTTP proofs for newest-sequence-first, tenant-scoped timeline reads."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register all FK targets before ORM flushes
from podium.auth import create_api_key
from podium.companies import create_company
from podium.db import tenant_session
from podium.events.service import append_event
from podium.main import create_app
from podium.timeline import TimelineItemDraft, TimelineType, project_timeline_event
from podium.users import create_user
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_company(
    admin: async_sessionmaker[AsyncSession], *, slug: str = "timeline"
) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        owner = await create_user(
            session, workspace_id=workspace.id, email=f"owner@{slug}.test", name="Owner"
        )
        peer = await create_user(
            session, workspace_id=workspace.id, email=f"peer@{slug}.test", name="Peer"
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=owner.id,
            slug=f"{slug}-company",
            name=slug,
        )
        _, owner_token = await create_api_key(
            session, workspace_id=workspace.id, name="owner", user_id=owner.id
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer", user_id=peer.id
        )
    return workspace.id, company.id, owner_token, peer_token


async def _project_items(
    app: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    instant = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    drafts = (
        TimelineItemDraft(
            source_event_seq=1,
            category="work",
            event_type=TimelineType.WORK_TASK_CREATED,
            subject_type="task",
            subject_id="task-a",
            title="First",
            occurred_at=instant,
            actor_type="employee",
            actor_id="ada",
        ),
        TimelineItemDraft(
            source_event_seq=2,
            category="work",
            event_type=TimelineType.WORK_TASK_BLOCKED,
            subject_type="task",
            subject_id="task-b",
            title="Second",
            occurred_at=instant,
            attention=True,
            attention_state="blocked",
            actor_type="employee",
            actor_id="lin",
        ),
        TimelineItemDraft(
            source_event_seq=3,
            category="direction",
            event_type=TimelineType.DIRECTION_GOAL_ADDED,
            subject_type="goal",
            subject_id="goal-a",
            title="Older",
            occurred_at=instant - timedelta(minutes=1),
            actor_type="user",
            actor_id="owner",
        ),
    )
    async with tenant_session(app, workspace_id) as session:
        for draft in drafts:
            await append_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                seq=draft.source_event_seq,
                run_id=None,
                type="run.text",
                employee_id=draft.actor_id,
                payload={"seq": draft.source_event_seq},
                created_at=draft.occurred_at,
            )
            await project_timeline_event(
                session,
                company_id=company_id,
                workspace_id=workspace_id,
                projector="timeline-v1",
                projector_version="1",
                expected_prior_seq=draft.source_event_seq - 1,
                item=draft,
            )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_timeline_page_is_newest_sequence_first_and_keyset_safe(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _ = await _seed_company(sessionmaker)
    await _project_items(app_sessionmaker, workspace_id=workspace_id, company_id=company_id)
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}/timeline"

    first = await api.get(f"{base}?limit=1", headers=_headers(owner_token))
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [item["source_event_seq"] for item in first_body["data"]] == [3]
    assert first_body["meta"]["as_of_seq"] == 3
    assert first_body["meta"]["has_more"] is True
    assert first_body["links"]["self"].endswith("timeline?limit=1")
    assert first_body["links"]["next"] is not None

    second = await api.get(first_body["links"]["next"], headers=_headers(owner_token))
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["source_event_seq"] for item in second_body["data"]] == [2]

    third = await api.get(second_body["links"]["next"], headers=_headers(owner_token))
    assert third.status_code == 200, third.text
    assert [item["source_event_seq"] for item in third.json()["data"]] == [1]
    assert third.json()["meta"] == {"has_more": False, "next_cursor": None, "as_of_seq": 3}


async def test_timeline_filters_are_applied_and_invalid_inputs_are_problems(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _ = await _seed_company(sessionmaker, slug="filters")
    await _project_items(app_sessionmaker, workspace_id=workspace_id, company_id=company_id)
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}/timeline"

    filtered = await api.get(
        f"{base}?category=work&attention=true&actor_id=lin&subject_id=task-b",
        headers=_headers(owner_token),
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["source_event_seq"] for item in filtered.json()["data"]] == [2]

    for suffix in (
        "?cursor=not-a-cursor",
        "?category=unknown",
        "?occurred_after=2026-08-09T10:00:00%2B01:00",
        "?occurred_after=2026-08-10T10:00:00Z&occurred_before=2026-08-09T10:00:00Z",
    ):
        response = await api.get(f"{base}{suffix}", headers=_headers(owner_token))
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_timeline_detail_etag_and_ownership_opacity(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, peer_token = await _seed_company(sessionmaker, slug="detail")
    await _project_items(app_sessionmaker, workspace_id=workspace_id, company_id=company_id)
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}/timeline"
    listing = await api.get(base, headers=_headers(owner_token))
    item_id = listing.json()["data"][0]["id"]

    detail = await api.get(f"{base}/{item_id}", headers=_headers(owner_token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["id"] == item_id
    assert detail.headers["etag"].startswith('"')

    unchanged = await api.get(
        f"{base}/{item_id}",
        headers={**_headers(owner_token), "If-None-Match": detail.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers["etag"] == detail.headers["etag"]

    hidden = await api.get(f"{base}/{item_id}", headers=_headers(peer_token))
    assert hidden.status_code == 404
