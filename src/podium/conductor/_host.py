"""One construction path for the conductor, hosted two ways: embedded (dev) or standalone (prod).

The conductor gets its OWN engines — a small privileged one for cross-tenant discovery and a
podium_app one for RLS-scoped mutations — separate from the api's request-serving pool, so a slow
run can never starve HTTP.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from podium.conductor._chorus_executor import ChorusRunExecutor, CompanyGraphHost
from podium.conductor._service import Conductor
from podium.db import make_engine, make_sessionmaker
from podium.logs import RunLogStore
from podium.settings import Settings


def build_conductor(settings: Settings) -> tuple[Conductor, Callable[[], Awaitable[None]]]:
    """Return (conductor, aclose). `aclose` disposes both engines on shutdown."""
    control_url = settings.conductor_control_database_url or settings.database_url
    control_engine = make_engine(control_url, pool_size=2, max_overflow=2)
    # max_concurrent (= batch_size) runs each hold up to ~2 short-lived connections at peak
    # (keep-alive renew + a status/finalize query), so give the app pool 2x headroom.
    app_engine = make_engine(
        settings.database_url,
        pool_size=settings.conductor_batch_size,
        max_overflow=settings.conductor_batch_size,
    )
    app_sessionmaker = make_sessionmaker(app_engine)
    host = CompanyGraphHost(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
        deployment=settings.model_deployment,
        workdir=Path(settings.workdir),
        app_sessionmaker=app_sessionmaker,
        log_store=RunLogStore(Path(settings.log_dir)),
        engine_ledger_dsn=settings.resolved_engine_ledger_dsn(),
    )
    conductor = Conductor(
        control_sessionmaker=make_sessionmaker(control_engine),
        app_sessionmaker=app_sessionmaker,
        executor=ChorusRunExecutor(host, max_ticks=settings.conductor_max_ticks),
        worker_id=settings.instance_id,
        lease_seconds=settings.conductor_lease_seconds,
        batch_size=settings.conductor_batch_size,
        poll_interval=settings.conductor_poll_interval,
    )

    async def aclose() -> None:
        await host.aclose()  # stop per-company event ingests
        await control_engine.dispose()
        await app_engine.dispose()

    return conductor, aclose
