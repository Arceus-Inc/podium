"""Bridge a chorus EventBus into the per-company EventMirror.

The chorus `subscribe` callback is synchronous and may fire on a beat's thread, so it does the
minimum — hand the event to an asyncio queue via `call_soon_threadsafe` (never blocking the beat).
An async drainer translates each event and writes it through the mirror. The queue is bounded: under
an extreme burst it drops-and-counts (a best-effort telemetry tap; the run lifecycle is exactly-once
via the conductor). Event→run routing is the mirror's job; an optional `resolve_root` maps a
descendant task id to its run's root task first (chorus lineage).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from podium.conductor._mirror import EventMirror

_log = structlog.get_logger("podium.ingest")


class EventBusLike(Protocol):
    def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]: ...


class EventIngest:
    def __init__(
        self,
        bus: EventBusLike,
        mirror: EventMirror,
        *,
        resolve_root: Callable[[str], str | None] | None = None,
        queue_maxsize: int = 10_000,
    ) -> None:
        self._bus = bus
        self._mirror = mirror
        self._resolve_root = resolve_root
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub: Callable[[], None] | None = None
        self._drainer: asyncio.Task[None] | None = None
        self.dropped = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsub = self._bus.subscribe(self._on_event)
        self._drainer = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._drainer is not None:
            self._drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drainer
            self._drainer = None

    def _on_event(self, event: Any) -> None:
        loop = self._loop
        if loop is not None:  # hop back onto the loop; the callback may be on a beat thread
            loop.call_soon_threadsafe(self._offer, event)

    def _offer(self, event: Any) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1  # never block the producer; telemetry is best-effort

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                trace_id = event.trace_id
                if (
                    trace_id is None
                    and event.task_id is not None
                    and self._resolve_root is not None
                ):
                    trace_id = self._resolve_root(event.task_id)  # pre-spine emitter fallback
                await self._mirror.record(
                    type=event.kind.value,
                    payload=dict(event.payload),
                    task_id=event.task_id,
                    trace_id=trace_id,
                    employee_id=event.employee_id,
                    at=event.at,
                )
            except Exception:  # a bad event must not kill the drainer
                _log.exception("ingest_record_failed")
