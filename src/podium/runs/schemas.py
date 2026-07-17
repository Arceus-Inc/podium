"""Request/response shapes for the runs API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class RunCreate(BaseModel):
    directive: str
    idempotency_key: str
    # One run resource; execution_mode discriminates (M4 §3.3). Delegation params are
    # meaningful only in delegation mode and required there — fail at the door.
    execution_mode: Literal["delivery", "delegation"] = "delivery"
    lead: str | None = None
    goal_id: str | None = None
    max_team_size: int | None = None
    spend_limit_cents: int | None = None

    @model_validator(mode="after")
    def _delegation_requires_lead_and_goal(self) -> RunCreate:
        if self.execution_mode == "delegation" and (self.lead is None or self.goal_id is None):
            raise ValueError("delegation runs require both lead and goal_id")
        return self

    def params(self) -> dict[str, object]:
        """The durable, non-default subset stored on the run row."""
        data: dict[str, object] = {"execution_mode": self.execution_mode}
        for key in ("lead", "goal_id", "max_team_size", "spend_limit_cents"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    company_id: uuid.UUID
    directive: str
    idempotency_key: str
    status: str
    error: str | None
    counts: dict[str, Any]
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime
