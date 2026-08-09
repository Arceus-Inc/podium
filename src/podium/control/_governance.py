"""GovernanceFacade — the human boundary over CEO workforce proposals (the T2 pattern).

The CEO's ledger-bound tool leaves a typed plan ``proposed``; these calls are the only way it
materializes. Approve delegates to the engine's WorkforcePlanService — employees, bounded
management grants, budgets, and the audit trail land atomically or not at all."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeAlias

from chorus.ledger import (
    Approval,
    ApprovalAction,
    ApprovalGate,
    ApprovalStatus,
    ApprovalSubjectKind,
)
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.governance import WorkforcePlanService
    from chorus.ledger import Ledger, WorkforcePlan


class UnknownPlanError(ValueError):
    """No such workforce plan in this company."""


class PlanConflictError(ValueError):
    """The plan is not in a decidable state (already applied/rejected/superseded)."""


class TaskSubjectRef(BaseModel):
    """The task an approval gates."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["task"] = "task"
    id: str


class ArtifactSubjectRef(BaseModel):
    """The artifact an approval gates."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["artifact"] = "artifact"
    id: str


class EmployeeSubjectRef(BaseModel):
    """The employee a hire approval gates."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["employee"] = "employee"
    id: str


class BudgetIncidentSubjectRef(BaseModel):
    """The budget incident an override approval gates."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["budget_incident"] = "budget_incident"
    id: str


ApprovalSubjectRef: TypeAlias = (
    TaskSubjectRef | ArtifactSubjectRef | EmployeeSubjectRef | BudgetIncidentSubjectRef
)


class ApprovalView(BaseModel):
    """One persisted human gate, projected directly from Chorus's approval ledger."""

    model_config = ConfigDict(frozen=True)

    id: str
    subject: ApprovalSubjectRef
    reason: str
    action: ApprovalAction
    status: ApprovalStatus
    gate_kind: ApprovalGate | None
    expires_at: datetime | None
    created_at: datetime


class PlannedEmployeeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    name: str
    profession: str
    reports_to: str
    budget_cents: int | None


class ManagementGrantView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_ref: str
    can_lead: bool
    can_subdelegate: bool
    max_delegation_depth: int
    max_team_size: int
    allowed_professions: tuple[str, ...]
    spend_limit_cents: int | None


class PlanView(BaseModel):
    """One immutable workforce-plan revision — the approval inbox's card."""

    model_config = ConfigDict(frozen=True)

    id: str
    revision: int
    status: str  # proposed|superseded|rejected|applied
    proposed_by: str
    decided_by: str | None
    rationale: str
    confidence: float
    source_goal_ids: tuple[str, ...]
    employees: tuple[PlannedEmployeeView, ...]
    grants: tuple[ManagementGrantView, ...]
    created_at: str | None
    decided_at: str | None


def _view(plan: WorkforcePlan) -> PlanView:
    return PlanView(
        id=plan.id,
        revision=plan.revision,
        status=plan.status.value,
        proposed_by=plan.proposed_by_employee_id,
        decided_by=plan.decided_by_user_id,
        rationale=plan.draft.rationale,
        confidence=plan.draft.confidence,
        source_goal_ids=plan.draft.source_goal_ids,
        employees=tuple(
            PlannedEmployeeView(
                ref=employee.ref,
                name=employee.name,
                profession=employee.profession,
                reports_to=employee.reports_to_ref,
                budget_cents=employee.budget_cents,
            )
            for employee in plan.draft.employees
        ),
        grants=tuple(
            ManagementGrantView(
                employee_ref=grant.employee_ref,
                can_lead=grant.can_lead,
                can_subdelegate=grant.can_subdelegate,
                max_delegation_depth=grant.max_delegation_depth,
                max_team_size=grant.max_team_size,
                allowed_professions=grant.allowed_professions,
                spend_limit_cents=grant.spend_limit_cents,
            )
            for grant in plan.draft.management_grants
        ),
        created_at=plan.created_at.isoformat() if plan.created_at else None,
        decided_at=plan.decided_at.isoformat() if plan.decided_at else None,
    )


