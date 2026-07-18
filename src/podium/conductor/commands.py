"""Command mailbox operations — enqueue a signal, and consume it exactly once (idempotent)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from podium.conductor.models import Command

_CONDUCTOR_CHANNEL = "podium_conductor"


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue_command(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    type: str,
    run_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Command:
    command = Command(
        workspace_id=workspace_id,
        company_id=company_id,
        run_id=run_id,
        type=type,
        payload=payload or {},
    )
    session.add(command)
    await session.flush()  # DB mints the uuidv7 id; RETURNING fills it
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": _CONDUCTOR_CHANNEL, "payload": str(company_id)},
    )
    return command


async def pending_commands(session: AsyncSession, company_id: uuid.UUID) -> Sequence[Command]:
    """Unconsumed commands for a company, oldest first (the conductor drains these in M2b)."""
    stmt = (
        select(Command)
        .where(Command.company_id == company_id, Command.consumed_at.is_(None))
        .order_by(Command.created_at)
    )
    return (await session.execute(stmt)).scalars().all()


async def mark_consumed(session: AsyncSession, command_id: uuid.UUID) -> bool:
    """Claim a command. False if it was already consumed — the guard makes consumption idempotent."""
    stmt = (
        update(Command)
        .where(Command.id == command_id, Command.consumed_at.is_(None))
        .values(consumed_at=_now())
        .returning(Command.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
