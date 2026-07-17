"""M2 exit, for real: a run goes queued→running→terminal driving a live CompanyGraph on a real model.

Opt in with PODIUM_RUN_INTEGRATION=1; creds are read from chorus/.env. Skipped by default so the
normal gate stays hermetic and free.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.companies import create_company
from podium.conductor import Conductor
from podium.conductor._chorus_executor import ChorusRunExecutor, CompanyGraphHost
from podium.db import tenant_session
from podium.events import list_run_events
from podium.logs import RunLogStore
from podium.runs import TERMINAL_STATUSES, RunStatus, create_run, get_run
from podium.workspaces import create_workspace

pytestmark = pytest.mark.integration

_CHORUS_ENV = Path(os.environ.get("PODIUM_CHORUS_ENV", "/Users/divyansh/chorus/.env"))


def _azure_creds() -> tuple[str, str, str] | None:
    if not os.environ.get("PODIUM_RUN_INTEGRATION") or not _CHORUS_ENV.exists():
        return None
    values: dict[str, str] = {}
    for line in _CHORUS_ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    key = values.get("AZURE_OPENAI_API_KEY")
    base = values.get("AZURE_OPENAI_BASE_URL")
    deployment = values.get("AZURE_OPENAI_DEPLOYMENT")
    if not (key and base and deployment):
        return None
    return key, base, deployment


@pytest.mark.parametrize("ledger_backend", ["sqlite", "postgres"])
async def test_real_run_reaches_a_terminal_status(
    ledger_backend: str,
    database_url: str,
    tmp_path: Path,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    creds = _azure_creds()
    if creds is None:
        pytest.skip("set PODIUM_RUN_INTEGRATION=1 and provide chorus/.env Azure creds")
    api_key, base_url, deployment = creds

    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="Integration", slug="integ")
        company = await create_company(
            s, workspace_id=ws.id, slug="acme", name="Acme", ledger_backend=ledger_backend
        )
        ws_id, company_id = ws.id, company.id
    async with tenant_session(app_sessionmaker, ws_id) as s:
        run, _ = await create_run(
            s,
            workspace_id=ws_id,
            company_id=company_id,
            directive="Write a one-line greeting to a file called hello.txt.",
            idempotency_key="integ-1",
        )
        run_id = run.id

    host = CompanyGraphHost(
        api_key=api_key,
        base_url=base_url,
        deployment=deployment,
        workdir=tmp_path,
        app_sessionmaker=app_sessionmaker,
        log_store=RunLogStore(tmp_path / "logs"),
        engine_ledger_dsn=database_url.replace("+asyncpg", "").replace(
            "://postgres@", "://podium_app@"
        ),
    )
    # A real agent building to chorus's DoD takes many slow beats; a modest budget proves the run
    # engages the real model + heartbeat (queued→running→a terminal state). For a full succeed-to-DoD
    # run, raise max_ticks and be patient — it is nondeterministic, not a gate.
    conductor = Conductor(
        control_sessionmaker=sessionmaker,
        app_sessionmaker=app_sessionmaker,
        executor=ChorusRunExecutor(host, max_ticks=int(os.environ.get("PODIUM_MAX_TICKS", "8"))),
        worker_id="integration",
    )

    try:
        assert await conductor.dispatch_once() == 1
        async with tenant_session(app_sessionmaker, ws_id) as s:
            finished = await get_run(s, run_id)
        assert finished is not None
        assert finished.status in TERMINAL_STATUSES  # claimed, executed on a real model, finalized
        assert finished.status != RunStatus.QUEUED

        # The live EventBus was mirrored: the run has a queryable event log (let the ingest drain).
        events: list[object] = []
        for _ in range(40):
            async with tenant_session(app_sessionmaker, ws_id) as s:
                events = await list_run_events(s, run_id, after=0, limit=100)
            if events:
                break
            await asyncio.sleep(0.1)
        assert events, "expected the run's chorus events to be mirrored into the event log"
    finally:
        await host.aclose()