def _subject_view(approval: Approval) -> ApprovalSubjectRef:
    match approval.subject_kind:
        case ApprovalSubjectKind.TASK:
            return TaskSubjectRef(id=approval.subject_id)
        case ApprovalSubjectKind.ARTIFACT:
            return ArtifactSubjectRef(id=approval.subject_id)
        case ApprovalSubjectKind.EMPLOYEE:
            return EmployeeSubjectRef(id=approval.subject_id)
        case ApprovalSubjectKind.BUDGET_INCIDENT:
            return BudgetIncidentSubjectRef(id=approval.subject_id)


def _require_created_at(approval: Approval) -> datetime:
    """Chorus assigns every persisted approval a creation timestamp."""
    if approval.created_at is None:
        raise RuntimeError(f"persisted approval {approval.id!r} has no created_at")
    return approval.created_at


def _approval_view(approval: Approval) -> ApprovalView:
    return ApprovalView(
        id=approval.id,
        subject=_subject_view(approval),
        reason=approval.reason,
        action=approval.action,
        status=approval.status,
        gate_kind=approval.gate_kind,
        expires_at=approval.expires_at,
        created_at=_require_created_at(approval),
    )


class GovernanceFacade:
    """Pure delegation to the engine's plan service; translation, never business logic."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def _service(self) -> WorkforcePlanService:
        from chorus.governance import WorkforcePlanService
        from chorus.roles import RoleRegistry, default_roles
        from chorus.workforce._ledger import LedgerWorkforce

        return WorkforcePlanService(
            self._ledger,
            workforce=LedgerWorkforce(self._ledger.employees),
            roles=RoleRegistry.from_plugins(default_roles()),
            max_org_depth=2,
        )

    def plans(self) -> list[PlanView]:
        """Every persisted plan revision, newest last — proposed ones are the pending inbox."""
        return [_view(plan) for plan in self._ledger.workforce_plans.list()]

    def pending_approvals(self) -> list[ApprovalView]:
        """Open approval gates, oldest first; Chorus excludes expired gates itself."""
        return [_approval_view(approval) for approval in self._ledger.approvals.pending()]

    def approval(self, approval_id: str) -> ApprovalView | None:
        """One persisted gate in this company, whatever its status."""
        try:
            uuid.UUID(approval_id)
        except ValueError:
            return None
        approval = self._ledger.approvals.get(approval_id)
        return _approval_view(approval) if approval is not None else None

    def approve(self, plan_id: str, *, by: str) -> PlanView:
        """Atomically materialize the latest valid proposal as an audited human decision."""
        return self._decide(plan_id, by=by, approve=True)

    def reject(self, plan_id: str, *, by: str) -> PlanView:
        """Reject the latest proposed revision without touching the workforce."""
        return self._decide(plan_id, by=by, approve=False)

    def _decide(self, plan_id: str, *, by: str, approve: bool) -> PlanView:
        import uuid

        from chorus.ledger import WorkforcePlanStatus

        try:
            uuid.UUID(plan_id)  # engine plan ids are uuid text; anything else can't exist
        except ValueError:
            raise UnknownPlanError(plan_id) from None
        latest = self._ledger.workforce_plans.latest(plan_id)
        if latest is None:
            raise UnknownPlanError(plan_id)
        if latest.status is not WorkforcePlanStatus.PROPOSED:
            raise PlanConflictError(f"plan {plan_id} is {latest.status.value}, not proposed")
        service = self._service()
        decided = (
            service.approve(plan_id, approved_by_user_id=by)
            if approve
            else service.reject(plan_id, rejected_by_user_id=by)
        )
        return _view(decided)


__all__ = [
    "ApprovalView",
    "GovernanceFacade",
    "ManagementGrantView",
    "PlanConflictError",
    "PlanView",
    "PlannedEmployeeView",
    "UnknownPlanError",
]
