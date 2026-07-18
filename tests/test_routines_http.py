"""OM-1 — standing heartbeats need a control surface: the routines doors.

Hiring provisions role-declared routines in the engine (the CEO's executive review); these doors
make them visible and governable — list, pause, resume, and fire-now (the e2e trigger: a firing
writes a task through the engine's own cron path, never runs an agent inline)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.main import create_app
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _hire_ceo(dsn: str, company_id: str) -> None:
    """Hire through the real engine facade so role-declared routines provision (spec 13 §5)."""
    from chorus.facade import Caps, Chorus
    from chorus.ledger import Ledger
    from chorus.observability import EventBus, LedgerInspector
    from chorus.roles import RoleRegistry, default_roles
    from chorus.workforce._ledger import LedgerWorkforce

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        org = Chorus(
            ledger=ledger,
            workforce=LedgerWorkforce(ledger.employees),
            memory_writer=None,  # type: ignore[arg-type]  # hire never runs a beat
            scheduler=None,  # type: ignore[arg-type]
            event_bus=EventBus(),
            inspector=LedgerInspector(ledger),
            dream=None,
            roles=RoleRegistry.from_plugins(default_roles()),
            caps=Caps(),
        )
        org.hire(name="Casey", role="ceo")
    finally:
        ledger.close()


async def _mint(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return str(ws.id), str(company.id), token


async def test_hire_provisions_the_ceo_review_and_the_doors_govern_it(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "rtn")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    _hire_ceo(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    listed = await api.get(f"{base}/routines", headers=headers)
    assert listed.status_code == 200
    routines = listed.json()
    assert len(routines) == 1
    routine = routines[0]
    assert routine["employee_id"] == "casey"
    assert routine["status"] == "active"
    assert routine["schedule"] == "0 * * * *"
    assert routine["next_run_at"] is not None
    assert "Executive review" in routine["intent"]

    paused = await api.post(f"{base}/routines/{routine['id']}/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = await api.post(f"{base}/routines/{routine['id']}/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"


async def test_fire_now_writes_a_task_through_the_cron_path(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "rtn2")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    _hire_ceo(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    routines = (await api.get(f"{base}/routines", headers=headers)).json()
    fired = await api.post(f"{base}/routines/{routines[0]['id']}/fire", headers=headers)
    assert fired.status_code == 200
    task_id = fired.json()["task_id"]
    assert task_id

    # The firing wrote a real engine task carrying the routine's intent — the org's own work shape.
    from chorus.ledger import Ledger

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        task = ledger.tasks.get(task_id)
        assert task is not None
        assert "Executive review" in task.intent
        assert task.assignee_employee_id == "casey"
    finally:
        ledger.close()


async def test_routines_doors_are_fail_closed(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "rtn3")
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    unauthed = await api.get(f"{base}/routines")
    assert unauthed.status_code == 401

    missing = await api.post(f"{base}/routines/nope/fire", headers=headers)
    assert missing.status_code == 404
