"""Runs HTTP: idempotent create (202), read, cancel — all auth + decide + RLS scoped."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.db import tenant_session
from podium.main import create_app
from podium.runs import Run, RunStatus, claim_queued_run, finalize_run
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


async def _setup(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str, str, str]:
    """Workspace A with a company and a key; plus company B in another workspace."""
    async with admin() as s, s.begin():
        a = await create_workspace(s, name="A", slug="a")
        b = await create_workspace(s, name="B", slug="b")
        a_company = await create_company(s, workspace_id=a.id, slug="ac", name="A Co")
        b_company = await create_company(s, workspace_id=b.id, slug="bc", name="B Co")
        _, token_a = await create_api_key(s, workspace_id=a.id, name="A key")
        return token_a, a.id, a_company.id, b_company.id


async def test_create_run_is_idempotent_over_http(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"}
    first = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "queued"


async def test_canonical_create_matches_legacy_idempotency_behavior(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, workspace_id, a_company, _b = await _setup(sessionmaker)
    url = f"/v1/workspaces/{workspace_id}/companies/{a_company}/runs"
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"}
    created = await api.post(url, json={"directive": "ship it"}, headers=headers)
    replay = await api.post(url, json={"directive": "ship it"}, headers=headers)
    assert created.status_code == 202
    assert replay.status_code == 409
    assert replay.json()["type"] == "urn:podium:problem:idempotency_in_progress"
    assert replay.headers["Retry-After"] == "1"


async def test_canonical_create_hides_company_for_a_different_workspace_path(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/workspaces/{uuid4()}/companies/{a_company}/runs",
        json={"directive": "ship it"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert response.status_code == 404


async def test_canonical_create_hides_foreign_company_owner(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, workspace_id, _a_company, b_company = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/workspaces/{workspace_id}/companies/{b_company}/runs",
        json={"directive": "ship it"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert response.status_code == 404


async def test_canonical_create_hides_another_users_company_in_the_same_workspace(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        owner = await create_user(session, workspace_id=workspace.id, email="owner@a.io", name="Owner")
        peer = await create_user(session, workspace_id=workspace.id, email="peer@a.io", name="Peer")
        company = await create_company(
            session,
            workspace_id=workspace.id,
            slug="owned",
            name="Owned",
            owner_user_id=owner.id,
        )
        _, owner_token = await create_api_key(
            session, workspace_id=workspace.id, name="owner-key", user_id=owner.id
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer-key", user_id=peer.id
        )
    url = f"/v1/workspaces/{workspace.id}/companies/{company.id}/runs"
    body = {"directive": "ship it"}
    peer_response = await api.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {peer_token}", "Idempotency-Key": "k1"},
    )
    async with sessionmaker() as session:
        run_count = await session.scalar(
            select(func.count()).select_from(Run).where(Run.company_id == company.id)
        )
    owner_response = await api.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "k1"},
    )
    assert peer_response.status_code == 404
    assert run_count == 0
    assert owner_response.status_code == 202


async def test_create_run_accepts_deprecated_body_idempotency_key(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "ship it", "idempotency_key": "k1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202


async def test_create_run_rejects_missing_or_conflicting_idempotency_keys(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    missing = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    conflicting = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "ship it", "idempotency_key": "body-key"},
        headers={**headers, "Idempotency-Key": "header-key"},
    )
    assert missing.status_code == 422
    assert conflicting.status_code == 422


async def test_create_run_rejects_idempotency_keys_over_the_shared_limit(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    too_long = "k" * 129
    url = f"/v1/companies/{a_company}/runs"
    header = await api.post(
        url,
        json={"directive": "ship it"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": too_long},
    )
    body = await api.post(
        url,
        json={"directive": "ship it", "idempotency_key": too_long},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert header.status_code == 422
    assert body.status_code == 422


async def test_in_progress_idempotency_replay_returns_problem_and_retry_after(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"}
    created = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    replay = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    assert created.status_code == 202
    assert replay.status_code == 409
    assert replay.json()["type"] == "urn:podium:problem:idempotency_in_progress"
    assert replay.headers["Retry-After"] == "1"


async def test_completed_idempotency_replay_returns_existing_resource(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"}
    created = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    run_id = created.json()["id"]
    workspace_id = created.json()["workspace_id"]
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        assert await claim_queued_run(session, run_id, owner="worker", lease_seconds=60)
        assert await finalize_run(session, run_id, owner="worker", status=RunStatus.SUCCEEDED)
    replay = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id
    assert replay.headers["Idempotency-Replayed"] == "true"


async def test_idempotency_key_reuse_returns_problem(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"}
    await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "ship it"}, headers=headers)
    reuse = await api.post(f"/v1/companies/{a_company}/runs", json={"directive": "change scope"}, headers=headers)
    assert reuse.status_code == 422
    assert reuse.json()["type"] == "urn:podium:problem:idempotency_key_reuse"


async def test_run_idempotency_openapi_documents_header_and_body_deprecation(
    api: httpx.AsyncClient,
) -> None:
    schema = (await api.get("/openapi.json")).json()
    operation = schema["paths"]["/v1/companies/{company_id}/runs"]["post"]
    header = next(parameter for parameter in operation["parameters"] if parameter["name"] == "Idempotency-Key")
    assert header["in"] == "header"
    assert header["schema"]["anyOf"][0]["maxLength"] == 128
    body_schema = schema["components"]["schemas"]["RunCreate"]
    assert body_schema["properties"]["idempotency_key"]["deprecated"] is True
    assert body_schema["properties"]["idempotency_key"]["anyOf"][0]["maxLength"] == 128
    assert "Idempotency-Replayed" in operation["responses"]["200"]["headers"]
    canonical = schema["paths"]["/v1/workspaces/{workspace_id}/companies/{company_id}/runs"][
        "post"
    ]
    canonical_header = next(
        parameter for parameter in canonical["parameters"] if parameter["name"] == "Idempotency-Key"
    )
    assert canonical_header["in"] == "header"


async def test_get_run_returns_status(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers=headers,
    )
    run_id = created.json()["id"]
    got = await api.get(f"/v1/companies/{a_company}/runs/{run_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["status"] == "queued"


async def test_cancel_moves_queued_run_to_canceled(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post(
        f"/v1/companies/{a_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers=headers,
    )
    run_id = created.json()["id"]
    cancel = await api.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceled"


async def test_run_creation_on_foreign_company_is_not_found(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, _a, b_company = await _setup(sessionmaker)
    # A's key targeting B's company — RLS hides B's company from A's session → 404, no run made.
    resp = await api.post(
        f"/v1/companies/{b_company}/runs",
        json={"directive": "d", "idempotency_key": "k1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_runs_require_authentication(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    resp = await api.get(f"/v1/companies/{a_company}/runs/run_whatever")
    assert resp.status_code == 401


async def test_create_run_with_delegation_params(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """CP-3: one run resource, execution_mode discriminates (M4 §3.3) — delegation params are
    stored durably on the run and echoed back; the conductor threads them into org.submit."""
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/companies/{a_company}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "directive": "ship the launch",
            "idempotency_key": "dk1",
            "execution_mode": "delegation",
            "lead": "lea",
            "goal_id": "22222222-2222-2222-2222-222222222222",
            "max_team_size": 3,
            "spend_limit_cents": 5000,
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["params"]["execution_mode"] == "delegation"
    assert body["params"]["lead"] == "lea"
    assert body["params"]["max_team_size"] == 3


async def test_create_run_omits_unset_params_and_returns_empty_counts(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/companies/{a_company}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"directive": "ship it", "idempotency_key": "optional", "assignee": "ada"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["counts"] == {}
    assert response.json()["params"] == {"execution_mode": "delivery", "assignee": "ada"}


async def test_run_create_rejects_engine_private_fields(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/companies/{a_company}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"directive": "ship it", "idempotency_key": "private", "engine_private": "secret"},
    )
    assert response.status_code == 422


async def test_run_openapi_uses_named_typed_nested_schemas(api: httpx.AsyncClient) -> None:
    document = (await api.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    run = schemas["RunOut"]

    assert run["properties"]["counts"] == {"$ref": "#/components/schemas/RunCounts"}
    assert run["properties"]["params"] == {"$ref": "#/components/schemas/RunParams"}
    assert run["properties"]["status"] == {"$ref": "#/components/schemas/RunStatus"}
    assert schemas["RunCounts"]["additionalProperties"] is False
    assert schemas["RunParams"]["additionalProperties"] is False


async def test_create_run_delegation_requires_lead_and_goal(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token, _workspace_id, a_company, _b = await _setup(sessionmaker)
    response = await api.post(
        f"/v1/companies/{a_company}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"directive": "ship it", "idempotency_key": "dk2", "execution_mode": "delegation"},
    )
    assert response.status_code == 422  # fail at the door, not mid-conductor
