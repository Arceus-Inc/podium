"""Minimal workspace create path (no HTTP router yet — that lands with M1 companies CRUD)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from podium.workspaces.models import Workspace


def _mint_id() -> str:
    # ponytail: uuid4, not a ULID lib. Swap to prefixed-ULID when time-sortable ids actually matter.
    return f"ws_{uuid4().hex}"


async def create_workspace(session: AsyncSession, *, name: str, slug: str) -> Workspace:
    """Insert a workspace and return it. Uniqueness of `slug` is enforced by the DB constraint."""
    workspace = Workspace(id=_mint_id(), name=name, slug=slug)
    session.add(workspace)
    await session.flush()  # assign/validate within the caller's transaction; caller commits
    return workspace
