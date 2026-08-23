"""Run session-state inspection is authorized and reads Chorus handles without mutation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import CompanyControlPlane, ControlPlaneProvider
from podium.db import tenant_session
from podium.main import create_app
from podium.runs import create_run, set_engine_task_id
from podium.users import create_user
from podium.workspaces import create_workspace


def _engine_dsn(database_url: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(engine_dsn=_engine_dsn(database_url))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@dataclass(slots=True)
class _CountingProvider:
    open_count: int = 0

    def read_plane(self, *, workspace_id: UUID, company_id: UUID) -> CompanyControlPlane:
        self.open_count += 1
        raise AssertionError(f"opened Chorus plane for denied company {company_id}")


@pytest_asyncio.fixture
async def guarded_api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[httpx.AsyncClient, _CountingProvider]]:
    provider = _CountingProvider()
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = provider
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, provider


async def _run_with_key(
    admin: async_sessionmaker[AsyncSession],
    app: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    engine_task_id: str | None = None,
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
        if engine_task_id is not None:
            assert await set_engine_task_id(session, run.id, engine_task_id)
    return workspace.id, company.id, run.id, token


def _seed_session(
    database_url: str,
    *,
    company_id: UUID,
    task_id: str,
    session_id: str | None = None,
    last_error: str | None = None,
) -> tuple[str, str]:
    """Create the Chorus task, beat run, and authoritative session handle through its repos."""
    from chorus.ledger import AgentSession, Ledger, SessionCost, Task
    from chorus.ledger import Run as EngineRun
    from chorus.workforce import Employee

    employee_id = "ada"
    engine_run_id = str(uuid4())
    handle_id = session_id or str(uuid4())
    ledger = Ledger.open(_engine_dsn(database_url), company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id=employee_id, name="Ada", role="backend_engineer"))
        ledger.tasks.submit(Task(id=task_id, intent="Ship session recovery"))
        ledger.runs.create(EngineRun(id=engine_run_id, employee_id=employee_id, task_id=task_id))
        ledger.agent_sessions.open(
            AgentSession(
                id=handle_id,
                dream_session_key="provider-session-key",
                employee_id=employee_id,
                task_id=task_id,
                run_id=engine_run_id,
                model="gpt-5",
                working_dir="/work/alpha",
                last_error=last_error,
                cost=SessionCost(
                    input_tokens=11,
                    output_tokens=22,
                    cache_read_tokens=3,
                    cache_write_tokens=4,
                    cost_usd=0.55,
                ),
            )
        )
    finally:
        ledger.close()
    return handle_id, engine_run_id


async def test_returns_null_session_without_an_engine_task(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="unassigned"
    )

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": str(run_id),
        "engine_task_id": None,
        "session": None,
    }


async def test_returns_null_session_when_the_task_has_no_handle(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = str(uuid4())
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="no-handle", engine_task_id=task_id
    )

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": str(run_id),
        "engine_task_id": task_id,
        "session": None,
    }


async def test_returns_the_open_recovery_handle_with_typed_totals(
    api: httpx.AsyncClient,
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = str(uuid4())
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="open", engine_task_id=task_id
    )
    handle_id, engine_run_id = _seed_session(
        database_url, company_id=company_id, task_id=task_id, last_error="working_dir_mismatch"
    )

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["engine_task_id"] == task_id
    assert body["session"] == {
        "id": handle_id,
        "task_id": task_id,
        "employee_id": "ada",
        "run_id": engine_run_id,
        "provider_session_key": "provider-session-key",
        "model": "gpt-5",
        "working_dir": "/work/alpha",
        "recovery_reason": "working_dir_mismatch",
        "status": "open",
        "cost": {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_tokens": 3,
            "cache_write_tokens": 4,
            "cost_usd": 0.55,
        },
        "created_at": body["session"]["created_at"],
        "updated_at": body["session"]["updated_at"],
    }


async def test_later_clean_beat_clears_the_recovery_reason(
    api: httpx.AsyncClient,
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from chorus.ledger import Ledger

    task_id = str(uuid4())
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="cleared", engine_task_id=task_id
    )
    handle_id, _engine_run_id = _seed_session(
        database_url, company_id=company_id, task_id=task_id, last_error="missing"
    )
    ledger = Ledger.open(_engine_dsn(database_url), company_id=str(company_id))
    try:
        ledger.agent_sessions.record_error(handle_id, None)
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["session"]["recovery_reason"] is None


@pytest.mark.parametrize(("method", "status"), [("seal", "sealed"), ("abort", "aborted")])
async def test_reads_terminal_latest_handles(
    method: str,
    status: str,
    api: httpx.AsyncClient,
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from chorus.ledger import Ledger

    task_id = str(uuid4())
    _workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug=status, engine_task_id=task_id
    )
    handle_id, _engine_run_id = _seed_session(database_url, company_id=company_id, task_id=task_id)
    ledger = Ledger.open(_engine_dsn(database_url), company_id=str(company_id))
    try:
        if method == "seal":
            ledger.agent_sessions.seal(handle_id)
        else:
            ledger.agent_sessions.abort(handle_id)
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/companies/{company_id}/runs/{run_id}/session-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["session"]["id"] == handle_id
    assert response.json()["session"]["status"] == status


async def test_unknown_persisted_recovery_reason_is_an_invariant_failure(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from chorus.errors import OrgInvariantViolation
    from chorus.ledger import Ledger

    task_id = str(uuid4())
    workspace_id, company_id, _run_id, _token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="invariant", engine_task_id=task_id
    )
    handle_id, _engine_run_id = _seed_session(database_url, company_id=company_id, task_id=task_id)
    ledger = Ledger.open(_engine_dsn(database_url), company_id=str(company_id))
    try:
        ledger.agent_sessions.record_error(handle_id, "unexpected")
    finally:
        ledger.close()

    provider = ControlPlaneProvider(engine_dsn=_engine_dsn(database_url))
    plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
    try:
        with pytest.raises(OrgInvariantViolation, match="unknown recovery reason"):
            plane.session_state.latest_for_task(task_id)
    finally:
        plane.close()


async def test_session_state_paths_are_opaque_for_invalid_and_foreign_runs(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, run_id, token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="alpha"
    )
    _other_workspace, other_company, other_run, _other_token = await _run_with_key(
        sessionmaker, app_sessionmaker, slug="bravo"
    )
    async with sessionmaker() as session, session.begin():
        other_company_in_alpha = await create_company(
            session, workspace_id=workspace_id, slug="other-alpha", name="Other Alpha"
        )
    headers = {"Authorization": f"Bearer {token}"}

    responses = (
        await api.get("/v1/companies/not-a-uuid/runs/not-a-uuid/session-state", headers=headers),
        await api.get(f"/v1/companies/{company_id}/runs/{uuid4()}/session-state", headers=headers),
        await api.get(
            f"/v1/companies/{other_company_in_alpha.id}/runs/{run_id}/session-state",
            headers=headers,
        ),
        await api.get(
            f"/v1/companies/{other_company}/runs/{other_run}/session-state", headers=headers
        ),
    )

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "run not found"}}


async def test_company_scoped_service_key_only_reads_its_company_without_opening_denied_plane(
    guarded_api: tuple[httpx.AsyncClient, _CountingProvider],
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, provider = guarded_api
    foreign_task_id = str(uuid4())
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="Scoped", slug="scoped")
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
            directive="own",
            idempotency_key="own",
        )
        other_run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=other_company.id,
            directive="other",
            idempotency_key="other",
        )
        assert await set_engine_task_id(session, other_run.id, foreign_task_id)
    headers = {"Authorization": f"Bearer {token}"}

    own_response = await client.get(
        f"/v1/companies/{own_company.id}/runs/{own_run.id}/session-state", headers=headers
    )
    denied_response = await client.get(
        f"/v1/companies/{other_company.id}/runs/{other_run.id}/session-state", headers=headers
    )

    assert own_response.status_code == 200, own_response.text
    assert own_response.json()["session"] is None
    assert denied_response.status_code == 404
    assert denied_response.json() == {"error": {"code": "not_found", "message": "run not found"}}
    assert provider.open_count == 0


async def test_user_key_cannot_read_another_users_company_or_open_chorus(
    guarded_api: tuple[httpx.AsyncClient, _CountingProvider],
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, provider = guarded_api
    task_id = str(uuid4())
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="Users", slug="users")
        user_a = await create_user(
            session, workspace_id=workspace.id, email="a@users.test", name="A"
        )
        user_b = await create_user(
            session, workspace_id=workspace.id, email="b@users.test", name="B"
        )
        company_b = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=user_b.id,
            slug="user-b",
            name="User B",
        )
        _, token_a = await create_api_key(
            session, workspace_id=workspace.id, user_id=user_a.id, name="user A key"
        )
    async with tenant_session(app_sessionmaker, workspace.id) as session:
        run_b, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company_b.id,
            directive="private",
            idempotency_key="private",
        )
        assert await set_engine_task_id(session, run_b.id, task_id)

    response = await client.get(
        f"/v1/companies/{company_b.id}/runs/{run_b.id}/session-state",
        headers={"Authorization": f"Bearer {token_a}"},
    )

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "run not found"}}
    assert provider.open_count == 0
