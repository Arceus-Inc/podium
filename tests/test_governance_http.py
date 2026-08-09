"""CO2 — the human boundary as product doors (the T2 pattern productized).

The CEO's tool leaves a typed WorkforcePlan pending; nothing materializes until a human hits
these doors. Approve runs the engine's WorkforcePlanService atomically (employees + bounded
management grants + audit trail); reject leaves the workforce untouched."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.main import create_app
from podium.users import create_user
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


def _seed_approvals(dsn: str, company_id: str) -> tuple[str, str, str, str]:
    from datetime import UTC, datetime, timedelta

    from chorus.ids import mint_id
    from chorus.ledger import Approval, ApprovalAction, ApprovalGate, ApprovalSubjectKind, Ledger

    task_approval_id, artifact_approval_id, expired_approval_id = mint_id(), mint_id(), mint_id()
    task_id, artifact_id, expired_task_id = mint_id(), mint_id(), mint_id()
    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.approvals.request(
            Approval(
                id=task_approval_id,
                subject_kind=ApprovalSubjectKind.TASK,
                subject_id=task_id,
                reason="accept the release",
                action=ApprovalAction.TASK_GATE,
                gate_kind=ApprovalGate.ACCEPTANCE,
            )
        )
        ledger.approvals.request(
            Approval(
                id=artifact_approval_id,
                subject_kind=ApprovalSubjectKind.ARTIFACT,
                subject_id=artifact_id,
                reason="promote the release",
                action=ApprovalAction.BOARD_APPROVAL,
            )
        )
        ledger.approvals.request(
            Approval(
                id=expired_approval_id,
                subject_kind=ApprovalSubjectKind.TASK,
                subject_id=expired_task_id,
                reason="stale gate",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    finally:
        ledger.close()
    return task_approval_id, artifact_approval_id, task_id, artifact_id


async def test_approvals_surface_pending_gates_and_reject_other_statuses(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "approvals")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    task_approval_id, artifact_approval_id, task_id, artifact_id = _seed_approvals(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals"
    headers = {"Authorization": f"Bearer {token}"}

    response = await api.get(f"{base}?status=pending", headers=headers)
    assert response.status_code == 200
    approvals = response.json()
    assert [approval["id"] for approval in approvals] == [task_approval_id, artifact_approval_id]
    assert approvals[0]["subject"] == {"kind": "task", "id": task_id}
    assert approvals[0]["action"] == "task_gate"
    assert approvals[0]["gate_kind"] == "acceptance"
    assert approvals[1]["subject"] == {"kind": "artifact", "id": artifact_id}
    assert approvals[1]["action"] == "board_approval"
    assert all(approval["status"] == "pending" for approval in approvals)
    assert all(approval["created_at"] is not None for approval in approvals)

    assert (await api.get(f"{base}?status=approved", headers=headers)).status_code == 422
    assert (await api.get(f"{base}?status=unknown", headers=headers)).status_code == 422


async def test_approvals_keep_company_ownership_opaque(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="Ownership", slug="approval-ownership")
        owner = await create_user(
            session, workspace_id=workspace.id, email="owner@example.com", name="Owner"
        )
        peer = await create_user(
            session, workspace_id=workspace.id, email="peer@example.com", name="Peer"
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            slug="private",
            name="Private",
            owner_user_id=owner.id,
        )
        _, owner_token = await create_api_key(
            session, workspace_id=workspace.id, name="owner", user_id=owner.id
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer", user_id=peer.id
        )
        foreign_workspace = await create_workspace(session, name="Foreign", slug="approval-foreign")
        _, foreign_token = await create_api_key(
            session, workspace_id=foreign_workspace.id, name="foreign"
        )

    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id, _, _, _ = _seed_approvals(dsn, str(company.id))
    path = f"/v1/workspaces/{workspace.id}/companies/{company.id}/approvals/{approval_id}"

    assert (
        await api.get(path, headers={"Authorization": f"Bearer {owner_token}"})
    ).status_code == 200
    assert (
        await api.get(path, headers={"Authorization": f"Bearer {peer_token}"})
    ).status_code == 404
    assert (
        await api.get(path, headers={"Authorization": f"Bearer {foreign_token}"})
    ).status_code == 403


def _seed_approval_detail(dsn: str, company_id: str) -> tuple[str, str, str]:
    from datetime import UTC, datetime, timedelta

    from chorus.ids import mint_id
    from chorus.ledger import Approval, ApprovalSubjectKind, Ledger

    pending_id, resolved_id, expired_id = mint_id(), mint_id(), mint_id()
    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.approvals.request(
            Approval(
                id=pending_id,
                subject_kind=ApprovalSubjectKind.TASK,
                subject_id=mint_id(),
                reason="awaiting approval",
            )
        )
        ledger.approvals.request(
            Approval(
                id=resolved_id,
                subject_kind=ApprovalSubjectKind.TASK,
                subject_id=mint_id(),
                reason="already approved",
            )
        )
        ledger.approvals.approve(resolved_id, decided_by_user_id="board-user")
        ledger.approvals.request(
            Approval(
                id=expired_id,
                subject_kind=ApprovalSubjectKind.TASK,
                subject_id=mint_id(),
                reason="timed out",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    finally:
        ledger.close()
    return pending_id, resolved_id, expired_id


async def test_approval_detail_serves_pending_resolved_and_expired(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "approval-detail")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    pending_id, resolved_id, expired_id = _seed_approval_detail(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals"
    headers = {"Authorization": f"Bearer {token}"}

    pending = await api.get(f"{base}/{pending_id}", headers=headers)
    resolved = await api.get(f"{base}/{resolved_id}", headers=headers)
    expired = await api.get(f"{base}/{expired_id}", headers=headers)

    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"
    assert expired.status_code == 200
    assert expired.json()["expires_at"] is not None
    assert all(response.json()["created_at"] is not None for response in (pending, resolved, expired))
    assert (await api.get(f"{base}/not-a-uuid", headers=headers)).status_code == 404
    assert (await api.get(f"{base}/{uuid.uuid4()}", headers=headers)).status_code == 404


async def test_approval_detail_is_opaque_across_companies(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "approval-detail-a")
    async with sessionmaker() as session, session.begin():
        other_company = await create_company(
            session,
            workspace_id=uuid.UUID(ws_id),
            slug="approval-detail-b",
            name="Approval Detail B",
        )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id, _, _ = _seed_approval_detail(dsn, company_id)

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{other_company.id}/approvals/{approval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def test_approval_detail_etag_revalidates_and_tracks_state(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    from chorus.ledger import Ledger

    ws_id, company_id, token = await _mint(sessionmaker, "approval-etag")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id, _, _ = _seed_approval_detail(dsn, company_id)
    path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    headers = {"Authorization": f"Bearer {token}"}

    first = await api.get(path, headers=headers)
    second = await api.get(path, headers=headers)
    assert first.status_code == 200
    assert first.headers["etag"] == second.headers["etag"]
    assert first.headers["etag"].startswith('"') and first.headers["etag"].endswith('"')

    matched = await api.get(path, headers={**headers, "If-None-Match": first.headers["etag"]})
    assert matched.status_code == 304
    assert matched.content == b""
    assert matched.headers["etag"] == first.headers["etag"]

    nonmatching = await api.get(path, headers={**headers, "If-None-Match": '"different"'})
    assert nonmatching.status_code == 200
    assert nonmatching.json()["id"] == approval_id

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.approvals.approve(approval_id, decided_by_user_id="board-user")
    finally:
        ledger.close()

    changed = await api.get(path, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["status"] == "approved"
    assert changed.headers["etag"] != first.headers["etag"]
