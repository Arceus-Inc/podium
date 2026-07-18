"""OM-4 — the board as a control surface: pause/resume an employee, set their budget.

Paperclip's board can pause any agent and edit budgets at every level; the engine already has
both primitives (EmployeeStatus.PAUSED gates invokability, BudgetPolicy caps spend with a
hard-stop incident). These doors expose them."""

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


def _hire(dsn: str, company_id: str) -> None:
    from chorus.ledger import Ledger
    from chorus.workforce import Employee

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.employees.create(Employee(id="rex", name="Rex", role="engineer"))
    finally:
        ledger.close()


async def _mint(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return str(ws.id), str(company.id), token


async def test_pause_and_resume_gate_the_employee(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "brd")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    _hire(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    paused = await api.post(f"{base}/employees/rex/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = await api.post(f"{base}/employees/rex/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "idle"

    missing = await api.post(f"{base}/employees/nope/pause", headers=headers)
    assert missing.status_code == 404


async def test_budget_patch_upserts_the_employee_policy(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "brd2")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    _hire(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    created = await api.patch(
        f"{base}/employees/rex/budget", headers=headers, json={"amount_cents": 500_000}
    )
    assert created.status_code == 200
    body = created.json()
    assert body["amount_cents"] == 500_000
    assert body["hard_stop_enabled"] is True  # the ceiling that auto-pauses is on by default

    updated = await api.patch(
        f"{base}/employees/rex/budget", headers=headers, json={"amount_cents": 250_000}
    )
    assert updated.status_code == 200
    assert updated.json()["amount_cents"] == 250_000

    # The engine sees exactly one policy for the scope — upsert, never duplicates.
    from chorus.ledger import BudgetScope, Ledger

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        policies = ledger.budget_policies.by_scope(BudgetScope.EMPLOYEE, "rex")
        assert len(policies) == 1 and policies[0].amount == 250_000
    finally:
        ledger.close()

    missing = await api.patch(
        f"{base}/employees/nope/budget", headers=headers, json={"amount_cents": 1}
    )
    assert missing.status_code == 404
