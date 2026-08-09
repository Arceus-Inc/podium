"""Typed HTTP contracts for stream-ticket minting."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StreamTicketView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str
    expires_at: datetime


class StreamTicketCreateEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: StreamTicketView
