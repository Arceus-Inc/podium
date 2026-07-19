"""AllocationFacade — the work-allocation board as podium DTOs (OBS P6).

Queued, running, and blocked are ledger truth read directly (wakes, live runs, task status +
task_dependency) — never reconstructed from event history. The board is the *whole* board: a
todo that cannot start yet (dependency-gated or awaiting dispatch) is waiting work, and every
`blocked`-status task is blocked work — both must be visible even when the liveness classifier
deems them healthy (F8: pending and blocked work must never read as zero)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from chorus.ledger import TaskStatus
from chorus.observability import LedgerInspector
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger
    from chorus.ledger._models import Task


class QueuedWake(BaseModel):
    """One item of waiting work: a queued/claimed wake, OR a todo task not yet dispatched
    (``reason`` then names why — ``dependency_blocked`` or ``awaiting_dispatch``)."""

    model_config = ConfigDict(frozen=True)

    task_id: str | None  # None for task-less nudges (e.g. a message wake)
    employee_id: str
    reason: str
    coalesced: int  # identical triggers folded into this wake


class RunningBeat(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    employee_id: str
    run_id: str
    lease_expires_at: datetime | None


class BlockedTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    intent_excerpt: str
    assignee: str | None


class AllocationBoard(BaseModel):
    model_config = ConfigDict(frozen=True)

    queued: list[QueuedWake]
    running: list[RunningBeat]
    blocked: list[BlockedTask]


class AllocationFacade:
    """Delegation to ledger truth: wakes.queued + waiting todos, runs.running, and blocked
    work (inspector.stuck plus every `blocked`-status task); translation only."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def board(self) -> AllocationBoard:
        ledger = self._ledger
        tasks = ledger.tasks.all()
        running_runs = ledger.runs.running()
        running_task_ids = {run.task_id for run in running_runs}

        queued_wakes = ledger.wakes.queued()
        waked_task_ids = {
            str(wake.payload["task_id"]) for wake in queued_wakes if "task_id" in wake.payload
        }
        queued = [
            QueuedWake(
                task_id=(str(wake.payload["task_id"]) if "task_id" in wake.payload else None),
                employee_id=wake.employee_id,
                reason=wake.reason.value,
                coalesced=wake.coalesced_count,
            )
            for wake in queued_wakes
        ]
        # A todo with no wake and no running beat is waiting work — either the scheduler has
        # not dispatched it yet, or it is gated on an unresolved dependency. The liveness
        # classifier calls it "resting"/"healthy", so it never surfaces via stuck(); the
        # operator must still see it. Ledger truth only (task.status + task_dependency).
        queued.extend(
            QueuedWake(
                task_id=task.id,
                employee_id=task.assignee_employee_id or "unassigned",
                reason=(
                    "dependency_blocked"
                    if ledger.dependencies.unresolved_blockers(task.id)
                    else "awaiting_dispatch"
                ),
                coalesced=1,
            )
            for task in tasks
            if task.status is TaskStatus.TODO
            and task.id not in waked_task_ids
            and task.id not in running_task_ids
        )
        running = [
            RunningBeat(
                task_id=run.task_id,
                employee_id=run.employee_id,
                run_id=run.id,
                lease_expires_at=run.lease_expires_at,
            )
            for run in running_runs
        ]
        blocked = {
            view.id: BlockedTask(
                task_id=view.id,
                intent_excerpt=view.intent[:200],
                assignee=view.assignee,
            )
            for view in LedgerInspector(ledger).stuck()
        }
        # A `blocked`-status task parked awaiting its subtree is "healthy" to the classifier
        # (healthy_blocker) and so absent from stuck() — but it is blocked work by definition.
        for task in tasks:
            if task.status is TaskStatus.BLOCKED and task.id not in blocked:
                blocked[task.id] = BlockedTask(
                    task_id=task.id,
                    intent_excerpt=task.intent[:200],
                    assignee=self._assignee_name(task),
                )
        return AllocationBoard(queued=queued, running=running, blocked=list(blocked.values()))

    def _assignee_name(self, task: Task) -> str | None:
        """Resolve the task's assignee to a display name (id fallback), mirroring the inspector."""
        if task.assignee_employee_id is None:
            return None
        employee = self._ledger.employees.get(task.assignee_employee_id)
        return employee.name if employee is not None else task.assignee_employee_id


__all__ = ["AllocationBoard", "AllocationFacade", "BlockedTask", "QueuedWake", "RunningBeat"]
