"""API-key minting and resolution. Tokens are random; only their sha256 hash is persisted."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from podium.auth._models import ApiKey

_TOKEN_PREFIX = "pk_"


def generate_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_key(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    company_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> tuple[ApiKey, str]:
    """Create a key and return (row, raw_token). The raw token is returned ONCE — never stored."""
    token = generate_token()
    key = ApiKey(
        workspace_id=workspace_id,
        company_id=company_id,
        user_id=user_id,
        key_hash=hash_token(token),
        prefix=token[:11],
        name=name,
    )
    session.add(key)
    await session.flush()  # DB mints the uuidv7 id; RETURNING fills it
    return key, token


async def resolve_api_key(session: AsyncSession, token: str | None) -> ApiKey | None:
    """Look up a live key by token. Returns None for missing/revoked — never raises on bad input."""
    if not token:
        return None
    stmt = select(ApiKey).where(ApiKey.key_hash == hash_token(token), ApiKey.revoked_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()
