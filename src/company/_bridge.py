"""Adapt chorus's concrete classes to horizon's ``dream.contracts`` ports (the strategy seam).

Maintained home of the bridge adapters (promoted from horizon/examples/chorus_bridge.py — the
examples copy is the legacy one; new consumers import ``company``).

This is the ONE module allowed to import both chorus (concretes) and horizon's ports. horizon itself
never imports chorus; a consumer wires the two together here. Three adapters:

  * :class:`ChorusGoalStore`   -> ``GoalStore``   (the OKR tree, backed by chorus's ``goal`` table)
  * :class:`ChorusIntakePort`  -> ``IntakePort``  (the single intake door ``Chorus.submit``, deduped)
  * :class:`ChorusOutcomeFeed` -> ``OutcomeFeed`` (chorus's ``EventBus``, translated to ``OutcomeEvent``)

Translations: ``str`` priority <-> ``TaskPriority``; chorus ``Goal`` <-> ``GoalNode``; chorus ``Event``
-> ``OutcomeEvent``. horizon's strategy-only fields (score/health/metric/target) are NOT written to
chorus's lean goal row — they live in horizon's own ``StrategyStore`` and are overlaid by the caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from hashlib import sha256

from chorus.events import Event, EventKind
from chorus.facade import Chorus
from chorus.ledger._models import Goal, GoalLevel, OriginKind, TaskPriority
from dream.contracts import GoalNode, OutcomeEvent, Priority

_TO_PRIORITY: dict[str, TaskPriority] = {
    "critical": TaskPriority.CRITICAL,
    "high": TaskPriority.HIGH,
    "medium": TaskPriority.MEDIUM,
    "low": TaskPriority.LOW,
}

# The chorus event kinds that carry an execution outcome horizon reacts to (the feedback loop).
_OUTCOME_KINDS: dict[EventKind, str] = {
    EventKind.RUN_DONE: EventKind.RUN_DONE.value,
    EventKind.RUN_EVALUATED: EventKind.RUN_EVALUATED.value,
    EventKind.TASK_STATUS: EventKind.TASK_STATUS.value,
    EventKind.RECOVERY_ESCALATED: EventKind.RECOVERY_ESCALATED.value,
}


def _to_priority(priority: Priority) -> TaskPriority:
    return _TO_PRIORITY.get(priority.lower(), TaskPriority.MEDIUM)


def _to_level(level: str) -> GoalLevel:
    try:
        return GoalLevel(level)
    except ValueError:
        return GoalLevel.GOAL


def _to_node(goal: Goal) -> GoalNode:
    # chorus holds only the skeleton; the strategy-only fields (score/health/metric/target) default
    # here and are overlaid from horizon's own StrategyStore by the caller.
    return GoalNode(
        id=goal.id,
        title=goal.title,
        level=goal.level.value,
        status=goal.status,
        parent_id=goal.parent_id,
        owner=goal.owner_employee_id,
    )


class ChorusGoalStore:
    """Adapt chorus's ``GoalRepo`` to the ``GoalStore`` port (the OKR tree horizon authors)."""

    def __init__(self, chorus: Chorus) -> None:
        self._goals = chorus._ledger.goals  # composition root: reach the concrete repo

    def upsert(self, node: GoalNode) -> str:
        goal = Goal(
            id=node.id,
            title=node.title,
            level=_to_level(node.level),
            status=node.status,
            parent_id=node.parent_id,
            owner_employee_id=node.owner,
        )
        if self._goals.get(node.id) is None:
            self._goals.create(goal)
        else:
            self._goals.update(goal)
        return node.id

    def get(self, goal_id: str) -> GoalNode | None:
        goal = self._goals.get(goal_id)
        return _to_node(goal) if goal is not None else None

    def children(self, parent_id: str | None) -> list[GoalNode]:
        return [_to_node(goal) for goal in self._goals.children(parent_id)]


