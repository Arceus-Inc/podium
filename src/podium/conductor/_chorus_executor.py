"""The real executor: drive a live `CompanyGraph` (company.build) with the chorus heartbeat.

This is the only place podium couples to chorus/the model. It holds one graph per company (lazily
built), submits the directive as a chorus task, and pulses `tick()`+`drain()` until the task reaches
a terminal status — mapping chorus's outcome onto a podium RunStatus. Cancel is cooperative: checked
between pulses. All model/LLM work happens here, against the company's own ledger — never the
product DB — so the conductor holds no product-DB connection while a run executes.
"""

from __future__ import annotations

from pathlib import Path

from chorus.ledger._models import TaskStatus

from company import CompanyConfig, CompanyGraph, build
from podium.conductor._executor import CancelCheck, ExecutionResult
from podium.runs import RunStatus

_TERMINAL: dict[TaskStatus, RunStatus] = {
    TaskStatus.DONE: RunStatus.SUCCEEDED,
    TaskStatus.CANCELLED: RunStatus.CANCELED,
    TaskStatus.REJECTED: RunStatus.FAILED,
}


class CompanyGraphHost:
    """One live CompanyGraph per company, built on first use from the model config + a workdir."""

    def __init__(self, *, api_key: str, base_url: str, deployment: str, workdir: Path) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._deployment = deployment
        self._workdir = workdir
        self._graphs: dict[str, CompanyGraph] = {}
        self._assignee: dict[str, str] = {}

    def get(self, company_id: str) -> tuple[CompanyGraph, str]:
        """Return (graph, assignee_name), building and staffing the company on first request."""
        if company_id not in self._graphs:
            graph = build(
                CompanyConfig(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    deployment=self._deployment,
                    workdir=self._workdir / company_id,
                    company_id=company_id,
                )
            )
            worker = graph.org.hire(name="Ace", role="backend_engineer")
            self._graphs[company_id] = graph
            self._assignee[company_id] = worker.name
        return self._graphs[company_id], self._assignee[company_id]


class ChorusRunExecutor:
    def __init__(self, host: CompanyGraphHost, *, max_ticks: int = 60) -> None:
        self._host = host
        self._max_ticks = max_ticks

    async def execute(
        self, *, workspace_id: str, company_id: str, directive: str, is_canceled: CancelCheck
    ) -> ExecutionResult:
        graph, assignee = self._host.get(company_id)
        task = graph.org.submit(directive, assignee=assignee)
        for _ in range(self._max_ticks):
            if await is_canceled():
                return ExecutionResult(status=RunStatus.CANCELED)
            await graph.org.tick()
            await graph.org.drain()
            current = graph.org._ledger.tasks.get(task.id)  # composition root reaches the ledger
            if current is not None and current.status in _TERMINAL:
                mapped = _TERMINAL[current.status]
                error = "task rejected" if mapped is RunStatus.FAILED else None
                return ExecutionResult(status=mapped, error=error)
        return ExecutionResult(status=RunStatus.TIMED_OUT, error="exceeded tick budget")
