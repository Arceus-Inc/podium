"""Exhaustive, mechanical Chorus-event mappings for the product timeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TypeAlias

from chorus.events import Event as ChorusEvent
from chorus.events import EventKind
from chorus.ledger import ExecutionMode, TaskPriority
from dream.contracts.strategy import LandedPhase, RecoveryHint

from podium.timeline.models import TimelineType
from podium.timeline.service_types import TimelineExclusion, TimelineItemDraft


class TimelinePayloadError(ValueError):
    """A raw Chorus event did not match its documented typed payload shape."""


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    """A consciously non-product-visible raw event kind."""

    reason: str


@dataclass(frozen=True, slots=True)
class TaskCreatedPayload:
    intent_excerpt: str
    priority: TaskPriority
    execution_mode: ExecutionMode

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TaskCreatedPayload:
        _require_keys(payload, required=frozenset({"intent_excerpt", "priority", "execution_mode"}))
        try:
            priority = TaskPriority(_string(payload, "priority"))
            execution_mode = ExecutionMode(_string(payload, "execution_mode"))
        except ValueError as exc:
            raise TimelinePayloadError("unknown task-created value") from exc
        return cls(
            intent_excerpt=_string(payload, "intent_excerpt"),
            priority=priority,
            execution_mode=execution_mode,
        )


@dataclass(frozen=True, slots=True)
class RunStalledPayload:
    reason: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> RunStalledPayload:
        _require_keys(payload, required=frozenset({"reason"}))
        return cls(reason=_string(payload, "reason"))


@dataclass(frozen=True, slots=True)
class BudgetHardStopPayload:
    gate: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> BudgetHardStopPayload:
        _require_keys(payload, required=frozenset({"gate"}))
        return cls(gate=_string(payload, "gate"))


@dataclass(frozen=True, slots=True)
class OutcomeLandedPayload:
    phase: LandedPhase
    summary: str
    passed: bool | None
    recovery_hint: RecoveryHint
    dod_status: str | None
    disposition: str | None
    diagnostic: str | None
    execution_mode: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> OutcomeLandedPayload:
        optional = frozenset({"dod_status", "disposition", "diagnostic", "execution_mode"})
        _require_keys(
            payload,
            required=frozenset({"phase", "summary", "passed", "recovery_hint"}),
            optional=optional,
        )
        try:
            phase = LandedPhase(_string(payload, "phase"))
            recovery_hint = RecoveryHint(_string(payload, "recovery_hint"))
        except ValueError as exc:
            raise TimelinePayloadError("unknown landed outcome value") from exc
        return cls(
            phase=phase,
            summary=_string(payload, "summary"),
            passed=_optional_bool(payload, "passed"),
            recovery_hint=recovery_hint,
            dod_status=_optional_string(payload, "dod_status"),
            disposition=_optional_string(payload, "disposition"),
            diagnostic=_optional_string(payload, "diagnostic"),
            execution_mode=_optional_string(payload, "execution_mode"),
        )


TimelineMapper: TypeAlias = Callable[[ChorusEvent, int], TimelineItemDraft | TimelineExclusion]
TimelineRule: TypeAlias = TimelineMapper | ExclusionRule


def map_timeline_event(event: ChorusEvent, *, source_event_seq: int) -> TimelineItemDraft | TimelineExclusion:
    """Validate and translate one raw Chorus event, or record its explicit exclusion."""
    rule = TIMELINE_RULES[event.kind]
    if isinstance(rule, ExclusionRule):
        return TimelineExclusion(source_event_seq=source_event_seq)
    return rule(event, source_event_seq)


def _task_created(event: ChorusEvent, source_event_seq: int) -> TimelineItemDraft:
    TaskCreatedPayload.from_payload(event.payload)
    return _task_item(
        event,
        source_event_seq=source_event_seq,
        event_type=TimelineType.WORK_TASK_CREATED,
        title="Task created",
    )


def _task_assigned(event: ChorusEvent, source_event_seq: int) -> TimelineItemDraft:
    _require_keys(event.payload, required=frozenset())
    return _task_item(
        event,
        source_event_seq=source_event_seq,
        event_type=TimelineType.WORK_TASK_DELEGATED,
        title="Task delegated",
    )


def _run_stalled(event: ChorusEvent, source_event_seq: int) -> TimelineItemDraft:
    RunStalledPayload.from_payload(event.payload)
    return _task_item(
        event,
        source_event_seq=source_event_seq,
        event_type=TimelineType.SYSTEM_STALLED,
        title="Run stalled",
        attention=True,
        attention_state="open",
    )


def _budget_hard_stop(event: ChorusEvent, source_event_seq: int) -> TimelineItemDraft:
    BudgetHardStopPayload.from_payload(event.payload)
    return _task_item(
        event,
        source_event_seq=source_event_seq,
        event_type=TimelineType.COST_HARD_STOP,
        title="Budget hard stop",
        attention=True,
        attention_state="open",
    )


def _outcome_landed(
    event: ChorusEvent, source_event_seq: int
) -> TimelineItemDraft | TimelineExclusion:
    payload = OutcomeLandedPayload.from_payload(event.payload)
    if payload.phase is LandedPhase.CANCELLED:
        return TimelineExclusion(source_event_seq=source_event_seq)
    event_type, title, attention = _landed_presentation(payload.phase)
    return _task_item(
        event,
        source_event_seq=source_event_seq,
        event_type=event_type,
        title=title,
        summary=payload.summary,
        attention=attention,
        attention_state="open" if attention else None,
    )


def _landed_presentation(phase: LandedPhase) -> tuple[TimelineType, str, bool]:
    presentations: Mapping[LandedPhase, tuple[TimelineType, str, bool]] = {
        LandedPhase.TERMINAL_PASS: (TimelineType.WORK_TASK_VERIFIED, "Task verified", False),
        LandedPhase.TERMINAL_FAIL: (TimelineType.WORK_TASK_REJECTED, "Task rejected", True),
        LandedPhase.NEEDS_REWORK: (TimelineType.WORK_TASK_BLOCKED, "Task needs rework", True),
        LandedPhase.DELEGATED: (TimelineType.WORK_TASK_DELEGATED, "Task delegated", False),
        LandedPhase.STRANDED: (TimelineType.SYSTEM_STALLED, "Task stranded", True),
    }
    return presentations[phase]


def _task_item(
    event: ChorusEvent,
    *,
    source_event_seq: int,
    event_type: TimelineType,
    title: str,
    summary: str | None = None,
    attention: bool = False,
    attention_state: str | None = None,
) -> TimelineItemDraft:
    task_id = event.task_id
    if task_id is None or task_id == "":
        raise TimelinePayloadError("timeline event requires a task_id")
    return TimelineItemDraft(
        source_event_seq=source_event_seq,
        category=event_type.category,
        event_type=event_type,
        subject_type="task",
        subject_id=task_id,
        title=title,
        summary=summary,
        actor_type="employee" if event.employee_id is not None else None,
        actor_id=event.employee_id,
        occurred_at=_utc(event.at),
        attention=attention,
        attention_state=attention_state,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise TimelinePayloadError("event timestamp must be timezone-aware UTC")
    return value


def _require_keys(
    payload: Mapping[str, object], *, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    received = frozenset(payload)
    if not required <= received or not received <= required | optional:
        raise TimelinePayloadError("payload keys do not match the event contract")


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TimelinePayloadError(f"{key} must be a string")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if type(value) is not str:
        raise TimelinePayloadError(f"{key} must be a string")
    return value


def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = payload[key]
    if value is None:
        return None
    if type(value) is not bool:
        raise TimelinePayloadError(f"{key} must be a boolean")
    return value


TIMELINE_RULES: Mapping[EventKind, TimelineRule] = MappingProxyType(
    {
        EventKind.TASK_CREATED: _task_created,
        EventKind.TASK_ASSIGNED: _task_assigned,
        EventKind.TASK_STATUS: ExclusionRule("status changes need a stable product transition"),
        EventKind.TASK_DEPENDENCY_RESOLVED: ExclusionRule("dependency state is reconciled"),
        EventKind.TASK_CHILDREN_DONE: ExclusionRule("parent integration state is reconciled"),
        EventKind.WAKE_ENQUEUED: ExclusionRule("scheduler wake is operational detail"),
        EventKind.WAKE_COALESCED: ExclusionRule("scheduler wake is operational detail"),
        EventKind.WAKE_CLAIMED: ExclusionRule("scheduler wake is operational detail"),
        EventKind.RUN_QUEUED: ExclusionRule("beat lifecycle is operational detail"),
        EventKind.RUN_STARTED: ExclusionRule("beat lifecycle is operational detail"),
        EventKind.RUN_TEXT: ExclusionRule("model prose is not product timeline data"),
        EventKind.RUN_TOOL_USE: ExclusionRule("tool telemetry is not product timeline data"),
        EventKind.RUN_TOOL_RESULT: ExclusionRule("tool telemetry is not product timeline data"),
        EventKind.RUN_TURN: ExclusionRule("beat lifecycle is operational detail"),
        EventKind.RUN_EVALUATED: ExclusionRule("outcome.landed is the authoritative outcome"),
        EventKind.RUN_DONE: ExclusionRule("outcome.landed is the authoritative outcome"),
        EventKind.SUBAGENT_SPAWNED: ExclusionRule("subagent telemetry is not product timeline data"),
        EventKind.SUBAGENT_COMPLETED: ExclusionRule("subagent telemetry is not product timeline data"),
        EventKind.MEMORY_RETRIEVED: ExclusionRule("memory telemetry is not product timeline data"),
        EventKind.LLM_CALL: ExclusionRule("model spend telemetry is separately projected"),
        EventKind.RUN_STALLED: _run_stalled,
        EventKind.ROUTINE_FIRED: ExclusionRule("routine payload has no stable product contract"),
        EventKind.ROUTINE_SUPPRESSED: ExclusionRule("scheduler detail is not product timeline data"),
        EventKind.RECOVERY_OPENED: ExclusionRule("recovery payload has no stable product contract"),
        EventKind.RECOVERY_ESCALATED: ExclusionRule("recovery payload has no stable product contract"),
        EventKind.RECOVERY_RESOLVED: ExclusionRule("recovery payload has no stable product contract"),
        EventKind.MONITOR_DUE: ExclusionRule("monitor detail is not product timeline data"),
        EventKind.BUDGET_SOFT_THRESHOLD: ExclusionRule("threshold payload has no stable product contract"),
        EventKind.BUDGET_HARD_STOP: _budget_hard_stop,
        EventKind.BUDGET_RESUMED: ExclusionRule("resume payload has no stable product contract"),
        EventKind.EMPLOYEE_HIRED: ExclusionRule("employee payload has no stable product contract"),
        EventKind.EMPLOYEE_PAUSED: ExclusionRule("employee payload has no stable product contract"),
        EventKind.EMPLOYEE_TERMINATED: ExclusionRule("employee payload has no stable product contract"),
        EventKind.APPROVAL_DECIDED: ExclusionRule("approval payload has no stable product contract"),
        EventKind.OUTCOME_LANDED: _outcome_landed,
    }
)


__all__ = [
    "TIMELINE_RULES",
    "ExclusionRule",
    "TimelinePayloadError",
    "map_timeline_event",
]
