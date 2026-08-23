"""Persistence primitives for single-use stream tickets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, insert, select, text
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


@dataclass(frozen=True)
class CreatedStreamTicketRow:
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime


async def create_stream_ticket(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID,
    ticket_hash: str,
) -> CreatedStreamTicketRow:
    stmt = (
        insert(StreamTicket)
        .values(
            workspace_id=workspace_id,
            company_id=company_id,
            actor_type=actor_type,
            actor_id=actor_id,
            ticket_hash=ticket_hash,
            created_at=text("statement_timestamp()"),
            expires_at=text("statement_timestamp() + interval '60 seconds'"),
        )
        .returning(StreamTicket.id, StreamTicket.created_at, StreamTicket.expires_at)
    )
    row = (await session.execute(stmt)).one()
    return CreatedStreamTicketRow(id=row.id, created_at=row.created_at, expires_at=row.expires_at)


async def redeem_stream_ticket(
    session: AsyncSession,
    *,
    ticket_hash: str,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> RedeemedStreamTicketRow | None:
    stmt = (
        delete(StreamTicket)
        .where(
            StreamTicket.ticket_hash == ticket_hash,
            StreamTicket.workspace_id == workspace_id,
            StreamTicket.company_id == company_id,
            StreamTicket.expires_at > text("statement_timestamp()"),
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
        await session.execute(
            delete(StreamTicket).where(
                StreamTicket.ticket_hash == ticket_hash,
                StreamTicket.workspace_id == workspace_id,
                StreamTicket.company_id == company_id,
                StreamTicket.expires_at <= text("statement_timestamp()"),
            )
        )
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


async def delete_expired_stream_tickets(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    limit: int,
) -> int:
    expired_ids = (
        select(StreamTicket.id)
        .where(
            StreamTicket.workspace_id == workspace_id,
            StreamTicket.company_id == company_id,
            StreamTicket.expires_at <= text("statement_timestamp()"),
        )
        .order_by(StreamTicket.expires_at)
        .limit(limit)
    )
    deleted_ids = (
        await session.execute(
            delete(StreamTicket)
            .where(StreamTicket.id.in_(expired_ids))
            .returning(StreamTicket.id)
        )
    ).scalars().all()
    return len(deleted_ids)
