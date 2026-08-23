"""Canonical run reads: owner-scoped detail and stable keyset pagination."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.db import tenant_session
from podium.main import create_app
from podium.runs import create_run
from podium.runs.models import Run
from podium.runs.schemas import RunOut, RunPage
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


async def _seed_runs(
    admin: async_sessionmaker[AsyncSession], *, count: int
) -> tuple[uuid.UUID, uuid.UUID, str, list[uuid.UUID]]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        company = await create_company(session, workspace_id=workspace.id, slug="c", name="C")
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service")
    async with tenant_session(admin, workspace.id) as session:
        run_ids: list[uuid.UUID] = []
        for index in range(count):
            run, _ = await create_run(
                session,
                workspace_id=workspace.id,
                company_id=company.id,
                directive=f"run {index}",
                idempotency_key=f"k{index}",
            )
            run_ids.append(run.id)
        await session.execute(
            update(Run)
            .where(Run.id.in_(run_ids))
            .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    return workspace.id, company.id, token, run_ids


def _runs_path(workspace_id: uuid.UUID, company_id: uuid.UUID) -> str:
    return f"/v1/workspaces/{workspace_id}/companies/{company_id}/runs"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_canonical_detail_matches_legacy_alias(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    workspace_id, company_id, token, run_ids = await _seed_runs(sessionmaker, count=1)
    canonical = await api.get(
        f"{_runs_path(workspace_id, company_id)}/{run_ids[0]}", headers=_auth(token)
    )
    legacy = await api.get(f"/v1/companies/{company_id}/runs/{run_ids[0]}", headers=_auth(token))

    assert canonical.status_code == 200
    assert legacy.status_code == 200
    assert RunOut.model_validate(canonical.json()) == RunOut.model_validate(legacy.json())


async def test_canonical_reads_hide_user_owned_company_from_workspace_peer(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        owner = await create_user(session, workspace_id=workspace.id, email="owner@a.io", name="Owner")
        peer = await create_user(session, workspace_id=workspace.id, email="peer@a.io", name="Peer")
        company = await create_company(
            session, workspace_id=workspace.id, owner_user_id=owner.id, slug="c", name="C"
        )
        _, owner_token = await create_api_key(
            session, workspace_id=workspace.id, name="owner", user_id=owner.id
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer", user_id=peer.id
        )
    async with tenant_session(sessionmaker, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="private",
            idempotency_key="private",
        )
    path = _runs_path(workspace.id, company.id)
    assert (await api.get(f"{path}/{run.id}", headers=_auth(owner_token))).status_code == 200
    assert (await api.get(f"{path}/{run.id}", headers=_auth(peer_token))).status_code == 404
    assert (await api.get(path, headers=_auth(peer_token))).status_code == 404


async def test_canonical_run_pages_have_stable_ties_without_duplicates(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    workspace_id, company_id, token, run_ids = await _seed_runs(sessionmaker, count=3)
    path = _runs_path(workspace_id, company_id)
    first_response = await api.get(f"{path}?limit=2", headers=_auth(token))
    assert first_response.status_code == 200
    first = RunPage.model_validate(first_response.json())
    assert [run.id for run in first.data] == sorted(run_ids, reverse=True)[:2]
    assert first.meta.has_more is True
    assert first.meta.next_cursor is not None
    assert first.links.self == f"{path}?limit=2"
    assert first.links.next == f"{path}?cursor={first.meta.next_cursor}&limit=2"

    second_response = await api.get(
        f"{path}?limit=2&cursor={first.meta.next_cursor}", headers=_auth(token)
    )
    assert second_response.status_code == 200
    second = RunPage.model_validate(second_response.json())
    assert second.meta.has_more is False
    assert second.meta.next_cursor is None
    assert second.links.self == f"{path}?cursor={first.meta.next_cursor}&limit=2"
    assert second.links.next is None
    returned_ids = [run.id for run in first.data + second.data]
    assert len(returned_ids) == len(set(returned_ids))
    assert set(returned_ids) == set(run_ids)


async def test_canonical_run_list_rejects_invalid_queries(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    workspace_id, company_id, token, _run_ids = await _seed_runs(sessionmaker, count=1)
    path = _runs_path(workspace_id, company_id)
    assert (await api.get(f"{path}?cursor=not-a-cursor", headers=_auth(token))).status_code == 422
    assert (await api.get(f"{path}?cursor={'a' * 513}", headers=_auth(token))).status_code == 422
    assert (await api.get(f"{path}?limit=201", headers=_auth(token))).status_code == 422
    assert (await api.get(f"{path}?unexpected=true", headers=_auth(token))).status_code == 422
