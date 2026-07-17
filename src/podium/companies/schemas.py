"""Request/response shapes for the companies API (pydantic, validated at the HTTP boundary)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CompanyCreate(BaseModel):
    slug: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    owner_user_id: uuid.UUID | None
    slug: str
    name: str
    state: str
    created_at: datetime
    # `config` is intentionally NOT exposed: it may hold per-company signing-key derivation inputs
    # and other internal material. Never echo it over the API.
