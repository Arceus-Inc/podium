"""The product-event envelope is a small, closed, transport-independent contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from podium.product_events import (
    ProductEvent,
    ProductEventActor,
    ProductEventData,
    ProductEventSubject,
    ProductEventType,
)

_EVENT_TYPES: tuple[ProductEventType, ...] = (
    "home.updated",
    "timeline.item.created",
    "timeline.item.updated",
    "attention.requested",
    "attention.resolved",
    "conversation.message.created",
    "conversation.message.updated",
    "decision.updated",
    "goal.updated",
    "task.updated",
    "task.activity.created",
    "deliverable.updated",
    "employee.updated",
    "team.updated",
    "learning.updated",
    "projection.rebuild_required",
)


def _envelope(**changes: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "schema_version": 1,
        "event_id": "event-001",
        "seq": 7,
        "type": "timeline.item.updated",
        "company_id": "company-001",
        "occurred_at": "2026-08-09T12:34:56Z",
        "actor": {"type": "service", "id": "podium-api"},
        "subject": {"type": "timeline_item", "id": "item-001", "version": 3},
        "causation_id": "command-001",
        "correlation_id": "run-001",
        "changed_fields": ["title", "attention"],
        "data": {"title": "Review architecture", "attention": True, "tags": ["api", "v1"]},
    }
    envelope.update(changes)
    return envelope


def test_golden_v1_envelope() -> None:
    event = ProductEvent.model_validate(_envelope())

    assert event == ProductEvent(
        schema_version=1,
        event_id="event-001",
        seq=7,
        type="timeline.item.updated",
        company_id="company-001",
        occurred_at=datetime(2026, 8, 9, 12, 34, 56, tzinfo=UTC),
        actor=ProductEventActor(type="service", id="podium-api"),
        subject=ProductEventSubject(type="timeline_item", id="item-001", version=3),
        causation_id="command-001",
        correlation_id="run-001",
        changed_fields=("title", "attention"),
        data={"title": "Review architecture", "attention": True, "tags": ["api", "v1"]},
    )


def test_every_closed_event_type_is_accepted() -> None:
    assert tuple(ProductEvent.model_validate(_envelope(type=event_type)).type for event_type in _EVENT_TYPES) == _EVENT_TYPES


def test_public_contract_type_exports_are_usable() -> None:
    payload: ProductEventData = {"nested": [1, True, None]}
    event_type: ProductEventType = "home.updated"

    assert ProductEvent.model_validate(_envelope(type=event_type, data=payload)).data == payload


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="literal"):
        ProductEvent.model_validate(_envelope(type="run.created"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("seq", 0),
        ("event_id", ""),
        ("company_id", ""),
        ("causation_id", ""),
        ("correlation_id", ""),
        ("actor", {"type": "service", "id": ""}),
        ("changed_fields", []),
        ("changed_fields", ["title", "title"]),
        ("changed_fields", [""]),
    ],
)
def test_envelope_rejects_invalid_required_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ProductEvent.model_validate(_envelope(**{field: value}))


@pytest.mark.parametrize(
    "occurred_at",
    [
        datetime(2026, 8, 9, 12, 34, 56),
        datetime(2026, 8, 9, 12, 34, 56, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_occurred_at_must_be_utc(occurred_at: datetime) -> None:
    with pytest.raises(ValidationError, match="UTC"):
        ProductEvent.model_validate(_envelope(occurred_at=occurred_at))


@pytest.mark.parametrize(
    "subject",
    [
        {"type": "task", "id": "", "version": 1},
        {"type": "task", "id": "task-001", "version": 0},
    ],
)
def test_subject_requires_nonempty_id_and_positive_version(subject: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProductEvent.model_validate(_envelope(subject=subject))


def test_json_data_rejects_non_json_values() -> None:
    with pytest.raises(ValidationError):
        ProductEvent.model_validate(_envelope(data={"bad": object()}))


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_json_data_rejects_nested_non_finite_floats(non_finite: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        ProductEvent.model_validate(_envelope(data={"nested": [{"bad": non_finite}]}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "   "),
        ("company_id", "\t"),
        ("causation_id", "\n"),
        ("correlation_id", "   "),
        ("actor", {"type": "   ", "id": "actor-001"}),
        ("actor", {"type": "service", "id": "   "}),
        ("subject", {"type": "   ", "id": "item-001", "version": 1}),
        ("subject", {"type": "item", "id": "   ", "version": 1}),
        ("changed_fields", ["title", "   "]),
    ],
)
def test_identity_and_changed_field_values_reject_whitespace_only(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match="blank"):
        ProductEvent.model_validate(_envelope(**{field: value}))


def test_identity_values_are_not_silently_trimmed() -> None:
    event = ProductEvent.model_validate(
        _envelope(
            event_id=" event-001 ",
            actor={"type": " service ", "id": " actor-001 "},
            changed_fields=[" title "],
        )
    )

    assert event.event_id == " event-001 "
    assert event.actor.type == " service "
    assert event.actor.id == " actor-001 "
    assert event.changed_fields == (" title ",)


def test_deterministic_json_round_trip() -> None:
    event = ProductEvent.model_validate(_envelope())
    encoded = event.model_dump_json()
    decoded = ProductEvent.model_validate_json(encoded)

    assert decoded == event
    assert decoded.model_dump_json() == encoded