class ChorusIntakePort:
    """Adapt ``Chorus.submit`` to the ``IntakePort`` port, with idempotent horizon-intake dedup."""

    def __init__(self, chorus: Chorus) -> None:
        self._chorus = chorus

    def submit(
        self,
        intent: str,
        *,
        assignee: str | None = None,
        priority: Priority = "medium",
        depends_on: Sequence[str] = (),
        goal_id: str | None = None,
        origin_fingerprint: str | None = None,
    ) -> str:
        fingerprint = origin_fingerprint or "default"
        existing = self._chorus._ledger.tasks.find_by_origin(OriginKind.HORIZON_INTAKE, fingerprint)
        if existing is not None:
            return existing.id  # idempotent: re-deriving the same opportunity is a no-op
        task = self._chorus.submit(
            intent,
            assignee=assignee,
            priority=_to_priority(priority),
            depends_on=depends_on,
            goal_id=goal_id,
            origin_kind=OriginKind.HORIZON_INTAKE,
            origin_fingerprint=fingerprint,
        )
        return task.id

    def set_priority(self, task_id: str, priority: Priority) -> None:
        # chorus has no reprioritize facade yet; the bridge writes ``task.priority`` directly (the
        # proper seam lands with the M2 Prioritiser). A pure data write — never a scheduler call.
        conn = self._chorus._ledger._conn
        conn.execute(
            "UPDATE task SET priority = ?, updated_at = ? WHERE id = ?",
            (_to_priority(priority).value, datetime.now(UTC).isoformat(), task_id),
        )
        conn.commit()


class ChorusOutcomeFeed:
    """Adapt chorus's ``EventBus`` to the ``OutcomeFeed`` port (``Event`` -> ``OutcomeEvent``)."""

    def __init__(self, chorus: Chorus) -> None:
        self._chorus = chorus
        self._bus = chorus._event_bus

    def subscribe(self, callback: Callable[[OutcomeEvent], None]) -> Callable[[], None]:
        def relay(event: Event) -> None:
            outcome = self._translate(event)
            if outcome is not None:
                callback(outcome)

        return self._bus.subscribe(relay)

    def replay(self, *, after: str | None = None) -> Iterator[OutcomeEvent]:
        for event in self._bus.replay(after=after):
            outcome = self._translate(event)
            if outcome is not None:
                yield outcome

    def _translate(self, event: Event) -> OutcomeEvent | None:
        kind = _OUTCOME_KINDS.get(event.kind)
        if kind is None:
            return None  # not an outcome horizon reacts to — drop it
        goal_id: str | None = None
        parent_task_id: str | None = None
        root_task_id: str | None = None
        team_id: str | None = None
        execution_mode: str | None = None
        if event.task_id is not None:
            task = self._chorus._ledger.tasks.get(event.task_id)
            if task is not None:
                goal_id = task.goal_id
                parent_task_id = task.parent_id
                team_id = task.team_id
                execution_mode = task.execution_mode.value
                root = task
                while root.parent_id is not None:
                    parent = self._chorus._ledger.tasks.get(root.parent_id)
                    if parent is None:
                        break
                    root = parent
                root_task_id = root.id
        payload = event.payload
        status = payload.get("status")
        passed = payload.get("passed")
        if passed is None:
            # a real chorus RUN_EVALUATED carries dream's evaluator verdict as ``outcome`` (pass|fail|
            # needs-changes), not a boolean — map it; a non-terminal "needs-changes" stays None (dropped).
            outcome = payload.get("outcome")
            if outcome == "pass":
                passed = True
            elif outcome == "fail":
                passed = False
        event_identity = json.dumps(
            {
                "kind": event.kind.value,
                "at": event.at.isoformat(),
                "trace_id": event.trace_id,
                "task_id": event.task_id,
                "employee_id": event.employee_id,
                "run_id": event.run_id,
                "payload": dict(event.payload),
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return OutcomeEvent(
            kind=kind,
            task_id=event.task_id,
            goal_id=goal_id,
            status=str(status) if status is not None else None,
            passed=bool(passed) if passed is not None else None,
            detail=json.dumps(dict(payload), sort_keys=True, default=str),
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            team_id=team_id,
            execution_mode=execution_mode,
            is_root_outcome=event.task_id is not None and event.task_id == root_task_id,
            event_id=sha256(event_identity.encode("utf-8")).hexdigest(),
            task_revision=int(event.at.timestamp() * 1_000_000),
        )
