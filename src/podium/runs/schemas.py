"""Request/response shapes for the runs API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RunCreate(BaseModel):
    directive: str
    idempotency_key: str


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    company_id: str
    directive: str
    idempotency_key: str
    status: str
    error: str | None
    counts: dict[str, Any]
    created_at: datetime
    updated_at: datetime
