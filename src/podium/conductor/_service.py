"""The Conductor: drains queued runs and reclaims crashed ones. Discovery is cross-tenant (a
privileged control-plane session); every mutation is RLS-scoped through `tenant_session`.

A run's connection is held only for the short CAS transitions — the long execution runs against the
chorus ledger via the executor, so a slow run never ties up a product-DB connection.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.conductor._executor import ExecutionResult, RunExecutor
from podium.db import tenant_session
from podium.runs import (
    RunRef,
    RunStatus,
    claim_queued_run,
    expired_lease_refs,
    finalize_run,
    get_run,
    queued_run_refs,
    reclaim_run,
)

_log = structlog.get_logger("podium.conductor")


class Conductor:
    def __init__(
        self,
        *,
        control_sessionmaker: async_sessionmaker[AsyncSession],
        app_sessionmaker: async_sessionmaker[AsyncSession],
        executor: RunExecutor,
        worker_id: str,
        lease_seconds: int = 300,
        batch_size: int = 10,
        poll_interval: float = 5.0,
    ) -> None:
        self._control_sm = control_sessionmaker
        self._app_sm = app_sessionmaker
        self._executor = executor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._poll_interval = poll_interval

    async def dispatch_once(self) -> int:
        """Reclaim expired leases, then claim and run each queued run. Returns the number run."""
        await self._reclaim_expired()
        async with self._control_sm() as session:
            refs = await queued_run_refs(session, limit=self._batch_size)

        dispatched = 0
        for ref in refs:
            if not await self._claim(ref):
                continue  # a 409 — another worker owns it; don't retry
            await self._process(ref)
            dispatched += 1
        return dispatched

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll loop (the correctness floor). NOTIFY wiring layers on top of this same dispatch."""
        while not stop.is_set():
            try:
                await self.dispatch_once()
            except Exception:  # a bad run must not kill the loop
                _log.exception("dispatch_failed", worker_id=self._worker_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)

    async def _claim(self, ref: RunRef) -> bool:
        async with tenant_session(self._app_sm, ref.workspace_id) as session:
            return await claim_queued_run(
                session, ref.id, owner=self._worker_id, lease_seconds=self._lease_seconds
            )

    async def _process(self, ref: RunRef) -> None:
        async def is_canceled() -> bool:
            async with tenant_session(self._app_sm, ref.workspace_id) as session:
                run = await get_run(session, ref.id)
                return run is not None and run.status == RunStatus.CANCELING

        try:
            result = await self._executor.execute(
                workspace_id=ref.workspace_id,
                company_id=ref.company_id,
                directive=ref.directive,
                is_canceled=is_canceled,
            )
        except Exception as exc:  # executor failure is a failed run, not a dead worker
            _log.exception("run_execution_failed", run_id=ref.id)
            result = ExecutionResult(status=RunStatus.FAILED, error=repr(exc))

        async with tenant_session(self._app_sm, ref.workspace_id) as session:
            await finalize_run(
                session, ref.id, owner=self._worker_id, status=result.status, error=result.error
            )

    async def _reclaim_expired(self) -> None:
        async with self._control_sm() as session:
            expired = await expired_lease_refs(session)
        for run_id, workspace_id in expired:
            async with tenant_session(self._app_sm, workspace_id) as session:
                if await reclaim_run(session, run_id):
                    _log.warning("run_lease_reclaimed", run_id=run_id)
