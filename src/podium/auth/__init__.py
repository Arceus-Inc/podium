"""Auth package — API keys, the Actor/Resource model, the pure decide(), and FastAPI deps."""

from __future__ import annotations

from podium.auth._actor import Actor, Resource, resolve_actor
from podium.auth._apikey import create_api_key, generate_token, hash_token, resolve_api_key
from podium.auth._decide import decide
from podium.auth._deps import get_sessionmaker, require_actor
from podium.auth._models import ApiKey
from podium.auth._ratelimit import (
    SlidingWindowRateLimiter,
    enforce_rate_limit,
    get_rate_limiter,
)

__all__ = [
    "Actor",
    "ApiKey",
    "Resource",
    "SlidingWindowRateLimiter",
    "create_api_key",
    "decide",
    "enforce_rate_limit",
    "generate_token",
    "get_rate_limiter",
    "get_sessionmaker",
    "hash_token",
    "require_actor",
    "resolve_actor",
    "resolve_api_key",
]
