"""Request/response shapes for the runs API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RunCreate(BaseModel):
    directive: str
    idempotency_key: str


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
    created_at: datetime
    updated_at: datetime
