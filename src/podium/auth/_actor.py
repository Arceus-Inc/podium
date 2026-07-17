"""The authenticated `Actor`, the typed `Resource` it acts on, and how a credential becomes one."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from podium.auth._apikey import resolve_api_key


@dataclass(frozen=True)
class Actor:
    workspace_id: uuid.UUID
    company_id: uuid.UUID | None  # None = workspace-wide key; set = scoped to one company
    actor_type: str  # "service" in M1b-1; "user" arrives with the users table
    actor_id: uuid.UUID  # the user id (user keys) or the api-key id (service keys)


@dataclass(frozen=True)
class Resource:
    kind: str  # "company" | "workspace"
    workspace_id: uuid.UUID
    company_id: uuid.UUID | None = None


async def resolve_actor(session: AsyncSession, token: str | None) -> Actor | None:
    """Resolve a bearer token to an Actor, or None if the credential is missing/invalid/revoked."""
    key = await resolve_api_key(session, token)
    if key is None:
        return None
    if key.user_id is not None:
        return Actor(
            workspace_id=key.workspace_id,
            company_id=key.company_id,
            actor_type="user",
            actor_id=key.user_id,
        )
    return Actor(
        workspace_id=key.workspace_id,
        company_id=key.company_id,
        actor_type="service",
        actor_id=key.id,
    )
