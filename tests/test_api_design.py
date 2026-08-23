"""API contract: RFC 9457 errors, request correlation, and response headers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import SlidingWindowRateLimiter, create_api_key
from podium.http_errors import PROBLEM_MEDIA_TYPE, REQUEST_ID_HEADER
from podium.main import create_app
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


async def _ws_key(admin: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return ws.id, token


async def test_duplicate_company_slug_is_409_conflict(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"slug": "acme", "name": "Acme"}
    assert (
        await api.post(f"/v1/workspaces/{ws_id}/companies", json=body, headers=headers)
    ).status_code == 201
    conflict = await api.post(f"/v1/workspaces/{ws_id}/companies", json=body, headers=headers)
    assert conflict.status_code == 409
    problem = conflict.json()
    assert problem["type"] == "urn:podium:problem:conflict"
    assert problem["detail"] == "resource conflict"
    assert "duplicate key" not in problem["detail"]
    assert conflict.headers["content-type"] == PROBLEM_MEDIA_TYPE


async def test_not_found_uses_problem_details(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{uuid4()}",  # well-formed but absent -> 404
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    problem = resp.json()
    assert problem["type"] == "urn:podium:problem:not_found"
    assert problem["detail"] == "company not found"
    assert isinstance(problem["trace_id"], str)


async def test_validation_error_problem_has_typed_field_details(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme"},  # missing "name"
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    problem = resp.json()
    assert problem["type"] == "urn:podium:problem:validation_error"
    assert any(error["field"] == "name" for error in problem["errors"])


async def test_created_company_returns_location_header(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme", "name": "Acme"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.headers["Location"] == f"/v1/workspaces/{ws_id}/companies/{resp.json()['id']}"
    assert UUID(resp.headers[REQUEST_ID_HEADER])


async def test_company_response_omits_internal_config(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.post(
        f"/v1/workspaces/{ws_id}/companies",
        json={"slug": "acme", "name": "Acme", "config": {"secret": "shh"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert "config" not in resp.json()  # internal config never echoed


async def test_rate_limited_response_sets_retry_after(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.rate_limiter = SlidingWindowRateLimiter(
        max_requests=1, window_seconds=60, clock=lambda: 0.0
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
        limited = await client.get(f"/v1/workspaces/{ws_id}/companies", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["type"] == "urn:podium:problem:rate_limit_exceeded"
    assert limited.headers["Retry-After"] == "60"


async def test_problem_echoes_valid_opaque_request_id(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    request_id = "req_company-create_42"
    resp = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{uuid4()}",
        headers={"Authorization": f"Bearer {token}", REQUEST_ID_HEADER: request_id},
    )
    assert resp.headers[REQUEST_ID_HEADER] == request_id
    assert resp.json()["trace_id"] == request_id


async def test_problem_generates_request_id_for_invalid_header(
    api: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ws_id, token = await _ws_key(sessionmaker)
    resp = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{uuid4()}",
        headers={"Authorization": f"Bearer {token}", REQUEST_ID_HEADER: "not a request id"},
    )
    request_id = resp.headers[REQUEST_ID_HEADER]
    assert request_id == resp.json()["trace_id"]
    assert request_id != "not a request id"
    assert str(UUID(request_id)) == request_id


async def test_openapi_exposes_problem_details_for_error_responses(api: httpx.AsyncClient) -> None:
    schema = (await api.get("/openapi.json")).json()
    problem_schema = schema["components"]["schemas"]["ProblemDetails"]
    assert problem_schema["properties"]["trace_id"]["maxLength"] == 128
    response = schema["paths"]["/v1/workspaces/{workspace_id}/companies"]["post"]["responses"][
        "409"
    ]
    assert response["content"][PROBLEM_MEDIA_TYPE]["schema"] == {
        "$ref": "#/components/schemas/ProblemDetails"
    }
    created = schema["paths"]["/v1/workspaces/{workspace_id}/companies"]["post"]["responses"][
        "201"
    ]
    assert "application/json" in created["content"]
    assert PROBLEM_MEDIA_TYPE not in created["content"]
