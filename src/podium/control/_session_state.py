"""Translation of Chorus agent-session handles into the Podium run read model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chorus.errors import OrgInvariantViolation

from podium.runs.session_state import (
    AgentSessionCost,
    AgentSessionStatus,
    AgentSessionView,
    SessionRecoveryReason,
)

if TYPE_CHECKING:
    from chorus.ledger import Ledger


def _recovery_reason(last_error: str | None) -> SessionRecoveryReason | None:
    if last_error is None:
        return None
    try:
        return SessionRecoveryReason(last_error)
    except ValueError as exc:
        raise OrgInvariantViolation("agent session has an unknown recovery reason") from exc


class SessionStateFacade:
    """Read the latest handle for a task, including sealed and aborted history."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def latest_for_task(self, task_id: str) -> AgentSessionView | None:
        session = self._ledger.agent_sessions.latest_for_task(task_id)
        if session is None:
            return None
        return AgentSessionView(
            id=session.id,
            task_id=session.task_id,
            employee_id=session.employee_id,
            run_id=session.run_id,
            provider_session_key=session.dream_session_key,
            model=session.model,
            working_dir=session.working_dir,
            recovery_reason=_recovery_reason(session.last_error),
            status=AgentSessionStatus(session.status),
            cost=AgentSessionCost(
                input_tokens=session.cost.input_tokens,
                output_tokens=session.cost.output_tokens,
                cache_read_tokens=session.cost.cache_read_tokens,
                cache_write_tokens=session.cost.cache_write_tokens,
                cost_usd=session.cost.cost_usd,
            ),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
