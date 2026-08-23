"""Durable Dream session checkpoint pointers are ordered, tenant-scoped, and immutable."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from dream import RunTrace, SessionHandle
from dream.services.session_store import SessionCostSnapshot
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.companies import create_company
from podium.db import metadata as _metadata
from podium.db import tenant_session
from podium.runs import (
    CheckpointReplayConflictError,
    CheckpointSessionMismatchError,
    DurableArtifactRef,
    create_run,
    list_run_session_checkpoints,
    save_run_session_checkpoint,
)
from podium.runs.models import RunSessionCheckpointRow
from podium.workspaces import create_workspace

_SAVED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
_USAGE = SessionCostSnapshot(
    input_tokens=10,
    output_tokens=20,
    cache_read_tokens=3,
    cache_write_tokens=4,
    cost_usd=0.12,
)


def _snapshot_ref(name: str = "session-1-v1") -> DurableArtifactRef:
    return DurableArtifactRef(f"s3://snapshots/{name}.json")


def _trace_ref(name: str = "session-1-v1") -> DurableArtifactRef:
    return DurableArtifactRef(f"s3://traces/{name}.jsonl")


async def _workspace_run(
    admin: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[UUID, UUID]:
    assert _metadata is not None  # register every FK target for this focused service test
    async with admin() as session, session.begin():
        workspace = await create_workspace(session, name=slug, slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=f"{slug}-company", name=slug
        )
        run, _ = await create_run(
            session,
            workspace_id=workspace.id,
            company_id=company.id,
            directive="ship it",
            idempotency_key=f"{slug}-run",
        )
        return workspace.id, run.id


def _handle(*, session_id: str = "session-1", saved_at: datetime = _SAVED_AT) -> SessionHandle:
    return SessionHandle(
        session_id=session_id,
        path=Path(f"/durable/sessions/{session_id}.json"),
        working_dir="/worktree",
        schema_version=2,
        saved_at=saved_at,
        usage_delta=_USAGE,
        usage_total=_USAGE,
    )


def _trace(session_id: str = "session-1") -> RunTrace:
    return RunTrace(session_id=session_id, events=())


async def test_save_persists_a_typed_immutable_checkpoint_and_exact_retry_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    handle = _handle()
    trace = _trace()

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        saved = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=handle,
            trace=trace,
            snapshot_ref=_snapshot_ref(),
            trace_ref=_trace_ref(),
        )
        retried = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=handle,
            trace=trace,
            snapshot_ref=_snapshot_ref(),
            trace_ref=_trace_ref(),
        )
        checkpoints = await list_run_session_checkpoints(session, run_id=run_id)

    assert saved == retried
    assert checkpoints == [saved]
    assert saved.workspace_id == workspace_id
    assert saved.run_id == run_id
    assert saved.session_id == "session-1"
    assert saved.sequence_no == 1
    assert saved.snapshot_schema_version == 2
    assert saved.snapshot_ref == "s3://snapshots/session-1-v1.json"
    assert saved.working_dir == "/worktree"
    assert saved.saved_at == _SAVED_AT
    assert saved.usage_delta_input_tokens == 10
    assert saved.usage_total_output_tokens == 20
    assert saved.trace_ref == "s3://traces/session-1-v1.jsonl"
    assert saved.trace_event_count == 0
    with pytest.raises(FrozenInstanceError):
        saved.trace_ref = "changed"  # type: ignore[misc]


async def test_save_orders_multiple_checkpoints_for_one_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        first = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(),
            trace=_trace(),
            snapshot_ref=_snapshot_ref("first"),
            trace_ref=_trace_ref("first"),
        )
        second = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(saved_at=_SAVED_AT + timedelta(seconds=1)),
            trace=_trace(),
            snapshot_ref=_snapshot_ref("second"),
            trace_ref=_trace_ref("second"),
        )
        checkpoints = await list_run_session_checkpoints(session, run_id=run_id)

    assert [checkpoint.checkpoint_id for checkpoint in checkpoints] == [
        first.checkpoint_id,
        second.checkpoint_id,
    ]
    assert first.checkpoint_id < second.checkpoint_id
    assert [checkpoint.sequence_no for checkpoint in checkpoints] == [1, 2]
    assert first.snapshot_ref != second.snapshot_ref


async def test_list_preserves_global_append_order_across_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        first = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(session_id="session-b"),
            trace=_trace("session-b"),
            snapshot_ref=_snapshot_ref("first"),
            trace_ref=_trace_ref("first"),
        )
        second = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(
                session_id="session-a", saved_at=_SAVED_AT + timedelta(seconds=1)
            ),
            trace=_trace("session-a"),
            snapshot_ref=_snapshot_ref("second"),
            trace_ref=_trace_ref("second"),
        )
        checkpoints = await list_run_session_checkpoints(session, run_id=run_id)

    assert [checkpoint.checkpoint_id for checkpoint in checkpoints] == [
        first.checkpoint_id,
        second.checkpoint_id,
    ]


async def test_concurrent_saves_are_serialized_into_one_session_sequence(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    first_saved = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def save_first() -> None:
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await save_run_session_checkpoint(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                handle=_handle(),
                trace=_trace(),
                snapshot_ref=_snapshot_ref("first"),
                trace_ref=_trace_ref("first"),
            )
            first_saved.set()
            await release_first.wait()

    async def save_second() -> None:
        await first_saved.wait()
        second_started.set()
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await save_run_session_checkpoint(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                handle=_handle(saved_at=_SAVED_AT + timedelta(seconds=1)),
                trace=_trace(),
                snapshot_ref=_snapshot_ref("second"),
                trace_ref=_trace_ref("second"),
            )

    first_task = asyncio.create_task(save_first())
    second_task = asyncio.create_task(save_second())
    await second_started.wait()
    await asyncio.sleep(0.05)
    assert not second_task.done()
    release_first.set()
    await asyncio.gather(first_task, second_task)

    async with tenant_session(app_sessionmaker, workspace_id) as session:
        checkpoints = await list_run_session_checkpoints(session, run_id=run_id)
    assert [checkpoint.sequence_no for checkpoint in checkpoints] == [1, 2]


async def test_conflicting_replay_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    handle = _handle()
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=handle,
            trace=_trace(),
            snapshot_ref=_snapshot_ref(),
            trace_ref=_trace_ref("first"),
        )
        with pytest.raises(CheckpointReplayConflictError, match="conflicting checkpoint replay"):
            await save_run_session_checkpoint(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                handle=handle,
                trace=_trace(),
                snapshot_ref=_snapshot_ref(),
                trace_ref=_trace_ref("conflict"),
            )


async def test_mismatched_handle_and_trace_fail_before_a_checkpoint_is_written(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        with pytest.raises(CheckpointSessionMismatchError, match=r"session-1.*session-2"):
            await save_run_session_checkpoint(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                handle=_handle(),
                trace=_trace("session-2"),
                snapshot_ref=_snapshot_ref(),
                trace_ref=_trace_ref("session-2"),
            )
        assert await list_run_session_checkpoints(session, run_id=run_id) == []


async def test_cross_workspace_run_attachment_is_rejected_by_the_composite_foreign_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    owner_workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    other_workspace_id, _ = await _workspace_run(sessionmaker, slug="beta")
    assert owner_workspace_id != other_workspace_id

    with pytest.raises(IntegrityError):
        async with tenant_session(app_sessionmaker, other_workspace_id) as session:
            await save_run_session_checkpoint(
                session,
                workspace_id=other_workspace_id,
                run_id=run_id,
                handle=_handle(),
                trace=_trace(),
                snapshot_ref=_snapshot_ref(),
                trace_ref=_trace_ref(),
            )


async def test_cross_workspace_list_is_empty(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    owner_workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    other_workspace_id, _ = await _workspace_run(sessionmaker, slug="beta")
    async with tenant_session(app_sessionmaker, owner_workspace_id) as session:
        await save_run_session_checkpoint(
            session,
            workspace_id=owner_workspace_id,
            run_id=run_id,
            handle=_handle(),
            trace=_trace(),
            snapshot_ref=_snapshot_ref(),
            trace_ref=_trace_ref(),
        )
    async with tenant_session(app_sessionmaker, other_workspace_id) as session:
        assert await list_run_session_checkpoints(session, run_id=run_id) == []


def test_durable_artifact_references_must_not_be_blank() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        DurableArtifactRef("   ")


async def test_checkpoint_rls_has_only_read_and_append_policies(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        policies = (
            (
                await session.execute(
                    text(
                        "SELECT cmd FROM pg_policies "
                        "WHERE tablename = 'run_session_checkpoints' ORDER BY cmd"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert policies == ["INSERT", "SELECT"]


async def test_app_role_cannot_update_or_delete_checkpoints(
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, run_id = await _workspace_run(sessionmaker, slug="alpha")
    async with tenant_session(app_sessionmaker, workspace_id) as session:
        checkpoint = await save_run_session_checkpoint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            handle=_handle(),
            trace=_trace(),
            snapshot_ref=_snapshot_ref(),
            trace_ref=_trace_ref(),
        )

    with pytest.raises(ProgrammingError):
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await session.execute(
                update(RunSessionCheckpointRow)
                .where(RunSessionCheckpointRow.checkpoint_id == checkpoint.checkpoint_id)
                .values(trace_ref="changed")
            )
    with pytest.raises(ProgrammingError):
        async with tenant_session(app_sessionmaker, workspace_id) as session:
            await session.execute(
                delete(RunSessionCheckpointRow).where(
                    RunSessionCheckpointRow.checkpoint_id == checkpoint.checkpoint_id
                )
            )
