"""FastAPI dependencies: pull the request-scoped sessionmaker and resolve the Actor (fail-closed)."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth._actor import Actor, resolve_actor


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sessionmaker


async def require_actor(
    request: Request,
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> Actor:
    """Resolve the caller or reject. No/invalid credential → 401 — the fail-closed wall."""
    async with sessionmaker() as session:
        actor = await resolve_actor(session, _bearer_token(request))
    if actor is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return actor
