"""Session checkpoint inspection is authenticated, ordered, and tenant-scoped."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from dream import RunTrace, SessionHandle
from dream.services.session_store import SessionCostSnapshot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.db import tenant_session
from podium.main import create_app
from podium.runs import DurableArtifactRef, create_run, save_run_session_checkpoint
from podium.users import create_user
from podium.workspaces import create_workspace

_SAVED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
_USAGE = SessionCostSnapshot(
    input_tokens=10,
    output_tokens=20,
    cache_read_tokens=3,
    cache_write_tokens=4,
    cost_usd=0.12,
)


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _run_with_key(
    admin: async_sessionmaker[AsyncSession],
    app: async_sessionmaker[AsyncSession],
    *,
    slug: str,
) -> tuple[UUID, UUID, UUID, str]:
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=f"{slug}-company", name=slug
        )
        _, token = await create_api_key(session, workspace_id=workspace.id, name=f"{slug} key")
    async with tenant_session(app, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="ship it",
            idempotency_key=f"{slug}-run",
        )
    return workspace.id, company.id, run.id, token


def _handle(*, session_id: str, saved_at: datetime) -> SessionHandle:
    return SessionHandle(
        session_id=session_id,
        path=Path(f"/durable/sessions/{session_id}.json"),
        working_dir="/worktree",
        schema_version=2,
        saved_at=saved_at,
        usage_delta=_USAGE,
        usage_total=_USAGE,
    )


async def _save_checkpoint(
    app: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    run_id: UUID,
    session_id: str,
    saved_at: datetime,
    name: str,
) -> None:
    async with tenant_session(app, workspace_id) as session:
        await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(session_id=session_id, saved_at=saved_at),
            trace=RunTrace(session_id=session_id, events=()),
            snapshot_ref=DurableArtifactRef(f"s3://snapshots/{name}.json"),
            trace_ref=DurableArtifactRef(f"s3://traces/{name}.jsonl"),
        )


async def test_lists_checkpoints_in_global_append_order(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace_id,
        run_id=run_id,
        session_id="session-b",
        saved_at=_SAVED_AT,
        name="first",
    )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace_id,
        run_id=run_id,
        session_id="session-a",
        saved_at=_SAVED_AT + timedelta(seconds=1),
        name="second",
    )

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    checkpoints = response.json()
    assert [checkpoint["checkpoint_id"] for checkpoint in checkpoints] == sorted(
        checkpoint["checkpoint_id"] for checkpoint in checkpoints
    )
    assert [checkpoint["session_id"] for checkpoint in checkpoints] == ["session-b", "session-a"]
    assert [checkpoint["sequence_no"] for checkpoint in checkpoints] == [1, 1]
    assert [checkpoint["snapshot_ref"] for checkpoint in checkpoints] == [
        "s3://snapshots/first.json",
        "s3://snapshots/second.json",
    ]
    assert checkpoints[0] == {
        "checkpoint_id": checkpoints[0]["checkpoint_id"],
        "workspace_id": str(workspace_id),
        "run_id": str(run_id),
        "session_id": "session-b",
        "sequence_no": 1,
        "snapshot_schema_version": 2,
        "snapshot_ref": "s3://snapshots/first.json",
        "working_dir": "/worktree",
        "saved_at": "2026-08-09T12:00:00Z",
        "usage_delta_input_tokens": 10,
        "usage_delta_output_tokens": 20,
        "usage_delta_cache_read_tokens": 3,
        "usage_delta_cache_write_tokens": 4,
        "usage_delta_cost_usd": 0.12,
        "usage_total_input_tokens": 10,
        "usage_total_output_tokens": 20,
        "usage_total_cache_read_tokens": 3,
        "usage_total_cache_write_tokens": 4,
        "usage_total_cost_usd": 0.12,
        "trace_ref": "s3://traces/first.jsonl",
        "trace_event_count": 0,
    }


async def test_lists_an_empty_tuple_for_a_visible_run(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_checkpoint_listing_requires_authentication(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _workspace_id, company_id, run_id, _token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )

    response = await api.get(f"/v1/companies/{company_id}/runs/{run_id}/session-checkpoints")

    assert response.status_code == 401


async def test_company_scoped_key_only_lists_its_own_company_run(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="alpha", slug="alpha")
        own_company = await create_company(
            session, workspace_id=workspace.id, slug="own", name="Own"
        )
        other_company = await create_company(
            session, workspace_id=workspace.id, slug="other", name="Other"
        )
        _, token = await create_api_key(
            session,
            workspace_id=workspace.id,
            company_id=own_company.id,
            name="own company key",
        )
    async with tenant_session(app_sessionmaker, workspace.id) as session:
        own_run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=own_company.id,
            directive="own run",
            idempotency_key="own-run",
        )
        other_run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=other_company.id,
            directive="other run",
            idempotency_key="other-run",
        )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace.id,
        run_id=own_run.id,
        session_id="own-session",
        saved_at=_SAVED_AT,
        name="own",
    )
    headers = {"Authorization": f"Bearer {token}"}

    own_response = await api.get(
        f"/v1/companies/{own_company.id}/runs/{own_run.id}/session-checkpoints",
        headers=headers,
    )
    other_response = await api.get(
        f"/v1/companies/{other_company.id}/runs/{other_run.id}/session-checkpoints",
        headers=headers,
    )

    assert own_response.status_code == 200, own_response.text
    assert [checkpoint["session_id"] for checkpoint in own_response.json()] == ["own-session"]
    assert other_response.status_code == 404
    assert other_response.json() == {"error": {"code": "not_found", "message": "run not found"}}


async def test_user_key_cannot_list_another_users_company_run_checkpoints(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="alpha", slug="alpha")
        user = await create_user(
            session, workspace_id=workspace.id, email="user@alpha.test", name="User"
        )
        other_user = await create_user(
            session, workspace_id=workspace.id, email="other@alpha.test", name="Other"
        )
        other_company = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=other_user.id,
            slug="other",
            name="Other",
        )
        _, token = await create_api_key(
            session, workspace_id=workspace.id, name="user key", user_id=user.id
        )
    async with tenant_session(app_sessionmaker, workspace.id) as session:
        other_run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=other_company.id,
            directive="other run",
            idempotency_key="other-run",
        )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace.id,
        run_id=other_run.id,
        session_id="other-session",
        saved_at=_SAVED_AT,
        name="other",
    )

    response = await api.get(
        f"/v1/companies/{other_company.id}/runs/{other_run.id}/session-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "run not found"}}


async def test_user_key_lists_its_own_owned_company_run_checkpoints(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="alpha", slug="alpha")
        user = await create_user(
            session, workspace_id=workspace.id, email="user@alpha.test", name="User"
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=user.id,
            slug="owned",
            name="Owned",
        )
        _, token = await create_api_key(
            session, workspace_id=workspace.id, name="user key", user_id=user.id
        )
    async with tenant_session(app_sessionmaker, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="owned run",
            idempotency_key="owned-run",
        )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace.id,
        run_id=run.id,
        session_id="owned-session",
        saved_at=_SAVED_AT,
        name="owned",
    )

    response = await api.get(
        f"/v1/companies/{company.id}/runs/{run.id}/session-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert [checkpoint["session_id"] for checkpoint in response.json()] == ["owned-session"]


async def test_service_key_lists_user_owned_company_run_checkpoints(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="alpha", slug="alpha")
        user = await create_user(
            session, workspace_id=workspace.id, email="user@alpha.test", name="User"
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=user.id,
            slug="owned",
            name="Owned",
        )
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service key")
    async with tenant_session(app_sessionmaker, workspace.id) as session:
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="owned run",
            idempotency_key="owned-run",
        )
    await _save_checkpoint(
        app_sessionmaker,
        workspace_id=workspace.id,
        run_id=run.id,
        session_id="owned-session",
        saved_at=_SAVED_AT,
        name="owned",
    )

    response = await api.get(
        f"/v1/companies/{company.id}/runs/{run.id}/session-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert [checkpoint["session_id"] for checkpoint in response.json()] == ["owned-session"]


async def test_checkpoint_paths_are_opaque_for_malformed_and_missing_identifiers(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )
    headers = {"Authorization": f"Bearer {token}"}

    responses = (
        await api.get(
            "/v1/companies/not-a-uuid/runs/not-a-uuid/session-checkpoints", headers=headers
        ),
        await api.get(
            f"/v1/companies/{company_id}/runs/{uuid4()}/session-checkpoints", headers=headers
        ),
        await api.get(
            f"/v1/companies/{uuid4()}/runs/{run_id}/session-checkpoints", headers=headers
        ),
    )

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "run not found"}}


async def test_checkpoint_paths_are_opaque_across_companies_and_workspaces(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a_workspace, _a_company, a_run, a_token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )
    _b_workspace, b_company, b_run, _b_token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="bravo"
    )
    async with sessionmaker() as session, session.begin():
        other_a_company = await create_company(
            session, workspace_id=a_workspace, slug="other-alpha", name="Other Alpha"
        )
    headers = {"Authorization": f"Bearer {a_token}"}

    cross_company = await api.get(
        f"/v1/companies/{other_a_company.id}/runs/{a_run}/session-checkpoints", headers=headers
    )
    cross_workspace = await api.get(
        f"/v1/companies/{b_company}/runs/{b_run}/session-checkpoints", headers=headers
    )

    assert cross_company.status_code == 404
    assert cross_workspace.status_code == 404
    assert (
        cross_company.json()
        == cross_workspace.json()
        == {"error": {"code": "not_found", "message": "run not found"}}
    )
