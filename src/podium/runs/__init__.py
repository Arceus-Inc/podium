"""Runs domain package — the directive-execution resource and its compare-and-swap lifecycle."""

from __future__ import annotations

from podium.runs.models import TERMINAL_STATUSES, Run, RunStatus
from podium.runs.service import (
    IdempotencyKeyReuseError,
    RunRef,
    active_engine_tasks,
    claim_queued_run,
    create_run,
    expired_lease_refs,
    finalize_run,
    get_run,
    get_visible_run,
    list_runs,
    queued_run_refs,
    reclaim_run,
    renew_lease,
    request_cancel,
    request_fingerprint,
    rollup_run_counts,
    set_engine_task_id,
    set_log_ref,
)

__all__ = [
    "TERMINAL_STATUSES",
    "IdempotencyKeyReuseError",
    "Run",
    "RunRef",
    "RunStatus",
    "active_engine_tasks",
    "claim_queued_run",
    "create_run",
    "expired_lease_refs",
    "finalize_run",
    "get_run",
    "get_visible_run",
    "list_runs",
    "queued_run_refs",
    "reclaim_run",
    "renew_lease",
    "request_cancel",
    "request_fingerprint",
    "rollup_run_counts",
    "set_engine_task_id",
    "set_log_ref",
]
