"""CO2 — the human boundary as product doors (the T2 pattern productized).

The CEO's tool leaves a typed WorkforcePlan pending; nothing materializes until a human hits
these doors. Approve runs the engine's WorkforcePlanService atomically (employees + bounded
management grants + audit trail); reject leaves the workforce untouched."""

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


def _propose_plan(dsn: str, company_id: str) -> str:
    """Seed what the CEO beat would leave behind: a real proposed plan in the engine ledger."""
    from chorus.governance import WorkforcePlanService
    from chorus.ledger import (
        Ledger,
        ManagementGrantDraft,
        PlannedEmployee,
        WorkforcePlanDraft,
    )
    from chorus.roles import RoleRegistry, default_roles
    from chorus.workforce import Employee
    from chorus.workforce._ledger import LedgerWorkforce

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        if ledger.employees.get("ceo") is None:
            ledger.employees.create(Employee(id="ceo", name="Casey", role="ceo"))
        service = WorkforcePlanService(
            ledger,
            workforce=LedgerWorkforce(ledger.employees),
            roles=RoleRegistry.from_plugins(default_roles()),
            max_org_depth=2,
        )
        draft = WorkforcePlanDraft(
            rationale="Linkport formation: one lead, one IC",
            confidence=0.9,
            source_goal_ids=("goal-linkport",),
            employees=(
                PlannedEmployee(
                    ref="lena",
                    name="Lena",
                    profession="backend_engineer",
                    reports_to_ref="ceo",
                    budget_cents=500_000,
                ),
                PlannedEmployee(
                    ref="ivy",
                    name="Ivy",
                    profession="backend_engineer",
                    reports_to_ref="lena",
                ),
            ),
            management_grants=(
                ManagementGrantDraft(
                    employee_ref="ceo",
                    can_lead=True,
                    can_subdelegate=True,
                    max_delegation_depth=2,
                    max_team_size=2,
                    allowed_professions=("backend_engineer",),
                ),
                ManagementGrantDraft(
                    employee_ref="lena",
                    can_lead=True,
                    can_subdelegate=False,
                    max_delegation_depth=1,
                    max_team_size=2,
                    allowed_professions=("backend_engineer",),
                ),
            ),
        )
        plan = service.propose(draft, proposed_by_employee_id="ceo")
        return plan.id
    finally:
        ledger.close()


async def _mint(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> tuple[str, str, str]:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return str(ws.id), str(company.id), token


async def test_plans_surface_and_approve_materializes(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "gov")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    plan_id = _propose_plan(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    listed = await api.get(f"{base}/plans", headers=headers)
    assert listed.status_code == 200
    plans = listed.json()
    assert len(plans) == 1
    plan = plans[0]
    assert plan["id"] == plan_id
    assert plan["status"] == "proposed"
    assert plan["proposed_by"] == "ceo"
    assert {e["ref"] for e in plan["employees"]} == {"lena", "ivy"}
    assert plan["employees"][0]["reports_to"] in {"ceo", "lena"}
    assert len(plan["grants"]) == 2

    # The human boundary: approve materializes atomically, with the decider audited.
    approved = await api.post(f"{base}/plans/{plan_id}/approve", headers=headers)
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "applied"
    assert body["decided_by"]  # the authenticated actor, never client-supplied

    roster = await api.get(f"{base}/workforce", headers=headers)
    ids = {member["id"] for member in roster.json()}
    assert {"ceo", "lena", "ivy"} <= ids

    # Approving twice is a conflict, not a double-materialization.
    again = await api.post(f"{base}/plans/{plan_id}/approve", headers=headers)
    assert again.status_code == 409


async def test_reject_leaves_workforce_untouched(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "gov2")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    plan_id = _propose_plan(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"
    headers = {"Authorization": f"Bearer {token}"}

    rejected = await api.post(f"{base}/plans/{plan_id}/reject", headers=headers)
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    roster = await api.get(f"{base}/workforce", headers=headers)
    assert {member["id"] for member in roster.json()} == {"ceo"}

    missing = await api.post(f"{base}/plans/nope/approve", headers=headers)
    assert missing.status_code == 404


async def test_plans_require_auth(
    sessionmaker: async_sessionmaker[AsyncSession], api: httpx.AsyncClient
) -> None:
    ws_id, company_id, _ = await _mint(sessionmaker, "gov3")
    response = await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}/plans")
    assert response.status_code == 401
