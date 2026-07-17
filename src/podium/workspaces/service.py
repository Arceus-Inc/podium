"""Minimal workspace create path (no HTTP router yet — that lands with M1 companies CRUD)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from podium.workspaces.models import Workspace


async def create_workspace(session: AsyncSession, *, name: str, slug: str) -> Workspace:
    """Insert a workspace and return it. The DB mints the uuidv7 id; the flush's RETURNING fills it.

    Uniqueness of `slug` is enforced by the DB constraint.
    """
    workspace = Workspace(name=name, slug=slug)
    session.add(workspace)
    await session.flush()  # assign/validate within the caller's transaction; caller commits
    return workspace
