"""AllocationFacade — the work-allocation board as podium DTOs (OBS P6).

Queued, claimed, and blocked are ledger truth read directly (wakes, live runs, the inspector's
stuck projection) — never reconstructed from event history."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from chorus.observability import LedgerInspector
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger


class QueuedWake(BaseModel):
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
    """Pure delegation: wakes.queued + runs.running + inspector.stuck; translation only."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def board(self) -> AllocationBoard:
        queued = [
            QueuedWake(
                task_id=(str(wake.payload["task_id"]) if "task_id" in wake.payload else None),
                employee_id=wake.employee_id,
                reason=wake.reason.value,
                coalesced=wake.coalesced_count,
            )
            for wake in self._ledger.wakes.queued()
        ]
        running = [
            RunningBeat(
                task_id=run.task_id,
                employee_id=run.employee_id,
                run_id=run.id,
                lease_expires_at=run.lease_expires_at,
            )
            for run in self._ledger.runs.running()
        ]
        blocked = [
            BlockedTask(
                task_id=view.id,
                intent_excerpt=view.intent[:200],
                assignee=view.assignee,
            )
            for view in LedgerInspector(self._ledger).stuck()
        ]
        return AllocationBoard(queued=queued, running=running, blocked=blocked)


__all__ = ["AllocationBoard", "AllocationFacade", "BlockedTask", "QueuedWake", "RunningBeat"]
