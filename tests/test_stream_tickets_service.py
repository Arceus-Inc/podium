"""Stream-ticket service: single-use redemption, tenant scope, and actor binding."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, create_api_key, resolve_actor
from podium.companies import create_company
from podium.db import tenant_session
from podium.stream_tickets import (
    StreamTicket,
    hash_stream_ticket,
    mint_stream_ticket,
    redeem_stream_ticket,
)
from podium.users import create_user
from podium.workspaces import create_workspace


async def _actor_from_token(admin: async_sessionmaker[AsyncSession], token: str) -> Actor:
    async with admin() as session:
        actor = await resolve_actor(session, token)
    assert actor is not None
    return actor


async def _seed_owned_company(
    admin: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    async with admin() as session, session.begin():
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
        _, owner_token = await create_api_key(
            session, workspace_id=workspace.id, name="owner", user_id=owner.id
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer", user_id=peer.id
        )
    return workspace.id, company.id, owner_token, peer_token


async def test_stream_ticket_redeems_once_and_replays_none(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _peer_token = await _seed_owned_company(sessionmaker)
    owner = await _actor_from_token(sessionmaker, owner_token)

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session, workspace_id=workspace_id, company_id=company_id, actor=owner
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        redeemed = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=owner,
            workspace_id=workspace_id,
            company_id=company_id,
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        replay = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=owner,
            workspace_id=workspace_id,
            company_id=company_id,
        )

    assert redeemed is not None
    assert redeemed.workspace_id == workspace_id
    assert redeemed.company_id == company_id
    assert redeemed.actor_id == owner.actor_id
    assert replay is None


async def test_stream_ticket_expiry_blocks_redemption(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _peer_token = await _seed_owned_company(sessionmaker)
    owner = await _actor_from_token(sessionmaker, owner_token)

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session, workspace_id=workspace_id, company_id=company_id, actor=owner
        )
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(StreamTicket)
            .where(StreamTicket.ticket_hash == hash_stream_ticket(minted.ticket))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        redeemed = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=owner,
            workspace_id=workspace_id,
            company_id=company_id,
        )

    assert redeemed is None


async def test_stream_ticket_concurrent_redeem_has_single_winner(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _peer_token = await _seed_owned_company(sessionmaker)
    owner = await _actor_from_token(sessionmaker, owner_token)

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session, workspace_id=workspace_id, company_id=company_id, actor=owner
        )

    async def attempt() -> uuid.UUID | None:
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            redeemed = await redeem_stream_ticket(
                session,
                ticket=minted.ticket,
                actor=owner,
                workspace_id=workspace_id,
                company_id=company_id,
            )
        return None if redeemed is None else redeemed.id

    winners = await asyncio.gather(*(attempt() for _ in range(8)))
    redeemed_ids = [winner for winner in winners if winner is not None]

    assert len(redeemed_ids) == 1


async def test_stream_ticket_is_bound_to_workspace_company_and_actor(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, peer_token = await _seed_owned_company(sessionmaker)
    owner = await _actor_from_token(sessionmaker, owner_token)
    peer = await _actor_from_token(sessionmaker, peer_token)
    async with sessionmaker() as session, session.begin():
        other_company = await create_company(
            session, workspace_id=workspace_id, slug="shared", name="Shared"
        )
        other_workspace = await create_workspace(session, name="B", slug="b")
        other_user = await create_user(
            session, workspace_id=other_workspace.id, email="other@example.com", name="Other"
        )
        _, other_token = await create_api_key(
            session, workspace_id=other_workspace.id, name="other", user_id=other_user.id
        )
    other_actor = await _actor_from_token(sessionmaker, other_token)

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        minted = await mint_stream_ticket(
            session, workspace_id=workspace_id, company_id=company_id, actor=owner
        )

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        peer_redeem = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=peer,
            workspace_id=workspace_id,
            company_id=company_id,
        )
        wrong_company = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=owner,
            workspace_id=workspace_id,
            company_id=other_company.id,
        )
    async with tenant_session(app_sessionmaker, other_workspace.id) as session:
        wrong_workspace = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=other_actor,
            workspace_id=other_workspace.id,
            company_id=company_id,
        )
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        rightful = await redeem_stream_ticket(
            session,
            ticket=minted.ticket,
            actor=owner,
            workspace_id=workspace_id,
            company_id=company_id,
        )

    assert peer_redeem is None
    assert wrong_company is None
    assert wrong_workspace is None
    assert rightful is not None


async def test_stream_ticket_row_is_rls_scoped_to_the_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, company_id, owner_token, _peer_token = await _seed_owned_company(sessionmaker)
    owner = await _actor_from_token(sessionmaker, owner_token)
    async with sessionmaker() as session, session.begin():
        other_workspace = await create_workspace(session, name="B", slug="b")

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await mint_stream_ticket(
            session, workspace_id=workspace_id, company_id=company_id, actor=owner
        )
    async with tenant_session(app_sessionmaker, other_workspace.id) as session:
        rows = (await session.execute(select(StreamTicket.id))).scalars().all()

    assert rows == []
