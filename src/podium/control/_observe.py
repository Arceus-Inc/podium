"""ObserveFacade — read-only telemetry: company status (LedgerInspector) + evolved skills."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from chorus.observability import LedgerInspector
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger


class CompanyStatus(BaseModel):
    """The company at a glance — counts only; drill-downs are their own views."""

    model_config = ConfigDict(frozen=True)

    employees: int
    open_tasks: int
    running_beats: int
    blocked_tasks: int
    open_incidents: int


class SkillSummary(BaseModel):
    """One evolved/created skill HEAD (the engine's procedural memory, read-only)."""

    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    name: str
    origin: str  # canonical|evolved|created
    state: str  # active|stale|archived
    revision_no: int


class SkillRevisionView(BaseModel):
    """One immutable skill revision with its execution provenance."""

    model_config = ConfigDict(frozen=True)

    id: str
    revision_no: int
    action: str
    content_hash: str
    label: str | None
    source_run_refs: tuple[str, ...]
    author_run_ref: str | None
    restored_from_ref: str | None
    created_at: datetime


class ReflectionTrajectoryView(BaseModel):
    """One persisted run/task trajectory cited by a reflection proposal."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    task_id: str


class ReflectionProposalTargetView(BaseModel):
    """The exact owner and immutable revision a proposal would change."""

    model_config = ConfigDict(frozen=True)

    kind: str
    owner_employee_id: str
    target_id: str
    target_revision: str


class ReflectionProposalView(BaseModel):
    """A proposal-only visible diff with its complete provenance."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    artifact_revision_id: str
    target: ReflectionProposalTargetView
    diff: str
    rationale: str
    trajectory_refs: tuple[ReflectionTrajectoryView, ...]
    evidence_artifact_revision_ids: tuple[str, ...]
    source_routine_run_id: str
    source_run_id: str
    source_employee_id: str
    created_at: datetime


class OrgReport(BaseModel):
    """The inspector's combined manager+leaf rollup — flat counts for allocation decisions."""

    model_config = ConfigDict(frozen=True)

    employees: int
    managers: int
    leaves: int
    tasks_total: int
    tasks_done: int
    tasks_blocked: int
    running_beats: int
    failed_runs: int
    completion_rate: float
    decomposition_count: int
    assignment_count: int
    reassignment_count: int
    dependency_edges: int


class WhyLink(BaseModel):
    """One link in a task's why-chain — task lineage first, then goal lineage (OM-2)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # task|goal
    id: str
    label: str  # task intent or goal title


class UnknownTaskError(ValueError):
    """No such task in this company."""


class UnknownSkillError(ValueError):
    """No such employee-owned skill in this company."""


class UnknownReflectionProposalError(ValueError):
    """No such reflection proposal revision in this company."""


class ArtifactSummary(BaseModel):
    """One landed outcome — the product shape of an engine artifact."""

    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    type: str  # engine ArtifactType value: pr|doc|finding|…
    url: str | None
    review_state: str | None
    is_primary: bool


class SpendRow(BaseModel):
    """One aggregate of the priced spend ledger (chorus cost_event — the source of truth)."""

    model_config = ConfigDict(frozen=True)

    key: str  # model name | employee slug | ISO day, per the grouping
    cost_cents: int
    input_tokens: int
    output_tokens: int
    events: int


class ObserveFacade:
    """Pure delegation to LedgerInspector projections + the skills tables; translation only."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def why(self, task_id: str) -> list[WhyLink]:
        """The task's full "why am I doing this?" chain, leaf-first: the task, its parent
        tasks, then the goal lineage up to the company root (paperclip's mandatory parentage,
        rendered from the columns chorus already stores)."""
        import uuid as _uuid

        try:
            _uuid.UUID(task_id)  # engine task ids are uuid text; anything else can't exist
        except ValueError:
            raise UnknownTaskError(task_id) from None
        task = self._ledger.tasks.get(task_id)
        if task is None:
            raise UnknownTaskError(task_id)
        chain: list[WhyLink] = []
        goal_id = None
        while task is not None:
            chain.append(WhyLink(kind="task", id=task.id, label=task.intent))
            goal_id = task.goal_id or goal_id  # nearest declared goal wins
            task = self._ledger.tasks.get(task.parent_id) if task.parent_id else None
        goal = self._ledger.goals.get(goal_id) if goal_id else None
        while goal is not None:
            chain.append(WhyLink(kind="goal", id=goal.id, label=goal.title))
            goal = self._ledger.goals.get(goal.parent_id) if goal.parent_id else None
        return chain

    def status(self) -> CompanyStatus:
        """Counts from the engine's own status projection (never re-derived from events)."""
        status = LedgerInspector(self._ledger).status()
        return CompanyStatus(
            employees=len(status.employees),
            open_tasks=status.open_tasks,
            running_beats=status.running_beats,
            blocked_tasks=len(status.blocked),
            open_incidents=len(status.open_incidents),
        )

    def spend_total_cents(self) -> int:
        """Company-lifetime spend from the priced ledger."""
        return sum(group.cost_cents for group in self._ledger.cost_events.grouped("model"))

    def artifacts(self, *, limit: int) -> list[ArtifactSummary]:
        """The landed-outcomes index, newest first, bounded."""
        return [
            ArtifactSummary(
                id=artifact.id,
                task_id=artifact.task_id,
                type=artifact.type.value,
                url=artifact.url,
                review_state=artifact.review_state,
                is_primary=artifact.is_primary,
            )
            for artifact in self._ledger.artifacts.list_recent(limit=limit)
        ]

    def report(self) -> OrgReport:
        """The org rollup from the engine's own projection (manager packets stay engine-side)."""
        rollup = LedgerInspector(self._ledger).org_report()
        return OrgReport(
            employees=rollup.employees,
            managers=rollup.managers,
            leaves=rollup.leaves,
            tasks_total=rollup.tasks_total,
            tasks_done=rollup.tasks_done,
            tasks_blocked=rollup.tasks_blocked,
            running_beats=rollup.running_beats,
            failed_runs=rollup.failed_runs,
            completion_rate=rollup.completion_rate,
            decomposition_count=rollup.decomposition_count,
            assignment_count=rollup.assignment_count,
            reassignment_count=rollup.reassignment_count,
            dependency_edges=rollup.dependency_edges,
        )

    def costs(self, by: str) -> list[SpendRow]:
        """Spend grouped by the engine's own aggregate (model | employee | day)."""
        return [
            SpendRow(
                key=group.key,
                cost_cents=group.cost_cents,
                input_tokens=group.input_tokens,
                output_tokens=group.output_tokens,
                events=group.events,
            )
            for group in self._ledger.cost_events.grouped(by)
        ]

    def skills(self, employee_id: str) -> list[SkillSummary]:
        """One employee's active skill HEADs (evolution is internal — this is telemetry)."""
        return [
            SkillSummary(
                id=skill.id,
                slug=skill.slug,
                name=skill.name,
                origin=skill.origin.value,
                state=skill.state.value,
                revision_no=skill.latest_revision_no,
            )
            for skill in self._ledger.skills.list_active(employee_id)
        ]

    def skill_revisions(self, employee_id: str, skill_id: str) -> tuple[SkillRevisionView, ...]:
        """One employee-owned skill's immutable revisions, oldest first."""
        try:
            uuid.UUID(skill_id)
        except ValueError:
            raise UnknownSkillError(skill_id) from None
        if self._ledger.employees.get(employee_id) is None:
            raise UnknownSkillError(skill_id)
        skill = self._ledger.skills.get(skill_id)
        if skill is None or skill.employee_id != employee_id:
            raise UnknownSkillError(skill_id)
        revisions = self._ledger.skill_revisions.by_skill(skill_id)
        return tuple(
            SkillRevisionView(
                id=revision.id,
                revision_no=revision.revision_no,
                action=revision.action,
                content_hash=revision.content_hash,
                label=revision.label,
                source_run_refs=revision.source_run_ids,
                author_run_ref=revision.author_run_id,
                restored_from_ref=revision.restored_from_revision_id,
                created_at=_require_created_at(revision.created_at),
            )
            for revision in revisions
        )

    def reflection_proposal(self, artifact_revision_id: str) -> ReflectionProposalView:
        """One immutable reflection proposal with the visible diff and cited evidence."""
        try:
            uuid.UUID(artifact_revision_id)
        except ValueError:
            raise UnknownReflectionProposalError(artifact_revision_id) from None
        proposal = self._ledger.reflection_proposals.get(artifact_revision_id)
        if proposal is None:
            raise UnknownReflectionProposalError(artifact_revision_id)
        return ReflectionProposalView(
            artifact_id=proposal.artifact_id,
            artifact_revision_id=proposal.artifact_revision_id,
            target=ReflectionProposalTargetView(
                kind=proposal.target.kind.value,
                owner_employee_id=proposal.target.owner_employee_id,
                target_id=proposal.target.target_id,
                target_revision=proposal.target.target_revision,
            ),
            diff=proposal.diff,
            rationale=proposal.rationale,
            trajectory_refs=tuple(
                ReflectionTrajectoryView(run_id=reference.run_id, task_id=reference.task_id)
                for reference in proposal.trajectory_refs
            ),
            evidence_artifact_revision_ids=proposal.evidence_artifact_revision_ids,
            source_routine_run_id=proposal.source_routine_run_id,
            source_run_id=proposal.source_run_id,
            source_employee_id=proposal.source_employee_id,
            created_at=_require_created_at(proposal.created_at),
        )


def _require_created_at(created_at: datetime | None) -> datetime:
    if created_at is None:
        raise RuntimeError("persisted skill revision is missing created_at")
    return created_at


__all__ = [
    "ArtifactSummary",
    "CompanyStatus",
    "ObserveFacade",
    "OrgReport",
    "ReflectionProposalTargetView",
    "ReflectionProposalView",
    "ReflectionTrajectoryView",
    "SkillRevisionView",
    "SkillSummary",
    "SpendRow",
    "UnknownReflectionProposalError",
    "UnknownSkillError",
    "UnknownTaskError",
    "WhyLink",
]
