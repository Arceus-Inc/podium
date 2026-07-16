"""FastAPI app: lifespan owns the engine; /healthz is liveness, /readyz proves the DB is reachable."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import SlidingWindowRateLimiter
from podium.companies.router import router as companies_router
from podium.conductor._host import build_conductor
from podium.db import make_engine, make_sessionmaker
from podium.http_errors import install_error_handlers
from podium.logging import configure_logging
from podium.runs.router import router as runs_router
from podium.settings import Settings, get_settings


def _make_rate_limiter(settings: Settings) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        max_requests=settings.rate_limit_max, window_seconds=settings.rate_limit_window_seconds
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(json=settings.log_json)
    engine = make_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    conductor_stop: asyncio.Event | None = None
    conductor_task: asyncio.Task[None] | None = None
    conductor_close: Callable[[], Awaitable[None]] | None = None
    if settings.conductor_embedded:  # dev: run the conductor in-process, on its own engines
        conductor, conductor_close = build_conductor(settings)
        conductor_stop = asyncio.Event()
        conductor_task = asyncio.create_task(conductor.run_forever(conductor_stop))

    try:
        yield
    finally:
        if conductor_stop is not None and conductor_task is not None:
            conductor_stop.set()
            await conductor_task
        if conductor_close is not None:
            await conductor_close()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="podium", lifespan=lifespan)
    app.state.rate_limiter = _make_rate_limiter(get_settings())
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        # Readiness must actually touch the DB — a probe that lies is worse than none.
        try:
            async with app.state.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            response.status_code = 503
            return {"status": "unavailable"}
        return {"status": "ready"}

    app.include_router(companies_router)
    app.include_router(runs_router)
    return app


app = create_app()
