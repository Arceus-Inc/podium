"""DirectionFacade — Chorus goals plus Horizon's persisted direction state as Podium DTOs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from horizon.generation import Proposal
from horizon.model import StrategyRecord
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger
    from horizon.ports import DecisionRepository, ProposalRepository, StrategyRepository


class GoalNode(BaseModel):
    """One goal with its subtree — the product shape of the alignment tree."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    level: str  # engine GoalLevel value: company|team|personal|…
    status: str
    owner: str | None  # owning employee slug, if assigned
    children: list[GoalNode]


class DecisionView(BaseModel):
    """One persisted Horizon decision, expressed without engine model types."""

    model_config = ConfigDict(frozen=True)

    id: str
    statement: str
    status: str
    owner: str | None
    rationale: str
    goal_ids: tuple[str, ...]


class TaskOutcomeView(BaseModel):
    """One durable outcome attached to a strategy record's task."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    outcome: str | None
    revision: int | None


class StaffingRequirementView(BaseModel):
    """One strategy-owned staffing constraint."""

    model_config = ConfigDict(frozen=True)

    profession: str
    count: int
    coverage: str
    outcome_area: str | None


class StrategyView(BaseModel):
    """One complete Horizon strategy record, flattened into frozen product DTOs."""

    model_config = ConfigDict(frozen=True)

    goal_id: str
    title: str
    score: float
    health: str
    metric: str | None
    target: str | None
    evidence: tuple[str, ...]
    decision_id: str | None
    task_id: str | None
    root_task_id: str | None
    task_ids: tuple[str, ...]
    team_id: str | None
    lead_id: str | None
    task_outcomes: tuple[TaskOutcomeView, ...]
    outcome_event_ids: tuple[str, ...]
    delivery_shape: str
    lead_professions: tuple[str, ...]
    staffing_requirements: tuple[StaffingRequirementView, ...]
    passes: int
    fails: int
    last_outcome_at: str | None
    done: bool
    attempts: int
    needs_recovery: bool
    last_diagnostic: str


class CandidateGoalView(BaseModel):
    """A candidate goal proposed by Horizon's evidence-gated funnel."""

    model_config = ConfigDict(frozen=True)

    title: str
    metric: str
    target: str
    rationale: str
    score: float


