"""User access — tenant-scoped. Callers pass a `tenant_session`; RLS enforces the workspace wall."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from podium.users.models import User


async def create_user(
    session: AsyncSession, *, workspace_id: uuid.UUID, email: str, name: str
) -> User:
    user = User(workspace_id=workspace_id, email=email, name=name)
    session.add(user)
    await session.flush()
    return user


async def list_users(session: AsyncSession) -> Sequence[User]:
    """Every user the current tenant session may see. RLS scopes the rows — no WHERE needed."""
    return (await session.execute(select(User))).scalars().all()
