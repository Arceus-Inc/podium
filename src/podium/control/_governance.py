"""GovernanceFacade — the human boundary over CEO workforce proposals (the T2 pattern).

The CEO's ledger-bound tool leaves a typed plan ``proposed``; these calls are the only way it
materializes. Approve delegates to the engine's WorkforcePlanService — employees, bounded
management grants, budgets, and the audit trail land atomically or not at all."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from chorus.ids import mint_id
from chorus.ledger import (
    LedgerIntegrityError,
    ReflectionApplicationAuthorization,
    ReflectionProposalReview,
    ReflectionProposalVerdict,
)
from pydantic import BaseModel, ConfigDict

from podium.control._observe import UnknownReflectionProposalError

if TYPE_CHECKING:
    from chorus.governance import WorkforcePlanService
    from chorus.ledger import Ledger, WorkforcePlan


class UnknownPlanError(ValueError):
    """No such workforce plan in this company."""


class PlanConflictError(ValueError):
    """The plan is not in a decidable state (already applied/rejected/superseded)."""


class ReflectionProposalAlreadyReviewedError(ValueError):
    """The proposal revision already has its one final human verdict."""


class ReflectionApplicationAlreadyAuthorizedError(ValueError):
    """The proposal already has its single-use application authorization."""


class ReflectionApplicationConflictError(ValueError):
    """The accepted review or queued-run invariants do not authorize this handoff."""


class UnknownApplicationRunError(ValueError):
    """No such application run in this company."""


class ReflectionApplicationAuthorizationView(BaseModel):
    """The auditable handoff from an accepted proposal to one separate queued run."""

    model_config = ConfigDict(frozen=True)

    id: str
    proposal_artifact_revision_id: str
    review_id: str
    proposal_source_run_id: str
    application_run_id: str
    authorized_by_user_id: str
    created_at: datetime


class ReflectionProposalReviewView(BaseModel):
    """One authenticated final human verdict for an exact proposal revision."""

    model_config = ConfigDict(frozen=True)

    id: str
    proposal_artifact_revision_id: str
    verdict: Literal["accepted", "rejected"]
    reviewer_user_id: str
    reason: str
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

    def approve(self, plan_id: str, *, by: str) -> PlanView:
        """Atomically materialize the latest valid proposal as an audited human decision."""
        return self._decide(plan_id, by=by, approve=True)

    def reject(self, plan_id: str, *, by: str) -> PlanView:
        """Reject the latest proposed revision without touching the workforce."""
        return self._decide(plan_id, by=by, approve=False)

    def review_reflection_proposal(
        self,
        artifact_revision_id: str,
        *,
        verdict: Literal["accepted", "rejected"],
        by: str,
        reason: str,
    ) -> ReflectionProposalReviewView:
        """Record the authenticated human's one final verdict without applying the diff."""
        try:
            uuid.UUID(artifact_revision_id)
        except ValueError:
            raise UnknownReflectionProposalError(artifact_revision_id) from None
        if self._ledger.reflection_proposals.get(artifact_revision_id) is None:
            raise UnknownReflectionProposalError(artifact_revision_id)
        if self._ledger.reflection_proposal_reviews.for_proposal(artifact_revision_id) is not None:
            raise ReflectionProposalAlreadyReviewedError(artifact_revision_id)
        review = ReflectionProposalReview(
            id=mint_id(),
            proposal_artifact_revision_id=artifact_revision_id,
            verdict=ReflectionProposalVerdict(verdict),
            reviewer_user_id=by,
            reason=reason,
        )
        try:
            recorded = self._ledger.reflection_proposal_reviews.record(review)
        except LedgerIntegrityError as exc:
            raise ReflectionProposalAlreadyReviewedError(artifact_revision_id) from exc
        if recorded.created_at is None:
            raise RuntimeError("persisted reflection proposal review is missing created_at")
        return ReflectionProposalReviewView(
            id=recorded.id,
            proposal_artifact_revision_id=recorded.proposal_artifact_revision_id,
            verdict=recorded.verdict.value,
            reviewer_user_id=recorded.reviewer_user_id,
            reason=recorded.reason,
            created_at=recorded.created_at,
        )

    def authorize_reflection_application(
        self,
        artifact_revision_id: str,
        *,
        application_run_id: str,
        by: str,
    ) -> ReflectionApplicationAuthorizationView:
        """Bind an accepted review to one existing queued run without executing that run."""
        try:
            uuid.UUID(artifact_revision_id)
        except ValueError:
            raise UnknownReflectionProposalError(artifact_revision_id) from None
        proposal = self._ledger.reflection_proposals.get(artifact_revision_id)
        if proposal is None:
            raise UnknownReflectionProposalError(artifact_revision_id)
        if (
            self._ledger.reflection_application_authorizations.for_proposal(
                artifact_revision_id
            )
            is not None
        ):
            raise ReflectionApplicationAlreadyAuthorizedError(artifact_revision_id)

        review = self._ledger.reflection_proposal_reviews.accepted(artifact_revision_id)
        if review is None or review.reviewer_user_id != by:
            raise ReflectionApplicationConflictError(
                "reflection application requires the authenticated reviewer's accepted verdict"
            )
        try:
            uuid.UUID(application_run_id)
        except ValueError:
            raise UnknownApplicationRunError(application_run_id) from None
        if self._ledger.runs.get(application_run_id) is None:
            raise UnknownApplicationRunError(application_run_id)

        authorization = ReflectionApplicationAuthorization(
            id=mint_id(),
            proposal_artifact_revision_id=artifact_revision_id,
            review_id=review.id,
            proposal_source_run_id=proposal.source_run_id,
            application_run_id=application_run_id,
            authorized_by_user_id=by,
        )
        try:
            recorded = self._ledger.reflection_application_authorizations.issue(authorization)
        except LedgerIntegrityError as exc:
            raise ReflectionApplicationAlreadyAuthorizedError(artifact_revision_id) from exc
        except ValueError as exc:
            raise ReflectionApplicationConflictError(str(exc)) from exc
        if recorded.created_at is None:
            raise RuntimeError("persisted reflection application authorization is missing created_at")
        return ReflectionApplicationAuthorizationView(
            id=recorded.id,
            proposal_artifact_revision_id=recorded.proposal_artifact_revision_id,
            review_id=recorded.review_id,
            proposal_source_run_id=recorded.proposal_source_run_id,
            application_run_id=recorded.application_run_id,
            authorized_by_user_id=recorded.authorized_by_user_id,
            created_at=recorded.created_at,
        )

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
    "GovernanceFacade",
    "ManagementGrantView",
    "PlanConflictError",
    "PlanView",
    "PlannedEmployeeView",
    "ReflectionApplicationAlreadyAuthorizedError",
    "ReflectionApplicationAuthorizationView",
    "ReflectionApplicationConflictError",
    "ReflectionProposalAlreadyReviewedError",
    "ReflectionProposalReviewView",
    "UnknownApplicationRunError",
    "UnknownPlanError",
]
