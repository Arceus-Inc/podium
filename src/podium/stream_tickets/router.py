"""Canonical company-scoped stream-ticket minting."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.db import tenant_session
from podium.stream_tickets.schemas import (
    StreamTicketCreateEnvelope,
    StreamTicketLinks,
    StreamTicketView,
)
from podium.stream_tickets.service import mint_stream_ticket

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/companies/{company_id}",
    tags=["stream-tickets"],
)


def _authorize(actor: Actor, workspace_id: uuid.UUID, company_id: uuid.UUID) -> None:
    resource = Resource(kind="company", workspace_id=workspace_id, company_id=company_id)
    if not decide(actor, "read", resource):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/stream-tickets", status_code=201, response_model=StreamTicketCreateEnvelope)
async def create(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> StreamTicketCreateEnvelope:
    if workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="company not found")
    _authorize(actor, workspace_id, company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="company not found")
        minted = await mint_stream_ticket(
            session, workspace_id=actor.workspace_id, company_id=company_id, actor=actor
        )
    return StreamTicketCreateEnvelope(
        data=StreamTicketView(ticket=minted.ticket, expires_at=minted.expires_at),
        links=StreamTicketLinks(
            stream=f"/v1/workspaces/{workspace_id}/companies/{company_id}/stream"
        ),
    )
