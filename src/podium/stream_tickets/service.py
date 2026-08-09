"""Mint and atomically redeem short-lived SSE stream tickets."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from podium.auth import Actor
from podium.stream_tickets.repository import (
    create_stream_ticket,
    delete_expired_stream_tickets,
)
from podium.stream_tickets.repository import (
    redeem_stream_ticket as redeem_stream_ticket_record,
)

_STREAM_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_STREAM_TICKET_CLEANUP_LIMIT = 128
STREAM_TICKET_TTL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(UTC)


def generate_stream_ticket() -> str:
    return secrets.token_urlsafe(32)


def hash_stream_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def is_valid_stream_ticket(ticket: str) -> bool:
    return _STREAM_TICKET_PATTERN.fullmatch(ticket) is not None


@dataclass(frozen=True)
class MintedStreamTicket:
    ticket: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class RedeemedStreamTicket:
    id: uuid.UUID
    actor: Actor
    expires_at: datetime


async def mint_stream_ticket(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor,
) -> MintedStreamTicket:
    now = _now()
    ticket = generate_stream_ticket()
    await delete_expired_stream_tickets(
        session,
        workspace_id=workspace_id,
        company_id=company_id,
        now=now,
        limit=_STREAM_TICKET_CLEANUP_LIMIT,
    )
    created = await create_stream_ticket(
        session,
        workspace_id=workspace_id,
        company_id=company_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        ticket_hash=hash_stream_ticket(ticket),
        created_at=now,
    )
    return MintedStreamTicket(ticket=ticket, expires_at=created.expires_at)


async def redeem_stream_ticket(
    session: AsyncSession,
    *,
    ticket: str,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> RedeemedStreamTicket | None:
    if not is_valid_stream_ticket(ticket):
        return None
    redeemed = await redeem_stream_ticket_record(
        session,
        ticket_hash=hash_stream_ticket(ticket),
        workspace_id=workspace_id,
        company_id=company_id,
        now=_now(),
    )
    if redeemed is None:
        return None
    return RedeemedStreamTicket(
        id=redeemed.id,
        actor=Actor(
            workspace_id=redeemed.workspace_id,
            company_id=redeemed.company_id,
            actor_type=redeemed.actor_type,
            actor_id=redeemed.actor_id,
        ),
        expires_at=redeemed.expires_at,
    )
