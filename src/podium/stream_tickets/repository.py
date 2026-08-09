"""Persistence primitives for single-use stream tickets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from podium.stream_tickets.models import StreamTicket


@dataclass(frozen=True)
class RedeemedStreamTicketRow:
    id: uuid.UUID
    workspace_id: uuid.UUID
    company_id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID
    expires_at: datetime


async def create_stream_ticket(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID,
    ticket_hash: str,
    expires_at: datetime,
    created_at: datetime,
) -> StreamTicket:
    ticket = StreamTicket(
        workspace_id=workspace_id,
        company_id=company_id,
        actor_type=actor_type,
        actor_id=actor_id,
        ticket_hash=ticket_hash,
        expires_at=expires_at,
        created_at=created_at,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def redeem_stream_ticket(
    session: AsyncSession,
    *,
    ticket_hash: str,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID,
    now: datetime,
) -> RedeemedStreamTicketRow | None:
    stmt = (
        delete(StreamTicket)
        .where(
            StreamTicket.ticket_hash == ticket_hash,
            StreamTicket.workspace_id == workspace_id,
            StreamTicket.company_id == company_id,
            StreamTicket.actor_type == actor_type,
            StreamTicket.actor_id == actor_id,
            StreamTicket.expires_at > now,
        )
        .returning(
            StreamTicket.id,
            StreamTicket.workspace_id,
            StreamTicket.company_id,
            StreamTicket.actor_type,
            StreamTicket.actor_id,
            StreamTicket.expires_at,
        )
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    ticket_id, row_workspace_id, row_company_id, row_actor_type, row_actor_id, expires_at = row
    return RedeemedStreamTicketRow(
        id=ticket_id,
        workspace_id=row_workspace_id,
        company_id=row_company_id,
        actor_type=row_actor_type,
        actor_id=row_actor_id,
        expires_at=expires_at,
    )
