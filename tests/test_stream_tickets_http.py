"""Canonical stream-ticket POST contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.main import create_app
from podium.stream_tickets import STREAM_TICKET_TTL_SECONDS, StreamTicket, hash_stream_ticket
from podium.stream_tickets.schemas import StreamTicketCreateEnvelope
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _path(workspace_id: UUID, company_id: UUID) -> str:
    return f"/v1/workspaces/{workspace_id}/companies/{company_id}/stream-tickets"


async def test_canonical_stream_ticket_mint_returns_ticket_once_and_persists_only_hash(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        company = await create_company(session, workspace_id=workspace.id, slug="c", name="C")
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service")

    response = await api.post(_path(workspace.id, company.id), headers=_auth(token))

    assert response.status_code == 201, response.text
    assert "location" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    envelope = StreamTicketCreateEnvelope.model_validate(response.json())
    assert envelope.links.stream == f"/v1/workspaces/{workspace.id}/companies/{company.id}/stream"
    assert envelope.data.expires_at.utcoffset() == timedelta()

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(StreamTicket).where(
                    StreamTicket.ticket_hash == hash_stream_ticket(envelope.data.ticket)
                )
            )
        ).scalar_one()

    assert row.workspace_id == workspace.id
    assert row.company_id == company.id
    assert row.ticket_hash == hash_stream_ticket(envelope.data.ticket)
    assert row.ticket_hash != envelope.data.ticket
    assert row.expires_at == envelope.data.expires_at
    assert row.created_at.tzinfo is not None
    assert row.expires_at - row.created_at == timedelta(seconds=STREAM_TICKET_TTL_SECONDS)


async def test_stream_ticket_post_hides_company_for_wrong_workspace_path(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        company = await create_company(session, workspace_id=workspace.id, slug="c", name="C")
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service")

    response = await api.post(_path(uuid4(), company.id), headers=_auth(token))

    assert response.status_code == 404
    assert response.json()["type"] == "urn:podium:problem:not_found"


async def test_stream_ticket_post_hides_user_owned_company_from_workspace_peer(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        owner = await create_user(
            session, workspace_id=workspace.id, email="owner@example.com", name="Owner"
        )
        peer = await create_user(
            session, workspace_id=workspace.id, email="peer@example.com", name="Peer"
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            owner_user_id=owner.id,
            slug="owned",
            name="Owned",
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer", user_id=peer.id
        )

    response = await api.post(_path(workspace.id, company.id), headers=_auth(peer_token))

    assert response.status_code == 404
    assert response.json()["detail"] == "company not found"


async def test_stream_ticket_post_hides_foreign_company_from_scoped_service_key(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        allowed_company = await create_company(
            session, workspace_id=workspace.id, slug="allowed", name="Allowed"
        )
        target_company = await create_company(
            session, workspace_id=workspace.id, slug="target", name="Target"
        )
        _, token = await create_api_key(
            session, workspace_id=workspace.id, company_id=allowed_company.id, name="scoped"
        )

    response = await api.post(_path(workspace.id, target_company.id), headers=_auth(token))

    assert response.status_code == 404
    assert response.json()["detail"] == "company not found"


async def test_stream_ticket_post_rejects_access_token_query_auth(
    api: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="A", slug="a")
        company = await create_company(session, workspace_id=workspace.id, slug="c", name="C")
        _, token = await create_api_key(session, workspace_id=workspace.id, name="service")

    response = await api.post(f"{_path(workspace.id, company.id)}?access_token={token}")

    assert response.status_code == 401
    assert response.json()["type"] == "urn:podium:problem:unauthorized"


async def test_stream_ticket_openapi_exposes_typed_success_contract(
    api: httpx.AsyncClient,
) -> None:
    schema = (await api.get("/openapi.json")).json()
    created = schema["paths"][
        "/v1/workspaces/{workspace_id}/companies/{company_id}/stream-tickets"
    ]["post"]["responses"]["201"]
    assert created["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StreamTicketCreateEnvelope"
    }
