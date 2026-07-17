"""Response shapes for the events API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    run_id: uuid.UUID | None
    company_id: uuid.UUID
    type: str
    employee_id: str | None  # chorus-minted id (engine context) — text until the M5.2 engine port
    payload: dict[str, Any]
    created_at: datetime


class EventPageMeta(BaseModel):
    next_after: int | None  # cursor to pass as ?after= for the next page
    has_next: bool


class EventPage(BaseModel):
    data: list[EventOut]
    meta: EventPageMeta
