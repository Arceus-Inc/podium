"""ObserveFacade — read-only telemetry: company status (LedgerInspector) + evolved skills."""

from __future__ import annotations

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


__all__ = ["CompanyStatus", "ObserveFacade", "OrgReport", "SkillSummary", "SpendRow"]
