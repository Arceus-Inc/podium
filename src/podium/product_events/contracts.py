"""Versioned contract for product events, independent of delivery or persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

ProductEventData: TypeAlias = dict[str, JsonValue]

ProductEventType: TypeAlias = Literal[
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
]

PRODUCT_EVENT_TYPES: tuple[ProductEventType, ...] = (
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


class ProductEventActor(BaseModel):
    """The actor that caused a product event without imposing a product taxonomy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    id: str = Field(min_length=1)

    @field_validator("type", "id")
    @classmethod
    def _require_nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actor identity values must not be blank")
        return value


class ProductEventSubject(BaseModel):
    """The versioned product resource changed by an event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    id: str = Field(min_length=1)
    version: int = Field(gt=0)

    @field_validator("type", "id")
    @classmethod
    def _require_nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subject identity values must not be blank")
        return value


class _ProductEventBody(BaseModel):
    """The validated data common to a product-event draft and durable envelope."""

    model_config = ConfigDict(allow_inf_nan=False, extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    type: ProductEventType
    occurred_at: datetime
    actor: ProductEventActor
    subject: ProductEventSubject
    causation_id: str | None = Field(default=None, min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)
    changed_fields: tuple[str, ...] = Field(min_length=1)
    data: ProductEventData = Field(default_factory=dict)

    @field_validator("causation_id", "correlation_id")
    @classmethod
    def _require_nonblank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("identity values must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("occurred_at must be UTC")
        return value

    @field_validator("changed_fields")
    @classmethod
    def _require_distinct_changed_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not field.strip() for field in value):
            raise ValueError("changed_fields must not contain blank values")
        if len(set(value)) != len(value):
            raise ValueError("changed_fields must be unique")
        return value


class ProductEventDraft(_ProductEventBody):
    """A validated normalized event before the database assigns identity and sequence."""

    def storage_payload(self) -> ProductEventData:
        """The JSONB representation that is independent of database-assigned envelope fields."""
        return {
            "schema_version": self.schema_version,
            "type": self.type,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": {"type": self.actor.type, "id": self.actor.id},
            "subject": {
                "type": self.subject.type,
                "id": self.subject.id,
                "version": self.subject.version,
            },
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "changed_fields": list(self.changed_fields),
            "data": self.data,
        }


class ProductEvent(_ProductEventBody):
    """The normalized v1 product event envelope."""

    event_id: str = Field(min_length=1)
    seq: int = Field(gt=0)
    company_id: str = Field(min_length=1)

    @field_validator("event_id", "company_id")
    @classmethod
    def _require_nonblank_durable_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity values must not be blank")
        return value
