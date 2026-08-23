"""Read-only product views of Chorus agent-session recovery state."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SessionRecoveryReason(StrEnum):
    """The closed recovery vocabulary persisted by Chorus."""

    MISSING = "missing"
    CORRUPT = "corrupt"
    SCHEMA_MISMATCH = "schema_mismatch"
    WORKING_DIR_MISMATCH = "working_dir_mismatch"


class AgentSessionStatus(StrEnum):
    """Lifecycle states of a Chorus agent-session handle."""

    OPEN = "open"
    SEALED = "sealed"
    ABORTED = "aborted"


class AgentSessionCost(BaseModel):
    """Cumulative provider-neutral cost totals for one session handle."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float


class AgentSessionView(BaseModel):
    """The stable Podium view of Chorus's authoritative session handle."""

    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    employee_id: str
    run_id: str | None
    provider_session_key: str
    model: str
    working_dir: str | None
    recovery_reason: SessionRecoveryReason | None
    status: AgentSessionStatus
    cost: AgentSessionCost
    created_at: datetime | None
    updated_at: datetime | None


class RunSessionState(BaseModel):
    """A visible product run and its latest authoritative agent-session handle."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    engine_task_id: str | None
    session: AgentSessionView | None
