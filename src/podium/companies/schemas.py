"""Request/response shapes for the companies API (pydantic, validated at the HTTP boundary)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CompanyCreate(BaseModel):
    slug: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    slug: str
    name: str
    state: str
    ledger_backend: str
    created_at: datetime
    # `config` is intentionally NOT exposed: it may hold per-company signing-key derivation inputs
    # and other internal material. Never echo it over the API.
