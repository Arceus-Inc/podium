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
