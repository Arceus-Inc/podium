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
    renew_lease,
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
        max_concurrent: int | None = None,
    ) -> None:
        self._control_sm = control_sessionmaker
        self._app_sm = app_sessionmaker
        self._executor = executor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._renew_interval = max(1, lease_seconds // 3)  # renew well before the lease lapses
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent or batch_size
        self._poll_interval = poll_interval

    async def dispatch_once(self) -> int:
        """Reclaim expired leases, then claim + run queued runs concurrently. Returns the number run."""
        await self._reclaim_expired()
        async with self._control_sm() as session:
            refs = await queued_run_refs(session, limit=self._batch_size)

        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def claim_and_run(ref: RunRef) -> bool:
            async with semaphore:
                if not await self._claim(ref):
                    return False  # a 409 — another worker owns it; don't retry
                await self._process(ref)
                return True

        results = await asyncio.gather(
            *(claim_and_run(ref) for ref in refs), return_exceptions=True
        )
        for ref, result in zip(refs, results, strict=True):
            if isinstance(result, Exception):  # a claim/finalize error must not vanish silently
                _log.error("run_dispatch_failed", run_id=ref.id, error=repr(result))
        return sum(1 for result in results if result is True)

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

        # Keep the lease alive for the whole run so a peer's reclaim can't double-dispatch it.
        keep_alive = asyncio.create_task(self._renew_lease_loop(ref))
        try:
            result = await self._executor.execute(
                run_id=ref.id,
                workspace_id=ref.workspace_id,
                company_id=ref.company_id,
                directive=ref.directive,
                params=ref.params,
                is_canceled=is_canceled,
            )
        except Exception as exc:  # executor failure is a failed run, not a dead worker
            _log.exception("run_execution_failed", run_id=ref.id)
            result = ExecutionResult(status=RunStatus.FAILED, error=repr(exc))
        finally:
            keep_alive.cancel()
            # The guarded loop only ever exits via this cancellation — swallow it so a stale lease
            # can never block finalize (losing a completed result is worse).
            with contextlib.suppress(asyncio.CancelledError):
                await keep_alive

        async with tenant_session(self._app_sm, ref.workspace_id) as session:
            await finalize_run(
                session, ref.id, owner=self._worker_id, status=result.status, error=result.error
            )

    async def _renew_lease_loop(self, ref: RunRef) -> None:
        while True:
            await asyncio.sleep(self._renew_interval)
            # A transient DB blip must not kill the keep-alive; just try again next tick.
            try:
                async with tenant_session(self._app_sm, ref.workspace_id) as session:
                    await renew_lease(
                        session, ref.id, owner=self._worker_id, lease_seconds=self._lease_seconds
                    )
            except Exception:
                _log.warning("run_lease_renew_failed", run_id=ref.id)

    async def _reclaim_expired(self) -> None:
        async with self._control_sm() as session:
            expired = await expired_lease_refs(session)
        for run_id, workspace_id in expired:
            async with tenant_session(self._app_sm, workspace_id) as session:
                if await reclaim_run(session, run_id):
                    _log.warning("run_lease_reclaimed", run_id=run_id)
