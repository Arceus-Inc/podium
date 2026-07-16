"""The seam between orchestration and execution. The Conductor claims and finalizes runs; a
`RunExecutor` does the actual work (drive a CompanyGraph). Injecting it keeps orchestration testable
with a fake, and confines all chorus/model coupling to the real implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from podium.runs import RunStatus

# Called between units of work; True means the run has been asked to cancel.
CancelCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ExecutionResult:
    """A terminal outcome the conductor writes back with `finalize_run`."""

    status: RunStatus  # SUCCEEDED | FAILED | CANCELED | TIMED_OUT
    error: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)


class RunExecutor(Protocol):
    async def execute(
        self, *, workspace_id: str, company_id: str, directive: str, is_canceled: CancelCheck
    ) -> ExecutionResult: ...
