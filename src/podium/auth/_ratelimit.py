"""Per-actor sliding-window rate limit.

ponytail: in-memory, per-process. Correct for single-worker dev and the plan's M1 scope; move the
window store to Redis (plan's deferred item) when podium runs multiple api workers.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request

from podium.auth._actor import Actor
from podium.auth._deps import require_actor


class SlidingWindowRateLimiter:
    """Allow up to `max_requests` per key within a trailing `window_seconds`. Clock is injectable."""

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sweep_every: int = 1024,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._sweep_every = sweep_every
        self._ops = 0
        self._hits: dict[str, list[float]] = {}

    @property
    def retry_after_seconds(self) -> int:
        """Whole seconds a blocked caller should wait — the window length, rounded up."""
        return max(1, int(self._window))

    @property
    def tracked_keys(self) -> int:
        return len(self._hits)

    def allow(self, key: str) -> bool:
        self._ops += 1
        if self._ops >= self._sweep_every:  # bounded memory: drop keys with no live hits
            self._evict_stale()
            self._ops = 0
        now = self._clock()
        cutoff = now - self._window
        recent = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(recent) >= self._max:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True

    def _evict_stale(self) -> None:
        cutoff = self._clock() - self._window
        self._hits = {
            key: live
            for key, hits in self._hits.items()
            if (live := [t for t in hits if t > cutoff])
        }


def get_rate_limiter(request: Request) -> SlidingWindowRateLimiter:
    limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    return limiter


async def enforce_rate_limit(
    actor: Actor = Depends(require_actor),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> Actor:
    """Auth + rate limit in one dependency. Keyed (company|workspace, actor_type, actor_id) → 429."""
    scope = actor.company_id or actor.workspace_id
    key = f"{scope}:{actor.actor_type}:{actor.actor_id}"
    if not limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(limiter.retry_after_seconds)},
        )
    return actor
