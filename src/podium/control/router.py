"""Control-plane HTTP doors. Every handler: authenticate → decide() → company visibility → plane.

Thin governed mappings onto CompanyControlPlane sub-facades (M4 §3.3): the router owns auth and
DTO serialization, the plane owns engine access — no engine type or connection escapes. Plane
reads run in a worker thread (the engine ledger is sync psycopg by design)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import urlencode

from chorus.errors import OrgInvariantViolation
from chorus.governance import GovernanceError, HumanAuthorization
from chorus.ledger import AuthenticationMethod, LedgerIntegrityError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies.service import get_company
from podium.control._allocation import AllocationBoard
from podium.control._comments import (
    CommentView,
    UndeliverableCommentError,
)
from podium.control._delegation import CapacityEntry, TeamSummary
from podium.control._direction import GoalNode
from podium.control._governance import (
    ApprovalDecisionView,
    ApprovalView,
    PlanConflictError,
    PlanView,
    UnknownPlanError,
)
from podium.control._observe import (
    ArtifactSummary,
    CompanyStatus,
    OrgReport,
    SkillSummary,
    SpendRow,
    UnknownTaskError,
    WhyLink,
)
from podium.control._plane import CompanyControlPlane, ControlPlaneProvider
from podium.control._routines import (
    RoutineFireConflict,
    RoutineSummary,
    UnknownRoutineError,
)
from podium.control._workforce import (
    BudgetView,
    DuplicateEmployee,
    EmployeeConflict,
    EmployeeView,
    UnknownEmployeeError,
    UnknownRole,
)
from podium.db import tenant_session
from podium.http_errors import ProblemHTTPException, get_request_id
from podium.runs.service import runs_by_status

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/companies/{company_id}", tags=["control"])

_T = TypeVar("_T")


def get_control_provider(request: Request) -> ControlPlaneProvider:
    provider = getattr(request.app.state, "control_provider", None)
    if provider is None:  # wired at lifespan from settings; absent only in misconfigured deploys
        raise HTTPException(status_code=503, detail="control plane unavailable")
    return provider  # type: ignore[no-any-return]


async def _visible_company_or_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    """Auth wall: decide() + product-DB visibility (RLS + ownership) — 404, never data."""
    resource = Resource(kind="company", workspace_id=workspace_id, company_id=company_id)
    if not decide(actor, "read", resource):
        raise HTTPException(status_code=403, detail="forbidden")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, company_id, user_id=actor.user_id)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")


async def _plane_read(
    provider: ControlPlaneProvider,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    read: Callable[[CompanyControlPlane], _T],
) -> _T:
    """One plane per request, opened and closed in a worker thread (sync engine connection)."""

    def _run() -> _T:
        plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
        try:
            return read(plane)
        finally:
            plane.close()

    return await run_in_threadpool(_run)


@router.get("/goals", response_model=list[GoalNode])
async def goal_tree(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[GoalNode]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.direction.goal_tree(),
    )


@router.get("/workforce", response_model=list[EmployeeView])
async def workforce(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[EmployeeView]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.workforce.roster(),
    )


@router.get("/teams", response_model=list[TeamSummary])
async def teams(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[TeamSummary]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.delegation.teams(),
    )


@router.get("/capacity", response_model=list[CapacityEntry])
async def capacity(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[CapacityEntry]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.delegation.capacity(),
    )


@router.get("/status", response_model=CompanyStatus)
async def status(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> CompanyStatus:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.status(),
    )


@router.get("/employees/{employee_id}/skills", response_model=list[SkillSummary])
async def employee_skills(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[SkillSummary]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.skills(employee_id),
    )


class GoalPatch(BaseModel):
    status: Literal["active", "archived"]


@router.patch("/goals/{goal_id}", response_model=GoalNode)
async def patch_goal(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    goal_id: str,
    body: GoalPatch,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> GoalNode:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    node = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.direction.set_goal_status(goal_id, body.status),
    )
    if node is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return node


class GoalCreate(BaseModel):
    title: str
    level: Literal["company", "team", "employee", "task", "goal"]
    parent_id: str | None = None


@router.post("/goals", status_code=201, response_model=GoalNode)
async def create_goal(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    body: GoalCreate,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> GoalNode:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    node = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.direction.create_goal(
            title=body.title, level=body.level, parent_id=body.parent_id
        ),
    )
    if node is None:
        raise HTTPException(status_code=404, detail="parent goal not found")
    return node


class EmployeeCreate(BaseModel):
    name: str
    role: str
    reports_to: str | None = None  # None = an org root (protected from termination)


@router.post("/employees", status_code=201, response_model=EmployeeView)
async def hire(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    body: EmployeeCreate,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> EmployeeView:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        return await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.workforce.hire(
                name=body.name, role=body.role, reports_to=body.reports_to
            ),
        )
    except UnknownRole as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateEmployee as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/employees/{employee_id}", status_code=204)
async def terminate(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> None:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.workforce.terminate(employee_id),
        )
    except UnknownEmployeeError as exc:
        raise HTTPException(status_code=404, detail="employee not found") from exc
    except OrgInvariantViolation as exc:  # e.g. the protected org root
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/allocation", response_model=AllocationBoard)
async def allocation(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> AllocationBoard:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.allocation.board(),
    )


@router.get("/costs", response_model=list[SpendRow])
async def costs(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    by: Literal["model", "employee", "day"] = "day",
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[SpendRow]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.costs(by),
    )


class CompanyOverview(BaseModel):
    runs_by_status: dict[str, int]  # product DB: the run lifecycle counts
    employees: int
    open_tasks: int
    running_beats: int
    blocked_tasks: int
    spend_cents: int


@router.get("/overview", response_model=CompanyOverview)
async def overview(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> CompanyOverview:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        run_counts = await runs_by_status(session, company_id)

    def _engine_half(plane: CompanyControlPlane) -> tuple[Any, int]:
        return plane.observe.status(), plane.observe.spend_total_cents()

    status_view, spend = await _plane_read(
        provider, workspace_id=workspace_id, company_id=company_id, read=_engine_half
    )
    return CompanyOverview(
        runs_by_status=run_counts,
        employees=status_view.employees,
        open_tasks=status_view.open_tasks,
        running_beats=status_view.running_beats,
        blocked_tasks=status_view.blocked_tasks,
        spend_cents=spend,
    )


@router.get("/report", response_model=OrgReport)
async def report(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> OrgReport:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.report(),
    )


@router.get("/artifacts", response_model=list[ArtifactSummary])
async def artifacts(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    limit: int = 50,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[ArtifactSummary]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    bounded = max(1, min(limit, 200))  # a page, never the firehose
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.artifacts(limit=bounded),
    )


@router.get("/export")
async def export_workforce(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> dict[str, object]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.workforce.export_bundle(),
    )


# -- the human boundary (CO2): CEO proposals decided by a person, never a model ---------------


class ApprovalsPageMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    next_cursor: str | None
    has_more: bool


class ApprovalsPageLinks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    self: str
    next: str | None


class ApprovalsPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: tuple[ApprovalView, ...]
    meta: ApprovalsPageMeta
    links: ApprovalsPageLinks


class ApprovalDetailMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApprovalDetailLinks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    self: str


class ApprovalDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: ApprovalView
    meta: ApprovalDetailMeta
    links: ApprovalDetailLinks


class ApprovalDecisionRequest(BaseModel):
    """One human verdict for a pending approval gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["approve", "deny", "request_revision", "hold"]