class DirectionBriefView(BaseModel):
    """The analyst evidence attached to a pending direction proposal."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    recommendation: str
    rationale: str
    confidence: float
    risks: tuple[str, ...]
    candidate_goals: tuple[CandidateGoalView, ...]
    evidence_refs: tuple[str, ...]


class ProposalView(BaseModel):
    """One human-gated Horizon proposal, including its optional analyst brief."""

    model_config = ConfigDict(frozen=True)

    id: str
    status: str
    brief: DirectionBriefView | None
    decision_statement: str
    decision_rationale: str
    created_at: str
    decided_by: str | None
    decided_at: str | None
    linked_decision_id: str | None
    note: str


class DirectionFacade:
    """Read-only composition of Chorus goals and Horizon's direction repositories."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        decisions: DecisionRepository,
        strategy: StrategyRepository,
        proposals: ProposalRepository,
    ) -> None:
        self._ledger = ledger
        self._decisions = decisions
        self._strategy = strategy
        self._proposals = proposals

    def decisions(self) -> list[DecisionView]:
        """Every Horizon decision in the repository's durable insertion order."""
        return [
            DecisionView(
                id=decision.id,
                statement=decision.statement,
                status=decision.status,
                owner=decision.owner,
                rationale=decision.rationale,
                goal_ids=tuple(decision.goal_ids),
            )
            for decision in self._decisions.all()
        ]

    def strategies(self) -> list[StrategyView]:
        """Every Horizon strategy record as a frozen product view."""
        return [self._strategy_view(record) for record in self._strategy.all()]

    def proposals(self) -> list[ProposalView]:
        """Every human-gated direction proposal, including evidence when present."""
        return [self._proposal_view(proposal) for proposal in self._proposals.all()]

    def create_goal(
        self, *, title: str, level: str, parent_id: str | None = None
    ) -> GoalNode | None:
        """Seed direction: a new goal (root, or a child of an existing one). Returns None when
        the named parent does not exist — a child must attach to real direction."""
        from chorus.ids import mint_id
        from chorus.ledger import Goal, GoalLevel

        if parent_id is not None and self._ledger.goals.get(parent_id) is None:
            return None
        goal = self._ledger.goals.create(
            Goal(id=mint_id(), title=title, level=GoalLevel(level), parent_id=parent_id)
        )
        return GoalNode(
            id=goal.id,
            title=goal.title,
            level=goal.level.value,
            status=goal.status,
            owner=goal.owner_employee_id,
            children=[],
        )

    def set_goal_status(self, goal_id: str, status: str) -> GoalNode | None:
        """Archive/restore a goal — an execution-independent ledger write (M4 §3.2).
        Returns the updated node (children omitted), or None when the goal is unknown."""
        from dataclasses import replace

        goal = self._ledger.goals.get(goal_id)
        if goal is None:
            return None
        updated = self._ledger.goals.update(replace(goal, status=status))
        return GoalNode(
            id=updated.id,
            title=updated.title,
            level=updated.level.value,
            status=updated.status,
            owner=updated.owner_employee_id,
            children=[],
        )

    def goal_tree(self) -> list[GoalNode]:
        """Every root goal with its subtree, engine order."""
        return self._subtrees(parent_id=None, seen=set())

    def _subtrees(self, *, parent_id: str | None, seen: set[str]) -> list[GoalNode]:
        # ponytail: one children() query per node — fine for real trees (tens of goals);
        # switch to one recursive CTE if trees ever grow past that.
        nodes: list[GoalNode] = []
        for goal in self._ledger.goals.children(parent_id):
            if goal.id in seen:  # corrupt parent edge — render the node, never recurse a cycle
                continue
            nodes.append(
                GoalNode(
                    id=goal.id,
                    title=goal.title,
                    level=goal.level.value,
                    status=goal.status,
                    owner=goal.owner_employee_id,
                    children=self._subtrees(parent_id=goal.id, seen=seen | {goal.id}),
                )
            )
        return nodes

    @staticmethod
    def _strategy_view(record: StrategyRecord) -> StrategyView:
        task_ids = sorted(set(record.task_outcomes) | set(record.task_outcome_revisions))
        return StrategyView(
            goal_id=record.goal_id,
            title=record.title,
            score=record.score,
            health=record.health,
            metric=record.metric,
            target=record.target,
            evidence=tuple(record.evidence),
            decision_id=record.decision_id,
            task_id=record.task_id,
            root_task_id=record.root_task_id,
            task_ids=tuple(record.task_ids),
            team_id=record.team_id,
            lead_id=record.lead_id,
            task_outcomes=tuple(
                TaskOutcomeView(
                    task_id=task_id,
                    outcome=record.task_outcomes.get(task_id),
                    revision=record.task_outcome_revisions.get(task_id),
                )
                for task_id in task_ids
            ),
            outcome_event_ids=tuple(record.outcome_event_ids),
            delivery_shape=record.delivery_shape,
            lead_professions=record.lead_professions,
            staffing_requirements=tuple(
                StaffingRequirementView(
                    profession=requirement.profession,
                    count=requirement.count,
                    coverage=requirement.coverage,
                    outcome_area=requirement.outcome_area,
                )
                for requirement in record.staffing_requirements
            ),
            passes=record.passes,
            fails=record.fails,
            last_outcome_at=record.last_outcome_at,
            done=record.done,
            attempts=record.attempts,
            needs_recovery=record.needs_recovery,
            last_diagnostic=record.last_diagnostic,
        )

    @staticmethod
    def _proposal_view(proposal: Proposal) -> ProposalView:
        brief = proposal.brief
        return ProposalView(
            id=proposal.id,
            status=proposal.status,
            brief=(
                DirectionBriefView(
                    candidate_id=brief.candidate_id,
                    recommendation=brief.recommendation,
                    rationale=brief.rationale,
                    confidence=brief.confidence,
                    risks=tuple(brief.risks),
                    candidate_goals=tuple(
                        CandidateGoalView(
                            title=goal.title,
                            metric=goal.metric,
                            target=goal.target,
                            rationale=goal.rationale,
                            score=goal.score,
                        )
                        for goal in brief.candidate_goals
                    ),
                    evidence_refs=tuple(brief.evidence_refs),
                )
                if brief is not None
                else None
            ),
            decision_statement=proposal.decision_statement,
            decision_rationale=proposal.decision_rationale,
            created_at=proposal.created_at,
            decided_by=proposal.decided_by,
            decided_at=proposal.decided_at,
            linked_decision_id=proposal.linked_decision_id,
            note=proposal.note,
        )


__all__ = [
    "CandidateGoalView",
    "DecisionView",
    "DirectionBriefView",
    "DirectionFacade",
    "GoalNode",
    "ProposalView",
    "StaffingRequirementView",
    "StrategyView",
    "TaskOutcomeView",
]
