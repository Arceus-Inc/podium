"""Per-company event mirror — the single writer of a company's event log.

One mirror per live CompanyGraph. Because it is the only writer for that company, it assigns the
per-company monotonic `seq` from an in-memory counter (seeded once from the table) with no
contention, and commits sequentially — so the seq stream has no gaps or out-of-order rows. Each event
is persisted and NOTIFY'd in ONE transaction (transactional outbox), so a live tailer can never see a
notify without its row.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.db import tenant_session
from podium.events import Event, append_event, max_company_seq

EVENTS_CHANNEL = "podium_events"


class EventMirror:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        company_id: str,
        workspace_id: str,
    ) -> None:
        self._sm = sessionmaker
        self._company_id = company_id
        self._workspace_id = workspace_id
        self._next_seq: int | None = None
        self._task_to_run: dict[str, str] = {}
        self._lock = (
            asyncio.Lock()
        )  # serialise seq assignment even if record() is called concurrently

    def register_run(self, *, run_id: str, engine_task_id: str) -> None:
        """Tell the mirror which podium run a chorus root task belongs to (for event routing)."""
        self._task_to_run[engine_task_id] = run_id

    async def record(
        self,
        *,
        type: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        employee_id: str | None = None,
        at: datetime | None = None,
    ) -> Event:
        """Persist one event with the next seq and wake the stream — atomically."""
        async with self._lock:
            async with tenant_session(self._sm, self._workspace_id) as session:
                seq = self._next_seq
                if seq is None:
                    seq = await max_company_seq(session, self._company_id) + 1
                run_id = self._task_to_run.get(task_id) if task_id is not None else None
                event = await append_event(
                    session,
                    company_id=self._company_id,
                    seq=seq,
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    type=type,
                    employee_id=employee_id,
                    payload=payload,
                    created_at=at or datetime.now(UTC),
                )
                # A collision here means two mirrors write one company — a violated invariant, not
                # a retry case: this mirror is meant to be the company's single writer.
                await _notify(session, self._company_id, seq, run_id, type)
            # Advance ONLY after the transaction committed — a failed commit reuses this seq (no gap).
            self._next_seq = seq + 1
            return event


async def _notify(
    session: AsyncSession, company_id: str, seq: int, run_id: str | None, type: str
) -> None:
    # Small payload — the row carries the full event; a tailer reads it by (company_id, seq).
    body = json.dumps({"company_id": company_id, "seq": seq, "run_id": run_id, "type": type})
    await session.execute(
        text("SELECT pg_notify(:channel, :body)"), {"channel": EVENTS_CHANNEL, "body": body}
    )