class ApprovalDecisionLinks(BaseModel):
    """Links for a decision proof; its approval remains the retrievable representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval: str


class ApprovalDecisionResponse(BaseModel):
    """The immutable decision proof in the control API's typed envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: ApprovalDecisionView
    links: ApprovalDecisionLinks


IdempotencyKeyHeader = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=128),
]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match", min_length=1, max_length=256)]


def _approval_cursor(created_at: datetime, approval_id: str) -> str:
    value = f"{created_at.astimezone(UTC).isoformat()}|{approval_id}".encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _cursor_key(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at_text, separator, approval_id = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode().partition("|")
        created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
        canonical_id = str(uuid.UUID(approval_id))
    except (UnicodeDecodeError, ValueError, binascii.Error):
        raise HTTPException(status_code=422, detail="invalid cursor") from None
    if (
        not separator
        or "|" in approval_id
        or created_at.utcoffset() != timedelta()
        or canonical_id != approval_id
        or cursor
        not in {
            _approval_cursor(created_at, approval_id),
            base64.urlsafe_b64encode(
                f"{created_at.astimezone(UTC).isoformat().replace('+00:00', 'Z')}|{approval_id}".encode()
            )
            .decode()
            .rstrip("="),
        }
    ):
        raise HTTPException(status_code=422, detail="invalid cursor")
    return created_at.astimezone(UTC), approval_id


def _approval_page(
    approvals: list[ApprovalView], *, cursor: tuple[datetime, str] | None, limit: int
) -> tuple[tuple[ApprovalView, ...], bool]:
    ordered = sorted(approvals, key=lambda approval: (approval.created_at, approval.id))
    after_cursor = (
        [approval for approval in ordered if (approval.created_at, approval.id) > cursor]
        if cursor is not None
        else ordered
    )
    return tuple(after_cursor[:limit]), len(after_cursor) > limit


def _approvals_link(
    workspace_id: uuid.UUID, company_id: uuid.UUID, *, cursor: str | None, limit: int
) -> str:
    query: list[tuple[str, str]] = [("status", "pending"), ("limit", str(limit))]
    if cursor is not None:
        query.append(("cursor", cursor))
    path = f"/v1/workspaces/{workspace_id}/companies/{company_id}/approvals"
    return f"{path}?{urlencode(query)}"


@router.get("/approvals", response_model=ApprovalsPage)
async def approvals(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    status: Literal["pending"] = "pending",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> ApprovalsPage:
    """Pending human gates, oldest first; no other approval status is readable yet."""
    del status
    if set(request.query_params) - {"status", "cursor", "limit"}:
        raise HTTPException(status_code=422, detail="unsupported query parameter")
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    # ponytail: pending() snapshots all gates; add a native bounded keyset query if volume warrants it.
    pending = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.governance.pending_approvals(),
    )
    page, has_more = _approval_page(pending, cursor=_cursor_key(cursor) if cursor else None, limit=limit)
    next_cursor = _approval_cursor(page[-1].created_at, page[-1].id) if has_more else None
    return ApprovalsPage(
        data=page,
        meta=ApprovalsPageMeta(next_cursor=next_cursor, has_more=has_more),
        links=ApprovalsPageLinks(
            self=_approvals_link(workspace_id, company_id, cursor=cursor, limit=limit),
            next=(
                _approvals_link(workspace_id, company_id, cursor=next_cursor, limit=limit)
                if next_cursor is not None
                else None
            ),
        ),
    )


def _approval_etag(view: ApprovalView) -> str:
    """A strong validator over every approval field the detail door returns."""
    payload = json.dumps(view.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return f'"{hashlib.sha256(payload.encode()).hexdigest()}"'


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    return value.strip() == "*" or any(
        validator.strip().removeprefix("W/") == etag for validator in value.split(",")
    )


def _if_match_matches(value: str | None, etag: str) -> bool:
    """Require exactly one strong ETag: wildcard and weak validators are unsafe for mutation."""
    return value is not None and value.strip() == etag


def _approval_decision_nonce(idempotency_key: str) -> str:
    """Never persist a raw HTTP idempotency key in Chorus's authorization ledger."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _approval_decision_request_hash(
    approval_id: str, body: ApprovalDecisionRequest
) -> str:
    """Bind idempotency to both the approval target and the full typed decision body."""
    canonical = json.dumps(
        {"approval_id": approval_id, "verdict": body.verdict},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _approval_detail_link(
    workspace_id: uuid.UUID, company_id: uuid.UUID, approval_id: str
) -> str:
    return f"/v1/workspaces/{workspace_id}/companies/{company_id}/approvals/{approval_id}"


def _decision_response(
    decision: ApprovalDecisionView,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    approval_id: str,
) -> ApprovalDecisionResponse:
    return ApprovalDecisionResponse(
        data=decision,
        links=ApprovalDecisionLinks(
            approval=_approval_detail_link(workspace_id, company_id, approval_id)
        ),
    )


def _is_same_decision_request(
    decision: ApprovalDecisionView,
    *,
    approval_id: str,
    user_id: str,
    method: AuthenticationMethod,
    request_hash: str,
    verdict: str,
) -> bool:
    return (
        decision.approval_id == approval_id
        and decision.user_id == user_id
        and decision.method is method
        and decision.request_hash == request_hash
        and decision.verdict.value == verdict
    )


def _idempotency_reuse_error() -> ProblemHTTPException:
    return ProblemHTTPException(
        status_code=409,
        code="idempotency_key_reuse",
        detail="Idempotency-Key was already used for a different approval decision",
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def approval(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    approval_id: str,
    request: Request,
    response: Response,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> ApprovalDetail | Response:
    """One persisted gate, including resolved and expired records."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    view = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.governance.approval(approval_id),
    )
    if view is None:
        raise HTTPException(status_code=404, detail="approval not found")
    etag = _approval_etag(view)
    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return ApprovalDetail(
        data=view,
        meta=ApprovalDetailMeta(),
        links=ApprovalDetailLinks(
            self=f"/v1/workspaces/{workspace_id}/companies/{company_id}/approvals/{approval_id}"
        ),
    )


@router.post(
    "/approvals/{approval_id}/decisions",
    status_code=201,
    response_model=ApprovalDecisionResponse,
    responses={
        200: {
            "model": ApprovalDecisionResponse,
            "headers": {
                "Idempotency-Replayed": {
                    "description": "True when the response is the immutable prior decision.",
                    "schema": {"type": "boolean"},
                }
            },
        },
        412: {"description": "If-Match does not match the current approval ETag."},
        428: {"description": "A strong If-Match header is required before a first mutation."},
    },
)
async def decide_approval(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    approval_id: str,
    body: ApprovalDecisionRequest,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> ApprovalDecisionResponse:
    """Record one authenticated human approval verdict, or replay its immutable proof."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    if actor.user_id is None:
        raise ProblemHTTPException(
            status_code=403,
            code="human_actor_required",
            detail="a user API key is required to decide an approval",
        )
    if idempotency_key is None:
        raise ProblemHTTPException(
            status_code=422,
            code="idempotency_key_required",
            detail="Idempotency-Key is required",
        )

    actor_user_id = str(actor.user_id)
    nonce = _approval_decision_nonce(idempotency_key)
    request_hash = _approval_decision_request_hash(approval_id, body)
    existing = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.governance.authorization_proof_by_nonce(nonce),
    )
    if existing is not None:
        if not _is_same_decision_request(
            existing,
            approval_id=approval_id,
            user_id=actor_user_id,
            method=AuthenticationMethod.API_KEY,
            request_hash=request_hash,
            verdict=body.verdict,
        ):
            raise _idempotency_reuse_error()
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
        return _decision_response(
            existing,
            workspace_id=workspace_id,
            company_id=company_id,
            approval_id=approval_id,
        )

    view = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.governance.approval(approval_id),
    )
    if view is None:
        raise HTTPException(status_code=404, detail="approval not found")
    etag = _approval_etag(view)
    if if_match is None:
        raise ProblemHTTPException(
            status_code=428,
            code="precondition_required",
            detail="a strong If-Match header is required",
        )
    if not _if_match_matches(if_match, etag):
        raise ProblemHTTPException(
            status_code=412,
            code="precondition_failed",
            detail="If-Match does not match the current approval ETag",
        )
    if view.status != "pending":
        raise ProblemHTTPException(
            status_code=409,
            code="approval_not_pending",
            detail="approval is not pending",
        )

    now = datetime.now(UTC)
    authorization = HumanAuthorization(
        decision_id=str(uuid.uuid4()),
        user_id=actor_user_id,
        method=AuthenticationMethod.API_KEY,
        authenticated_at=now,
        nonce=nonce,
        decided_at=now,
        request_id=get_request_id(),
        request_hash=request_hash,
    )
    try:
        decision = await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.governance.decide_approval(
                approval_id,
                verdict=body.verdict,
                authorization=authorization,
            ),
        )
    except (GovernanceError, LedgerIntegrityError, OrgInvariantViolation, ValueError) as exc:
        raced = await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.governance.authorization_proof_by_nonce(nonce),
        )
        if raced is not None:
            if not _is_same_decision_request(
                raced,
                approval_id=approval_id,
                user_id=actor_user_id,
                method=AuthenticationMethod.API_KEY,
                request_hash=request_hash,
                verdict=body.verdict,
            ):
                raise _idempotency_reuse_error() from exc
            response.status_code = 200
            response.headers["Idempotency-Replayed"] = "true"
            return _decision_response(
                raced,
                workspace_id=workspace_id,
                company_id=company_id,
                approval_id=approval_id,
            )
        raise ProblemHTTPException(
            status_code=409,
            code="approval_conflict",
            detail="approval could not be decided in its current state",
        ) from exc
    return _decision_response(
        decision,
        workspace_id=workspace_id,
        company_id=company_id,
        approval_id=approval_id,
    )


@router.get("/plans", response_model=list[PlanView])
async def workforce_plans(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[PlanView]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.governance.plans(),
    )


async def _decide_plan(
    *,
    approve: bool,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    plan_id: str,
    actor: Actor,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: ControlPlaneProvider,
) -> PlanView:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    decided_by = str(
        actor.user_id or actor.actor_id
    )  # the authenticated human/key, never client-supplied
    try:
        return await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: (
                plane.governance.approve(plan_id, by=decided_by)
                if approve
                else plane.governance.reject(plan_id, by=decided_by)
            ),
        )
    except UnknownPlanError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrgInvariantViolation as exc:  # a stale plan the org has since outgrown
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # engine invariant (e.g. profile versioning) — a conflict, not a 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/approve", response_model=PlanView)
async def approve_plan(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    plan_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> PlanView:
    """Atomically materialize the CEO's proposal — employees, grants, budgets, audit trail."""
    return await _decide_plan(
        approve=True,
        workspace_id=workspace_id,
        company_id=company_id,
        plan_id=plan_id,
        actor=actor,
        sessionmaker=sessionmaker,
        provider=provider,
    )


@router.post("/plans/{plan_id}/reject", response_model=PlanView)
async def reject_plan(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    plan_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> PlanView:
    """Decline without touching the workforce; the CEO may propose a fresh revision."""
    return await _decide_plan(
        approve=False,
        workspace_id=workspace_id,
        company_id=company_id,
        plan_id=plan_id,
        actor=actor,
        sessionmaker=sessionmaker,
        provider=provider,
    )


@router.get("/routines", response_model=list[RoutineSummary])
async def routines(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[RoutineSummary]:
    """The company's standing heartbeats — every routine hire provisioned, any status."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.routines.list(),
    )


async def _routine_action(
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: ControlPlaneProvider,
    act: Callable[[CompanyControlPlane], _T],
) -> _T:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        return await _plane_read(
            provider, workspace_id=workspace_id, company_id=company_id, read=act
        )
    except UnknownRoutineError as exc:
        raise HTTPException(status_code=404, detail="routine not found") from exc
    except RoutineFireConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/routines/{routine_id}/pause", response_model=RoutineSummary)
async def pause_routine(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    routine_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> RoutineSummary:
    return await _routine_action(
        workspace_id=workspace_id,
        company_id=company_id,
        actor=actor,
        sessionmaker=sessionmaker,
        provider=provider,
        act=lambda plane: plane.routines.pause(routine_id),
    )


@router.post("/routines/{routine_id}/resume", response_model=RoutineSummary)
async def resume_routine(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    routine_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> RoutineSummary:
    return await _routine_action(
        workspace_id=workspace_id,
        company_id=company_id,
        actor=actor,
        sessionmaker=sessionmaker,
        provider=provider,
        act=lambda plane: plane.routines.resume(routine_id),
    )


@router.post("/routines/{routine_id}/fire")
async def fire_routine_now(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    routine_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> dict[str, str]:
    """Fire the routine now through the engine's cron path; the conductor's pulse runs the task."""
    task_id = await _routine_action(
        workspace_id=workspace_id,
        company_id=company_id,
        actor=actor,
        sessionmaker=sessionmaker,
        provider=provider,
        act=lambda plane: plane.routines.fire(routine_id),
    )
    return {"task_id": task_id}


@router.get("/tasks/{task_id}/why", response_model=list[WhyLink])
async def task_why(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    task_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[WhyLink]:
    """The task's why-chain, leaf-first: task lineage, then goal lineage to the company root."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        return await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.observe.why(task_id),
        )
    except UnknownTaskError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


class CommentCreate(BaseModel):
    body: str


@router.get("/tasks/{task_id}/comments", response_model=list[CommentView])
async def task_comments(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    task_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[CommentView]:
    """The task's comment thread, oldest first — shared context, not a private inbox."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        return await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.comments.thread(task_id),
        )
    except UnknownTaskError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.post("/tasks/{task_id}/comments", status_code=201, response_model=CommentView)
async def post_task_comment(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    task_id: str,
    body: CommentCreate,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> CommentView:
    """The human joins the thread; delivery wakes whoever the task concerns."""
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    author = str(actor.user_id or actor.workspace_id)  # the authenticated actor, never client-supplied
    try:
        return await _plane_read(
            provider,
            workspace_id=workspace_id,
            company_id=company_id,
            read=lambda plane: plane.comments.post(task_id, body=body.body, by_user=author),
        )
    except UnknownTaskError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except UndeliverableCommentError as exc:
        raise HTTPException(status_code=409, detail="no one to notify on this task") from exc


class BudgetPatch(BaseModel):
    amount_cents: int


async def _employee_action(
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: ControlPlaneProvider,
    act: Callable[[CompanyControlPlane], _T],
) -> _T:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    try:
        return await _plane_read(
            provider, workspace_id=workspace_id, company_id=company_id, read=act
        )
    except UnknownEmployeeError as exc:
        raise HTTPException(status_code=404, detail="employee not found") from exc
    except EmployeeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/employees/{employee_id}/pause", response_model=EmployeeView)
async def pause_employee(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> EmployeeView:
    """Board control (OM-4): pause any employee — no new beat dispatches until resumed."""
    return await _employee_action(
        workspace_id=workspace_id, company_id=company_id, actor=actor,
        sessionmaker=sessionmaker, provider=provider,
        act=lambda plane: plane.workforce.pause(employee_id),
    )


@router.post("/employees/{employee_id}/resume", response_model=EmployeeView)
async def resume_employee(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> EmployeeView:
    return await _employee_action(
        workspace_id=workspace_id, company_id=company_id, actor=actor,
        sessionmaker=sessionmaker, provider=provider,
        act=lambda plane: plane.workforce.resume(employee_id),
    )


@router.patch("/employees/{employee_id}/budget", response_model=BudgetView)
async def patch_employee_budget(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    body: BudgetPatch,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> BudgetView:
    """Board control (OM-4): the employee's monthly spend cap; the hard ceiling auto-stops."""
    return await _employee_action(
        workspace_id=workspace_id, company_id=company_id, actor=actor,
        sessionmaker=sessionmaker, provider=provider,
        act=lambda plane: plane.workforce.set_budget(employee_id, amount_cents=body.amount_cents),
    )
