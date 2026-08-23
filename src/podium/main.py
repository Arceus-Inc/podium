"""FastAPI app: lifespan owns the engine; /healthz is liveness, /readyz proves the DB is reachable."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Response
from sqlalchemy import text

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from cockpit.router import router as cockpit_router
from cockpit.router import shell_router as cockpit_shell_router
from podium.auth import SlidingWindowRateLimiter
from podium.companies.router import router as companies_router
from podium.conductor._host import build_conductor
from podium.control import ControlPlaneProvider
from podium.control.router import router as control_router
from podium.db import make_engine, make_sessionmaker
from podium.dev import router as dev_router
from podium.evaluations.router import router as evaluations_router
from podium.events import Broadcaster
from podium.events.router import router as events_router
from podium.http_errors import cache_problem_openapi, install_error_handlers
from podium.logging import configure_logging
from podium.logs import RunLogStore
from podium.runs.router import router as runs_router
from podium.settings import get_settings


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

    broadcaster = Broadcaster.from_url(settings.database_url)
    await broadcaster.start()
    app.state.broadcaster = broadcaster
    app.state.log_store = RunLogStore(settings.log_dir)
    app.state.cockpit_workdir = settings.workdir  # semantic/episodic stores live per company here
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=settings.resolved_engine_ledger_dsn()
    )
    if settings.dev_bootstrap:  # the privileged playground-minting door — dev stacks only
        bootstrap_url = settings.conductor_control_database_url or settings.database_url
        bootstrap_engine = make_engine(bootstrap_url)
        app.state.bootstrap_engine = bootstrap_engine
        app.state.bootstrap_sessionmaker = make_sessionmaker(bootstrap_engine)
    structlog.get_logger("podium").info(
        "log_store_ready",
        log_dir=str(settings.log_dir),
        note="conductor and api must share this path or /logs 404s across hosts",
    )

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
        await broadcaster.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="podium", lifespan=lifespan)
    settings = get_settings()
    app.state.rate_limiter = SlidingWindowRateLimiter(
        max_requests=settings.rate_limit_max, window_seconds=settings.rate_limit_window_seconds
    )
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        # Readiness must actually touch the DB — a probe that lies is worse than none — and
        # prove every shipped engine delta is applied (a skipped migrate step reads not-ready).
        from chorus.ledger import load_migrations

        try:
            async with app.state.sessionmaker() as session:
                applied = {
                    row[0]
                    for row in await session.execute(
                        text("SELECT id FROM chorus_schema_migrations")
                    )
                }
        except Exception:
            response.status_code = 503
            return {"status": "unavailable"}
        pending = sorted(m.id for m in load_migrations() if m.id not in applied)
        if pending:
            response.status_code = 503
            return {"status": "unavailable", "engine_deltas": f"pending: {', '.join(pending)}"}
        return {"status": "ready", "engine_deltas": "applied"}

    app.include_router(companies_router)
    app.include_router(runs_router)
    app.include_router(events_router)
    app.include_router(evaluations_router)
    app.include_router(control_router)
    app.include_router(cockpit_shell_router)
    app.include_router(dev_router)
    app.include_router(cockpit_router)
    cache_problem_openapi(app)
    return app


app = create_app()
