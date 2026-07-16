"""User access — tenant-scoped. Callers pass a `tenant_session`; RLS enforces the workspace wall."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from podium.users.models import User


def _mint_id() -> str:
    return f"usr_{uuid4().hex}"


async def create_user(session: AsyncSession, *, workspace_id: str, email: str, name: str) -> User:
    user = User(id=_mint_id(), workspace_id=workspace_id, email=email, name=name)
    session.add(user)
    await session.flush()
    return user


async def list_users(session: AsyncSession) -> Sequence[User]:
    """Every user the current tenant session may see. RLS scopes the rows — no WHERE needed."""
    return (await session.execute(select(User))).scalars().all()
