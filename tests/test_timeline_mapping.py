"""Contract tests for exhaustive mechanical Chorus-to-timeline mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from chorus.events import Event, EventKind
from dream.contracts.strategy import LandedPhase, RecoveryHint

from podium.timeline.mapping import (
    TIMELINE_RULES,
    ExclusionRule,
    TimelinePayloadError,
    map_timeline_event,
)
from podium.timeline.models import TimelineType
from podium.timeline.service_types import TimelineExclusion, TimelineItemDraft

EVENT_KIND_SNAPSHOT = (
    "task.created",
    "task.assigned",
    "task.status",
    "task.dependency_resolved",
    "task.children_done",
    "wake.enqueued",
    "wake.coalesced",
    "wake.claimed",
    "run.queued",
    "run.started",
    "run.text",
    "run.tool_use",
    "run.tool_result",
    "run.turn",
    "run.evaluated",
    "run.done",
    "run.subagent_spawned",
    "run.subagent_completed",
    "memory.retrieved",
    "llm.call",
    "run.stalled",
    "routine.fired",
    "routine.suppressed",
    "recovery.opened",
    "recovery.escalated",
    "recovery.resolved",
    "monitor.due",
    "budget.soft_threshold",
    "budget.hard_stop",
    "budget.resumed",
    "employee.hired",
    "employee.paused",
    "employee.terminated",
    "approval.decided",
    "outcome.landed",
)


def _event(kind: EventKind, *, payload: dict[str, object], task_id: str | None = "task-1") -> Event:
    return Event(
        kind=kind,
        at=datetime.now(UTC),
        task_id=task_id,
        employee_id="employee-1",
        payload=payload,
    )


def test_timeline_rules_snapshot_and_exhaustiveness_match_chorus() -> None:
    assert tuple(kind.value for kind in EventKind) == EVENT_KIND_SNAPSHOT
    assert set(TIMELINE_RULES) == set(EventKind)
    assert len(TIMELINE_RULES) == len(EventKind)
    assert all(callable(rule) or isinstance(rule, ExclusionRule) for rule in TIMELINE_RULES.values())


def test_task_created_maps_only_its_typed_payload() -> None:
    mapped = map_timeline_event(
        _event(
            EventKind.TASK_CREATED,
            payload={"intent_excerpt": "Ship it", "priority": "high", "execution_mode": "delivery"},
        ),
        source_event_seq=7,
    )

    assert isinstance(mapped, TimelineItemDraft)
    assert mapped.source_event_seq == 7
    assert mapped.category == "work"
    assert mapped.event_type is TimelineType.WORK_TASK_CREATED
    assert mapped.subject_type == "task"
    assert mapped.subject_id == "task-1"
    assert mapped.summary is None


def test_task_created_rejects_an_untyped_payload_shape() -> None:
    with pytest.raises(TimelinePayloadError):
        map_timeline_event(
            _event(EventKind.TASK_CREATED, payload={"intent_excerpt": "Ship it"}),
            source_event_seq=7,
        )


def test_explicitly_excluded_event_returns_an_exclusion() -> None:
    mapped = map_timeline_event(
        _event(EventKind.RUN_TEXT, payload={"text": "untrusted model prose"}), source_event_seq=8
    )

    assert mapped == TimelineExclusion(source_event_seq=8)


def test_initial_task_assignment_is_not_a_delegation() -> None:
    mapped = map_timeline_event(
        _event(EventKind.TASK_ASSIGNED, payload={}), source_event_seq=9
    )

    assert mapped == TimelineExclusion(source_event_seq=9)


@pytest.mark.parametrize(
    ("phase", "recovery_hint", "passed", "event_type", "attention"),
    [
        (
            LandedPhase.TERMINAL_PASS,
            RecoveryHint.NONE,
            True,
            TimelineType.WORK_TASK_VERIFIED,
            False,
        ),
        (
            LandedPhase.TERMINAL_FAIL,
            RecoveryHint.NONE,
            False,
            TimelineType.WORK_TASK_REJECTED,
            True,
        ),
        (
            LandedPhase.NEEDS_REWORK,
            RecoveryHint.REWORK,
            False,
            TimelineType.WORK_TASK_BLOCKED,
            True,
        ),
        (
            LandedPhase.DELEGATED,
            RecoveryHint.WAIT_FOR_CHILDREN,
            None,
            TimelineType.WORK_TASK_DELEGATED,
            False,
        ),
        (LandedPhase.STRANDED, RecoveryHint.ESCALATE, None, TimelineType.SYSTEM_STALLED, True),
    ],
)
def test_landed_outcome_maps_its_typed_phase_mechanically(
    phase: LandedPhase,
    recovery_hint: RecoveryHint,
    passed: bool | None,
    event_type: TimelineType,
    attention: bool,
) -> None:
    mapped = map_timeline_event(
        _event(
            EventKind.OUTCOME_LANDED,
            payload={
                "phase": phase.value,
                "summary": "Typed outcome summary",
                "passed": passed,
                "recovery_hint": recovery_hint.value,
            },
        ),
        source_event_seq=9,
    )

    assert isinstance(mapped, TimelineItemDraft)
    assert mapped.event_type is event_type
    assert mapped.attention is attention
    assert mapped.summary == "Typed outcome summary"


def test_cancelled_landed_outcome_is_explicitly_excluded() -> None:
    mapped = map_timeline_event(
        _event(
            EventKind.OUTCOME_LANDED,
            payload={
                "phase": LandedPhase.CANCELLED.value,
                "summary": "Beat cancelled",
                "passed": None,
                "recovery_hint": RecoveryHint.NONE.value,
            },
        ),
        source_event_seq=10,
    )

    assert mapped == TimelineExclusion(source_event_seq=10)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "phase": LandedPhase.TERMINAL_PASS.value,
            "summary": "DoD passed",
            "passed": False,
            "recovery_hint": RecoveryHint.NONE.value,
        },
        {
            "phase": LandedPhase.TERMINAL_PASS.value,
            "summary": "DoD passed",
            "passed": True,
            "recovery_hint": RecoveryHint.REWORK.value,
        },
        {
            "phase": LandedPhase.DELEGATED.value,
            "summary": "Delegated to subtree",
            "passed": None,
            "recovery_hint": RecoveryHint.WAIT_FOR_CHILDREN.value,
            "execution_mode": "unknown",
        },
    ],
)
def test_landed_outcome_rejects_contradictory_or_unknown_typed_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(TimelinePayloadError):
        map_timeline_event(_event(EventKind.OUTCOME_LANDED, payload=payload), source_event_seq=11)


def test_budget_hard_stop_requires_its_typed_gate() -> None:
    mapped = map_timeline_event(
        _event(EventKind.BUDGET_HARD_STOP, payload={"gate": "dispatch"}), source_event_seq=11
    )

    assert isinstance(mapped, TimelineItemDraft)
    assert mapped.event_type is TimelineType.COST_HARD_STOP
    assert mapped.attention is True


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (EventKind.RUN_STALLED, {"reason": "unknown"}),
        (EventKind.BUDGET_HARD_STOP, {"gate": "unknown"}),
    ],
)
def test_closed_scheduler_payload_values_are_validated(
    kind: EventKind, payload: dict[str, object]
) -> None:
    with pytest.raises(TimelinePayloadError):
        map_timeline_event(_event(kind, payload=payload), source_event_seq=12)
