"""DelegationFacade — teams and capacity (chorus M8 surfaces) as podium DTOs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chorus.adapters import CapacityAdapter
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger


class TeamSummary(BaseModel):
    """One durable team with its current membership."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    lead: str  # lead employee slug
    status: str  # engine TeamStatus value: forming|active|dissolved|…
    members: list[str]  # member employee slugs (the lead is not repeated here)


class CapacityEntry(BaseModel):
    """Observed execution + budget facts for one profession (the engine's own aggregate)."""

    model_config = ConfigDict(frozen=True)

    role: str
    eligible: int  # employees invokable right now
    running: int
    assigned: int  # non-terminal tasks assigned to this profession
    queued: int  # queued wakes
    budget_blocked: int
    budget_headroom_cents: int | None  # None = no budget cap configured


class DelegationFacade:
    """Pure delegation to chorus's team tables + CapacityAdapter; translation only."""

    def __init__(self, ledger: Ledger, company_id: str) -> None:
        self._ledger = ledger
        self._company_id = company_id

    def teams(self) -> list[TeamSummary]:
        """Every team, engine order, with member slugs resolved."""
        return [
            TeamSummary(
                id=team.id,
                name=team.name,
                lead=team.lead_employee_id,
                status=team.status.value,
                members=[
                    member.employee_id for member in self._ledger.team_members.members_of(team.id)
                ],
            )
            for team in self._ledger.teams.list()
        ]

    def capacity(self) -> list[CapacityEntry]:
        """Profession aggregates from the engine's own CapacityAdapter (never re-derived)."""
        snapshot = CapacityAdapter(self._ledger, company_id=self._company_id).snapshot()
        return [
            CapacityEntry(
                role=entry.profession,
                eligible=entry.eligible,
                running=entry.running,
                assigned=entry.assigned_nonterminal,
                queued=entry.queued_wakes,
                budget_blocked=entry.budget_blocked,
                budget_headroom_cents=entry.budget_headroom_cents,
            )
            for entry in snapshot
        ]


__all__ = ["CapacityEntry", "DelegationFacade", "TeamSummary"]
