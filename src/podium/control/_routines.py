"""RoutinesFacade — the standing-heartbeat surface (chorus routines) as podium DTOs.

Hire provisions role-declared routines in the engine; this facade lists and governs them. Fire-now
routes through the engine's own cron firing (:func:`chorus.cron._fire.fire_routine`) — a firing
writes a task, never runs an agent — so an on-demand fire is byte-identical to a scheduled one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger
    from chorus.observability import LedgerInspector
    from chorus.observability import RoutineView as EngineRoutineView


class UnknownRoutineError(ValueError):
    """No such routine in this company."""


class RoutineFireConflict(ValueError):
    """The routine exists but could not fire (paused, no cron trigger, or coalesced)."""


class RoutineSummary(BaseModel):
    """One standing routine — definition, clock, and status, fused for the panel."""

    model_config = ConfigDict(frozen=True)

    id: str
    employee_id: str
    status: str  # active|paused|deleted
    schedule: str | None  # cron expression of the first cron trigger
    next_run_at: str | None
    last_fired_at: str | None
    intent: str


def _summary(view: EngineRoutineView) -> RoutineSummary:
    cron = next((t for t in view.triggers if t.cron_expression is not None), None)
    return RoutineSummary(
        id=view.id,
        employee_id=view.employee_id,
        status=view.status.value,
        schedule=cron.cron_expression if cron else None,
        next_run_at=cron.next_run_at.isoformat() if cron and cron.next_run_at else None,
        last_fired_at=cron.last_fired_at.isoformat() if cron and cron.last_fired_at else None,
        intent=view.intent_template,
    )


class RoutinesFacade:
    """Pure delegation to the engine's routine tables; translation, never business logic."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def _get_or_raise(self, routine_id: str) -> None:
        import uuid

        try:
            uuid.UUID(routine_id)  # engine routine ids are uuid text; anything else can't exist
        except ValueError:
            raise UnknownRoutineError(routine_id) from None
        if self._ledger.routines.get(routine_id) is None:
            raise UnknownRoutineError(routine_id)

    def _inspector(self) -> LedgerInspector:
        from chorus.observability import LedgerInspector

        return LedgerInspector(self._ledger)

    def list(self) -> list[RoutineSummary]:
        """Every routine of the company, any status."""
        return [_summary(view) for view in self._inspector().list_routines()]

    def pause(self, routine_id: str) -> RoutineSummary:
        """Stop a routine from firing (drops out of the tick's cron scan)."""
        return self._set_status(routine_id, paused=True)

    def resume(self, routine_id: str) -> RoutineSummary:
        """Resume a paused routine — its trigger starts selecting again."""
        return self._set_status(routine_id, paused=False)

    def fire(self, routine_id: str) -> str:
        """Fire the routine now through the engine's cron path; returns the spawned task id.

        Advances the trigger's edge past ``now``, so the next scheduled firing is unaffected.
        """
        from chorus.cron._fire import fire_routine

        self._get_or_raise(routine_id)
        for trigger in self._ledger.routine_triggers.by_routine(routine_id):
            if trigger.cron_expression is None:
                continue
            task_id = fire_routine(self._ledger, trigger, now=datetime.now(UTC))
            if task_id is not None:
                return task_id
        # Paused routine, no cron trigger, or a concurrency-coalesced suppression.
        raise RoutineFireConflict(f"routine {routine_id} did not fire")

    def _set_status(self, routine_id: str, *, paused: bool) -> RoutineSummary:
        from chorus.ledger import RoutineStatus

        self._get_or_raise(routine_id)
        status = RoutineStatus.PAUSED if paused else RoutineStatus.ACTIVE
        self._ledger.routines.set_status(routine_id, status)
        return _summary(self._inspector().routine(routine_id))


__all__ = ["RoutineFireConflict", "RoutineSummary", "RoutinesFacade", "UnknownRoutineError"]
