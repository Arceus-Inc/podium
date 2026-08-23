"""CO2 — the human boundary as product doors (the T2 pattern productized).

The CEO's tool leaves a typed WorkforcePlan pending; nothing materializes until a human hits
these doors. Approve runs the engine's WorkforcePlanService atomically (employees + bounded
management grants + audit trail); reject leaves the workforce untouched."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.control._governance import ApprovalView, TaskSubjectRef
from podium.control.router import _approval_page, _cursor_key
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
    body = response.json()
    approvals = body["data"]
    assert [approval["id"] for approval in approvals] == [task_approval_id, artifact_approval_id]
    assert approvals[0]["subject"] == {"kind": "task", "id": task_id}
    assert approvals[0]["action"] == "task_gate"
    assert approvals[0]["gate_kind"] == "acceptance"
    assert approvals[1]["subject"] == {"kind": "artifact", "id": artifact_id}
    assert approvals[1]["action"] == "board_approval"
    assert all(approval["status"] == "pending" for approval in approvals)
    assert all(approval["created_at"] is not None for approval in approvals)
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    assert body["links"]["self"] == f"{base}?status=pending&limit=50"
    assert body["links"]["next"] is None

    assert (await api.get(f"{base}?status=approved", headers=headers)).status_code == 422
    assert (await api.get(f"{base}?status=unknown", headers=headers)).status_code == 422
    assert (await api.get(f"{base}?cursor=not-a-cursor", headers=headers)).status_code == 422
    assert (await api.get(f"{base}?cursor={'x' * 513}", headers=headers)).status_code == 422
    assert (await api.get(f"{base}?unexpected=value", headers=headers)).status_code == 422


async def test_approvals_page_is_keyseted_and_linked(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint(sessionmaker, "approval-page")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    task_approval_id, artifact_approval_id, _, _ = _seed_approvals(dsn, company_id)
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals"
    headers = {"Authorization": f"Bearer {token}"}

    first = await api.get(f"{base}?limit=1", headers=headers)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["data"][0]["id"] == task_approval_id
    assert first_body["meta"]["has_more"] is True
    assert first_body["meta"]["next_cursor"] is not None
    assert first_body["links"]["next"] is not None

    second = await api.get(first_body["links"]["next"], headers=headers)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["data"][0]["id"] == artifact_approval_id
    assert second_body["meta"] == {"next_cursor": None, "has_more": False}
    assert second_body["links"]["self"] == first_body["links"]["next"]
    assert {first_body["data"][0]["id"], second_body["data"][0]["id"]} == {
        task_approval_id,
        artifact_approval_id,
    }
    assert (await api.get(f"{base}?limit=201", headers=headers)).status_code == 422


def test_approval_page_breaks_created_at_ties_by_id() -> None:
    created_at = datetime.fromisoformat("2026-08-09T00:00:00+00:00")
    first = ApprovalView(
        id="00000000-0000-0000-0000-000000000001",
        subject=TaskSubjectRef(id="task-a"),
        reason="a",
        action="task_gate",
        status="pending",
        gate_kind=None,
        decided_by_user_id=None,
        decided_at=None,
        expires_at=None,
        created_at=created_at,
    )
    second = ApprovalView(
        id="00000000-0000-0000-0000-000000000002",
        subject=TaskSubjectRef(id="task-b"),
        reason="b",
        action="task_gate",
        status="pending",
        gate_kind=None,
        decided_by_user_id=None,
        decided_at=None,
        expires_at=None,
        created_at=created_at,
    )

    page, has_more = _approval_page([second, first], cursor=None, limit=1)
    assert [approval.id for approval in page] == [first.id]
    next_page, next_has_more = _approval_page(
        [second, first], cursor=(first.created_at, first.id), limit=1
    )
    assert [approval.id for approval in next_page] == [second.id]
    assert has_more is True
    assert next_has_more is False


def test_approval_cursor_accepts_utc_spellings_and_rejects_other_offsets() -> None:
    approval_id = "00000000-0000-0000-0000-000000000001"

    def _encode(timestamp: str) -> str:
        return base64.urlsafe_b64encode(f"{timestamp}|{approval_id}".encode()).decode().rstrip("=")

    expected = (datetime(2026, 8, 9, tzinfo=UTC), approval_id)
    assert _cursor_key(_encode("2026-08-09T00:00:00+00:00")) == expected
    assert _cursor_key(_encode("2026-08-09T00:00:00Z")) == expected
    with pytest.raises(HTTPException, match="invalid cursor"):
        _cursor_key(_encode("2026-08-09T01:00:00+01:00"))


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
    list_path = f"/v1/workspaces/{workspace.id}/companies/{company.id}/approvals"

    assert (
        await api.get(path, headers={"Authorization": f"Bearer {owner_token}"})
    ).status_code == 200
    assert (
        await api.get(list_path, headers={"Authorization": f"Bearer {owner_token}"})
    ).status_code == 200
    assert (
        await api.get(path, headers={"Authorization": f"Bearer {peer_token}"})
    ).status_code == 404
    assert (
        await api.get(list_path, headers={"Authorization": f"Bearer {peer_token}"})
    ).status_code == 404
    assert (
        await api.get(path, headers={"Authorization": f"Bearer {foreign_token}"})
    ).status_code == 403
    assert (
        await api.get(list_path, headers={"Authorization": f"Bearer {foreign_token}"})
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
    assert pending.json()["data"]["status"] == "pending"
    assert pending.json()["meta"] == {}
    assert pending.json()["links"]["self"] == f"{base}/{pending_id}"
    assert resolved.status_code == 200
    assert resolved.json()["data"]["status"] == "approved"
    assert resolved.json()["data"]["decided_by_user_id"] == "board-user"
    assert resolved.json()["data"]["decided_at"] is not None
    assert expired.status_code == 200
    assert expired.json()["data"]["expires_at"] is not None
    assert all(response.json()["data"]["created_at"] is not None for response in (pending, resolved, expired))
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

    normal_syntax = await api.get(
        path,
        headers={**headers, "If-None-Match": f'"other", W/{first.headers["etag"]}'},
    )
    assert normal_syntax.status_code == 304
    assert normal_syntax.content == b""
    assert (await api.get(path, headers={**headers, "If-None-Match": "*"})).status_code == 304

    nonmatching = await api.get(path, headers={**headers, "If-None-Match": '"different"'})
    assert nonmatching.status_code == 200
    assert nonmatching.json()["data"]["id"] == approval_id

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        ledger.approvals.approve(approval_id, decided_by_user_id="board-user")
    finally:
        ledger.close()

    changed = await api.get(path, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["data"]["status"] == "approved"
    assert changed.json()["data"]["decided_by_user_id"] == "board-user"
    assert changed.json()["data"]["decided_at"] is not None
    assert changed.headers["etag"] != first.headers["etag"]


async def _mint_user(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> tuple[str, str, str]:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name=slug.upper(), slug=slug)
        user = await create_user(
            session,
            workspace_id=workspace.id,
            email=f"{slug}@example.com",
            name=slug,
        )
        company = await create_company(
            session, workspace_id=workspace.id, slug=slug, name=slug.upper()
        )
        _, token = await create_api_key(
            session, workspace_id=workspace.id, name=slug, user_id=user.id
        )
        return str(workspace.id), str(company.id), token


async def _mint_peer_user(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, slug: str
) -> tuple[str, str]:
    async with sessionmaker() as session, session.begin():
        user = await create_user(
            session,
            workspace_id=uuid.UUID(workspace_id),
            email=f"{slug}@example.com",
            name=slug,
        )
        _, token = await create_api_key(
            session,
            workspace_id=uuid.UUID(workspace_id),
            name=slug,
            user_id=user.id,
        )
        return str(user.id), token


def _seed_authorization_gate(dsn: str, company_id: str) -> str:
    from chorus.governance import GovernanceResolver
    from chorus.ids import mint_id
    from chorus.ledger import ApprovalGate, Ledger, Task

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        task_id = mint_id()
        ledger.tasks.submit(Task(id=task_id, intent="ship the decision API"))
        return GovernanceResolver(ledger).open_task_gate(
            task_id,
            gate_kind=ApprovalGate.AUTHORIZATION,
            reason="human authorization required",
        ).id
    finally:
        ledger.close()


async def _approval_etag_for(
    api: httpx.AsyncClient, path: str, headers: dict[str, str]
) -> str:
    detail = await api.get(path, headers=headers)
    assert detail.status_code == 200
    return detail.headers["etag"]


async def test_approval_decision_creates_immutable_authenticated_proof(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-decision")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    etag = await _approval_etag_for(api, approval_path, headers)

    created = await api.post(
        path,
        json={"verdict": "approve"},
        headers={
            **headers,
            "Idempotency-Key": "approve-once",
            "If-Match": etag,
            "X-Request-ID": "request-create",
        },
    )

    assert created.status_code == 201
    envelope = created.json()
    decision = envelope["data"]
    assert decision["approval_id"] == approval_id
    assert decision["verdict"] == "approve"
    assert decision["method"] == "api_key"
    assert decision["request_id"] == "request-create"
    assert decision["authenticated_at"] == decision["decided_at"]
    assert envelope["links"] == {"approval": approval_path}
    assert "location" not in created.headers
    assert (await api.get(approval_path, headers=headers)).json()["data"]["status"] == "approved"


async def test_approval_decision_replays_before_preconditions_and_binds_request_id(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-replay")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post(
        path,
        json={"verdict": "approve"},
        headers={
            **headers,
            "Idempotency-Key": "same-key",
            "If-Match": await _approval_etag_for(api, approval_path, headers),
            "X-Request-ID": "first-request",
        },
    )
    assert created.status_code == 201

    replayed = await api.post(
        path,
        json={"verdict": "approve"},
        headers={**headers, "Idempotency-Key": "same-key", "X-Request-ID": "second-request"},
    )
    assert replayed.status_code == 200
    assert replayed.headers["idempotency-replayed"] == "true"
    assert replayed.json() == created.json()
    assert replayed.json()["data"]["request_id"] == "first-request"

    reused = await api.post(
        path,
        json={"verdict": "deny"},
        headers={**headers, "Idempotency-Key": "same-key"},
    )
    assert reused.status_code == 409
    assert reused.headers["content-type"].startswith("application/problem+json")
    assert reused.json()["type"] == "urn:podium:problem:idempotency_key_reuse"

    other_approval_id = _seed_authorization_gate(dsn, company_id)
    cross_target = await api.post(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{other_approval_id}/decisions",
        json={"verdict": "approve"},
        headers={**headers, "Idempotency-Key": "same-key"},
    )
    assert cross_target.status_code == 409
    assert cross_target.json()["type"] == "urn:podium:problem:idempotency_key_reuse"


async def test_approval_decision_requires_human_idempotency_and_fresh_strong_etag(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, service_token = await _mint(sessionmaker, "decision-preconditions")
    async with sessionmaker() as session, session.begin():
        user = await create_user(
            session,
            workspace_id=uuid.UUID(ws_id),
            email="decision-user@example.com",
            name="Decision User",
        )
        _, user_token = await create_api_key(
            session, workspace_id=uuid.UUID(ws_id), name="decision-user", user_id=user.id
        )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    user_headers = {"Authorization": f"Bearer {user_token}"}

    service = await api.post(
        path,
        json={"verdict": "approve"},
        headers={
            "Authorization": f"Bearer {service_token}",
            "Idempotency-Key": "service-key",
            "If-Match": await _approval_etag_for(
                api, approval_path, {"Authorization": f"Bearer {service_token}"}
            ),
        },
    )
    assert service.status_code == 403
    assert service.json()["type"] == "urn:podium:problem:human_actor_required"

    missing = await api.post(
        path,
        json={"verdict": "approve"},
        headers={**user_headers, "Idempotency-Key": "missing-etag"},
    )
    assert missing.status_code == 428
    assert missing.headers["content-type"].startswith("application/problem+json")

    stale = await api.post(
        path,
        json={"verdict": "approve"},
        headers={
            **user_headers,
            "Idempotency-Key": "stale-etag",
            "If-Match": 'W/"stale"',
        },
    )
    assert stale.status_code == 412
    assert stale.headers["content-type"].startswith("application/problem+json")


async def test_approval_hold_stays_pending_then_new_key_can_approve(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-hold")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    etag = await _approval_etag_for(api, approval_path, headers)

    held = await api.post(
        path,
        json={"verdict": "hold"},
        headers={**headers, "Idempotency-Key": "hold-key", "If-Match": etag},
    )
    assert held.status_code == 201
    assert held.json()["data"]["verdict"] == "hold"
    assert (await api.get(approval_path, headers=headers)).json()["data"]["status"] == "pending"

    approved = await api.post(
        path,
        json={"verdict": "approve"},
        headers={**headers, "Idempotency-Key": "approve-key", "If-Match": etag},
    )
    assert approved.status_code == 201
    assert approved.json()["data"]["verdict"] == "approve"
    assert (await api.get(approval_path, headers=headers)).json()["data"]["status"] == "approved"


def _seed_unhandled_approval(dsn: str, company_id: str) -> str:
    from chorus.ids import mint_id
    from chorus.ledger import Approval, ApprovalAction, ApprovalSubjectKind, Ledger

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        approval = ledger.approvals.request(
            Approval(
                id=mint_id(),
                subject_kind=ApprovalSubjectKind.BUDGET_INCIDENT,
                subject_id=mint_id(),
                reason="requires an unavailable handler",
                action=ApprovalAction.BUDGET_OVERRIDE,
            )
        )
        return approval.id
    finally:
        ledger.close()


async def test_approval_decision_rolls_back_proof_when_handler_fails(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-rollback")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_unhandled_approval(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    request_headers = {
        **headers,
        "Idempotency-Key": "rollback-key",
        "If-Match": await _approval_etag_for(api, approval_path, headers),
    }

    failed = await api.post(path, json={"verdict": "approve"}, headers=request_headers)
    assert failed.status_code == 409
    assert (await api.get(approval_path, headers=headers)).json()["data"]["status"] == "pending"

    replay_attempt = await api.post(path, json={"verdict": "approve"}, headers=request_headers)
    assert replay_attempt.status_code == 409
    nonce = hashlib.sha256(b"rollback-key").hexdigest()
    from chorus.governance import GovernanceResolver
    from chorus.ledger import Ledger

    ledger = Ledger.open(dsn, company_id=company_id)
    try:
        assert GovernanceResolver(ledger).get_authorization_proof_by_nonce(nonce) is None
    finally:
        ledger.close()


async def test_approval_decision_keeps_private_company_and_approval_opaque(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="Private", slug="approval-private")
        owner = await create_user(
            session,
            workspace_id=workspace.id,
            email="owner-decision@example.com",
            name="Owner",
        )
        peer = await create_user(
            session,
            workspace_id=workspace.id,
            email="peer-decision@example.com",
            name="Peer",
        )
        company = await create_company(
            session,
            workspace_id=workspace.id,
            slug="private-decision",
            name="Private Decision",
            owner_user_id=owner.id,
        )
        _, peer_token = await create_api_key(
            session, workspace_id=workspace.id, name="peer-decision", user_id=peer.id
        )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, str(company.id))

    hidden = await api.post(
        f"/v1/workspaces/{workspace.id}/companies/{company.id}/approvals/{approval_id}/decisions",
        json={"verdict": "approve"},
        headers={"Authorization": f"Bearer {peer_token}", "Idempotency-Key": "opaque-key"},
    )
    assert hidden.status_code == 404


async def test_concurrent_same_key_approval_decisions_converge_to_one_proof(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-concurrent-same")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    request_headers = {
        **headers,
        "Idempotency-Key": "concurrent-same-key",
        "If-Match": await _approval_etag_for(api, approval_path, headers),
    }

    first, second = await asyncio.gather(
        api.post(path, json={"verdict": "approve"}, headers=request_headers),
        api.post(path, json={"verdict": "approve"}, headers=request_headers),
    )

    assert sorted((first.status_code, second.status_code)) == [200, 201]
    created, replayed = (first, second) if first.status_code == 201 else (second, first)
    assert replayed.headers["idempotency-replayed"] == "true"
    assert replayed.json() == created.json()
    assert created.json()["data"]["approval_id"] == approval_id


async def test_concurrent_different_body_same_key_is_idempotency_reuse(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _mint_user(sessionmaker, "approval-concurrent-different")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    headers = {"Authorization": f"Bearer {token}"}
    request_headers = {
        **headers,
        "Idempotency-Key": "concurrent-different-key",
        "If-Match": await _approval_etag_for(api, approval_path, headers),
    }

    first, second = await asyncio.gather(
        api.post(path, json={"verdict": "approve"}, headers=request_headers),
        api.post(path, json={"verdict": "deny"}, headers=request_headers),
    )

    assert sorted((first.status_code, second.status_code)) == [201, 409]
    reused = first if first.status_code == 409 else second
    assert reused.headers["content-type"].startswith("application/problem+json")
    assert reused.json()["type"] == "urn:podium:problem:idempotency_key_reuse"


async def test_same_company_peer_cannot_replay_another_users_decision_proof(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, first_token = await _mint_user(sessionmaker, "approval-user-bound")
    second_user_id, second_token = await _mint_peer_user(
        sessionmaker, ws_id, "approval-user-bound-peer"
    )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    first_headers = {"Authorization": f"Bearer {first_token}"}
    idempotency_key = "user-bound-key"

    created = await api.post(
        path,
        json={"verdict": "approve"},
        headers={
            **first_headers,
            "Idempotency-Key": idempotency_key,
            "If-Match": await _approval_etag_for(api, approval_path, first_headers),
        },
    )
    assert created.status_code == 201
    assert created.json()["data"]["method"] == "api_key"
    assert created.json()["data"]["user_id"] != second_user_id

    reused = await api.post(
        path,
        json={"verdict": "approve"},
        headers={"Authorization": f"Bearer {second_token}", "Idempotency-Key": idempotency_key},
    )
    assert reused.status_code == 409
    assert reused.json()["type"] == "urn:podium:problem:idempotency_key_reuse"
    assert created.json()["data"]["user_id"] not in reused.text


async def test_concurrent_same_key_same_body_different_users_never_replays_proof(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, first_token = await _mint_user(
        sessionmaker, "approval-concurrent-users"
    )
    _, second_token = await _mint_peer_user(
        sessionmaker, ws_id, "approval-concurrent-users-peer"
    )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    approval_id = _seed_authorization_gate(dsn, company_id)
    approval_path = f"/v1/workspaces/{ws_id}/companies/{company_id}/approvals/{approval_id}"
    path = f"{approval_path}/decisions"
    first_headers = {"Authorization": f"Bearer {first_token}"}
    etag = await _approval_etag_for(api, approval_path, first_headers)

    first, second = await asyncio.gather(
        api.post(
            path,
            json={"verdict": "approve"},
            headers={
                **first_headers,
                "Idempotency-Key": "concurrent-user-bound-key",
                "If-Match": etag,
            },
        ),
        api.post(
            path,
            json={"verdict": "approve"},
            headers={
                "Authorization": f"Bearer {second_token}",
                "Idempotency-Key": "concurrent-user-bound-key",
                "If-Match": etag,
            },
        ),
    )

    assert sorted((first.status_code, second.status_code)) == [201, 409]
    reused = first if first.status_code == 409 else second
    assert "idempotency-replayed" not in reused.headers
    assert reused.json()["type"] == "urn:podium:problem:idempotency_key_reuse"
