"""TDD: ChorusOutcomeFeed projects outcome.landed mechanically (Phase 0)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from chorus.events import Event, EventKind
from dream.contracts.strategy import LandedPhase, RecoveryHint
from podium.conductor.company._bridge import ChorusOutcomeFeed

_NOW = datetime.fromisoformat("2026-06-17T12:00:00+00:00")


class _Tasks:
    def __init__(self, task: object | None = None) -> None:
        self._task = task

    def get(self, task_id: str) -> object | None:
        if self._task is not None and getattr(self._task, "id", None) == task_id:
            return self._task
        return None


def _feed(task: object | None = None) -> ChorusOutcomeFeed:
    chorus = SimpleNamespace(
        _ledger=SimpleNamespace(tasks=_Tasks(task)),
        _event_bus=SimpleNamespace(subscribe=lambda _cb: (lambda: None), replay=lambda **_: iter(())),
    )
    return ChorusOutcomeFeed(chorus)  # type: ignore[arg-type]


def _task(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "id": "task-1",
        "goal_id": "goal-1",
        "parent_id": None,
        "team_id": None,
        "execution_mode": SimpleNamespace(value="delivery"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_outcome_landed_maps_phase_fields() -> None:
    feed = _feed(_task())
    event = Event(
        kind=EventKind.OUTCOME_LANDED,
        at=_NOW,
        task_id="task-1",
        payload={
            "phase": LandedPhase.NEEDS_REWORK.value,
            "summary": "DoD failed — rework",
            "diagnostic": "missing tests",
            "passed": False,
            "recovery_hint": RecoveryHint.REWORK.value,
        },
    )
    outcome = feed._translate(event)
    assert outcome is not None
    assert outcome.kind == "outcome.landed"
    assert outcome.phase == "needs_rework"
    assert outcome.recovery_hint == "rework"
    assert outcome.summary == "DoD failed — rework"
    assert outcome.passed is False
    assert outcome.detail == "missing tests"
    assert outcome.goal_id == "goal-1"


def test_run_evaluated_and_run_done_are_excluded() -> None:
    feed = _feed(_task())
    for kind in (EventKind.RUN_EVALUATED, EventKind.RUN_DONE):
        event = Event(kind=kind, at=_NOW, task_id="task-1", payload={"passed": True})
        assert feed._translate(event) is None


def test_task_status_still_forwarded() -> None:
    feed = _feed(_task())
    event = Event(
        kind=EventKind.TASK_STATUS,
        at=_NOW,
        task_id="task-1",
        payload={"status": "blocked"},
    )
    outcome = feed._translate(event)
    assert outcome is not None
    assert outcome.kind == "task.status"
    assert outcome.status == "blocked"


def test_recovery_escalated_still_forwarded() -> None:
    feed = _feed(_task())
    event = Event(
        kind=EventKind.RECOVERY_ESCALATED,
        at=_NOW,
        task_id="task-1",
        payload={"status": "escalated"},
    )
    outcome = feed._translate(event)
    assert outcome is not None
    assert outcome.kind == "recovery.escalated"
