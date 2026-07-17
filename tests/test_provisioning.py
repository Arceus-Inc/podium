"""M5 provisioning saga: a company is born `provisioning` and becomes `idle` only after the
conductor successfully hosts it (graph built, engine store reachable, default worker seeded).
No per-company DDL — the engine tables exist from migration; provisioning is state + seed rows.
A failed host leaves the company `provisioning` (retried on the next ensure); the flip is a
guarded CAS, so it is idempotent and never resurrects a later state.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.companies import create_company, get_company
from podium.conductor._chorus_executor import CompanyGraphHost
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.workspaces import create_workspace

_FAKE_MODEL = {"api_key": "test-key", "base_url": "http://localhost:9", "deployment": "none"}


def _pg_conninfo(database_url: str, *, user: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", f"://{user}@")


async def _company(
    admin: async_sessionmaker[AsyncSession], *, backend: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name="A", slug="a")
        company = await create_company(
            s, workspace_id=ws.id, slug="c", name="C", ledger_backend=backend
        )
        assert company.state == "provisioning"  # born unprovisioned
        return ws.id, company.id


async def test_successful_host_flips_provisioning_to_idle(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, company_id = await _company(sessionmaker, backend="postgres")
    host = CompanyGraphHost(
        **_FAKE_MODEL,
        workdir=tmp_path,
        app_sessionmaker=app_sessionmaker,
        log_store=RunLogStore(tmp_path / "logs"),
        engine_ledger_dsn=_pg_conninfo(database_url, user="podium_app"),
    )
    try:
        await host.ensure(company_id, ws_id)
        async with tenant_session(app_sessionmaker, ws_id) as s:
            company = await get_company(s, company_id)
        assert company is not None and company.state == "idle"
        # Idempotent: a second ensure (cached runtime) never regresses the state.
        await host.ensure(company_id, ws_id)
        async with tenant_session(app_sessionmaker, ws_id) as s:
            company = await get_company(s, company_id)
        assert company is not None and company.state == "idle"
    finally:
        await host.aclose()


async def test_failed_host_leaves_the_company_provisioning(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, company_id = await _company(sessionmaker, backend="postgres")
    host = CompanyGraphHost(
        **_FAKE_MODEL,
        workdir=tmp_path,
        app_sessionmaker=app_sessionmaker,
        log_store=RunLogStore(tmp_path / "logs"),
        engine_ledger_dsn="host=127.0.0.1 port=1 dbname=nope",  # unreachable engine store
    )
    with pytest.raises(Exception):
        await host.ensure(company_id, ws_id)
    async with tenant_session(app_sessionmaker, ws_id) as s:
        company = await get_company(s, company_id)
    assert company is not None and company.state == "provisioning"  # retried next ensure
    await host.aclose()
