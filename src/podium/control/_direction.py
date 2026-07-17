"""DirectionFacade — the alignment tree (chorus goals; horizon's local mirror) as podium DTOs.

Proposals/decisions are horizon workdir stores and land with the CP-2 doors (heartbeat side);
the read plane serves the ledger-backed tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger


class GoalNode(BaseModel):
    """One goal with its subtree — the product shape of the alignment tree."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    level: str  # engine GoalLevel value: company|team|personal|…
    status: str
    owner: str | None  # owning employee slug, if assigned
    children: list[GoalNode]


class DirectionFacade:
    """Pure delegation to the engine's goal table; recursion is shape, not business logic."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

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


__all__ = ["DirectionFacade", "GoalNode"]
