"""Request/response shapes for the runs API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from podium.runs.models import RunStatus

IDEMPOTENCY_KEY_MAX_LENGTH = 128


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directive: str
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
        json_schema_extra={"deprecated": True},
    )
    # One run resource; execution_mode discriminates (M4 §3.3). Delegation params are
    # meaningful only in delegation mode and required there — fail at the door. Formation
    # routes to the CEO, whose proposal waits at the /plans human boundary (CO1).
    execution_mode: Literal["delivery", "delegation", "formation"] = "delivery"
    assignee: str | None = (
        None  # delivery only: direct the run at one employee (default worker otherwise)
    )
    lead: str | None = None
    goal_id: str | None = None
    max_team_size: int | None = None
    spend_limit_cents: int | None = None

    @model_validator(mode="after")
    def _delegation_requires_lead_and_goal(self) -> RunCreate:
        if self.execution_mode == "delegation" and (self.lead is None or self.goal_id is None):
            raise ValueError("delegation runs require both lead and goal_id")
        return self

    def params(self) -> RunParams:
        """The durable, typed run parameters stored at the persistence boundary."""
        return RunParams(
            execution_mode=self.execution_mode,
            assignee=self.assignee,
            lead=self.lead,
            goal_id=self.goal_id,
            max_team_size=self.max_team_size,
            spend_limit_cents=self.spend_limit_cents,
        )


class RunParams(BaseModel):
    """Public, durable execution parameters. Unknown engine fields must never cross this boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: Literal["delivery", "delegation", "formation"] | None = None
    assignee: str | None = None
    lead: str | None = None
    goal_id: str | None = None
    max_team_size: int | None = None
    spend_limit_cents: int | None = None


class RunCounts(BaseModel):
    """Public lifecycle totals, populated when the run's event spine is rolled up."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: int | None = None
    llm_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int | None = None
    tool_errors: int | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    company_id: uuid.UUID
    directive: str
    idempotency_key: str
    status: RunStatus
    error: str | None
    counts: RunCounts
    params: RunParams
    created_at: datetime
    updated_at: datetime


class RunPageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    next_cursor: str | None
    has_more: bool


class RunPageLinks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    self: str
    next: str | None


class RunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data: list[RunOut]
    meta: RunPageMeta
    links: RunPageLinks
