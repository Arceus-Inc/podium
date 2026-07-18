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
    rollup_run_counts,
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
        self._inflight: set[asyncio.Task[None]] = set()

    async def dispatch_once(self) -> int:
        """Reclaim expired leases, then claim queued runs and execute them in the BACKGROUND.

        Returns the number claimed. Execution is spawned, never awaited here: awaiting the
        batch let one long delegation run hold the poll loop hostage while every later run
        sat queued (live 2026-07-18). In-flight work is bounded by ``max_concurrent``; runs
        beyond capacity simply stay queued for a later poll. ``drain()`` is the explicit join
        for tests and shutdown.
        """
        await self._reclaim_expired()
        capacity = self._max_concurrent - len(self._inflight)
        if capacity <= 0:
            return 0
        async with self._control_sm() as session:
            refs = await queued_run_refs(session, limit=min(self._batch_size, capacity))
        claimed = 0
        for ref in refs:
            if not await self._claim(ref):
                continue  # a 409 — another worker owns it; don't retry
            claimed += 1
            task = asyncio.create_task(self._process_guarded(ref))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        return claimed

    async def _process_guarded(self, ref: RunRef) -> None:
        try:
            await self._process(ref)
        except Exception:  # a claim/finalize error must not vanish silently
            _log.exception("run_dispatch_failed", run_id=ref.id)

    async def drain(self) -> None:
        """Await every in-flight run — the deterministic join for tests and shutdown."""
        while self._inflight:
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)

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
            await rollup_run_counts(session, ref.id)  # the run's spine folded once, durably

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
