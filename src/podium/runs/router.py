"""Runs HTTP surface: authenticate → decide → tenant_session. The workspace is always the actor's.

Creating a run first fetches the target company through the tenant session, so a company that RLS
hides (another tenant's) yields 404 — no run is ever created against a foreign company.

Ordinary run routes type path ids as `uuid.UUID`, so FastAPI rejects malformed ids with 422 before
any query runs. Session inspection routes deliberately accept strings and parse them locally,
returning the same opaque 404 for malformed, missing, and tenant-invisible company or run ids.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import NoReturn, Protocol, TypeVar, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.control import CompanyControlPlane
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.runs.schemas import RunCreate, RunOut, RunSessionCheckpointOut
from podium.runs.service import (
    create_run,
    get_run,
    list_run_session_checkpoints,
    request_cancel,
)
from podium.runs.session_state import AgentSessionView, RunSessionState

router = APIRouter(prefix="/v1", tags=["runs"])

_T = TypeVar("_T")


@runtime_checkable
class _ControlPlaneProvider(Protocol):
    def read_plane(
        self, *, workspace_id: uuid.UUID, company_id: uuid.UUID
    ) -> CompanyControlPlane: ...


def _authorize(actor: Actor, action: str, company_id: uuid.UUID | None = None) -> None:
    resource = Resource(kind="run", workspace_id=actor.workspace_id, company_id=company_id)
    if not decide(actor, action, resource):
        raise HTTPException(status_code=403, detail="forbidden")


def _get_log_store(request: Request) -> RunLogStore:
    store: RunLogStore = request.app.state.log_store
    return store


def _get_control_provider(request: Request) -> _ControlPlaneProvider:
    provider: object | None = getattr(request.app.state, "control_provider", None)
    if not isinstance(provider, _ControlPlaneProvider):
        raise HTTPException(status_code=503, detail="control plane unavailable")
    return provider


async def _plane_read(
    provider: _ControlPlaneProvider,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    read: Callable[[CompanyControlPlane], _T],
) -> _T:
    """Open a short-lived Chorus read plane only after Podium visibility is established."""

    def _run() -> _T:
        plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
        try:
            return read(plane)
        finally:
            plane.close()

    return await run_in_threadpool(_run)


def _run_not_found() -> NoReturn:
    raise HTTPException(status_code=404, detail="run not found")


def _uuid_or_not_found(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        _run_not_found()


@router.post("/companies/{company_id}/runs", status_code=202, response_model=RunOut)
async def create(
    company_id: uuid.UUID,
    body: RunCreate,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "create", company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, company_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="company not found")
        run, _created = await create_run(
            session,
            workspace_id=actor.workspace_id,
            company_id=company_id,
            directive=body.directive,
            idempotency_key=body.idempotency_key,
            params=body.params(),
        )
        return RunOut.model_validate(run)


@router.get("/companies/{company_id}/runs/{run_id}", response_model=RunOut)
async def get(
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "read", company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        run = await get_run(session, run_id, user_id=actor.user_id)
    if run is None or run.company_id != company_id:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut.model_validate(run)


@router.get(
    "/companies/{company_id}/runs/{run_id}/session-checkpoints",
    response_model=tuple[RunSessionCheckpointOut, ...],
)
async def list_session_checkpoints(
    company_id: str,
    run_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> tuple[RunSessionCheckpointOut, ...]:
    """List a visible run's immutable Dream checkpoints in global append order."""
    parsed_company_id = _uuid_or_not_found(company_id)
    parsed_run_id = _uuid_or_not_found(run_id)
    _authorize(actor, "read")
    if actor.company_id is not None and actor.company_id != parsed_company_id:
        _run_not_found()
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_company(session, parsed_company_id, user_id=actor.user_id) is None:
            _run_not_found()
        run = await get_run(session, parsed_run_id)
        if run is None or run.company_id != parsed_company_id:
            _run_not_found()
        checkpoints = await list_run_session_checkpoints(session, run_id=parsed_run_id)
    return tuple(RunSessionCheckpointOut.model_validate(checkpoint) for checkpoint in checkpoints)


@router.get(
    "/companies/{company_id}/runs/{run_id}/session-state",
    response_model=RunSessionState,
)
async def get_session_state(
    company_id: str,
    run_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: _ControlPlaneProvider = Depends(_get_control_provider),
) -> RunSessionState:
    """Return a visible run's latest authoritative Chorus agent-session state."""
    parsed_company_id = _uuid_or_not_found(company_id)
    parsed_run_id = _uuid_or_not_found(run_id)
    _authorize(actor, "read")
    if actor.company_id is not None and actor.company_id != parsed_company_id:
        _run_not_found()
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, parsed_company_id, user_id=actor.user_id)
        run = await get_run(session, parsed_run_id)
        if company is None or run is None or run.company_id != parsed_company_id:
            _run_not_found()
        engine_task_id = run.engine_task_id

    session_view: AgentSessionView | None = None
    if engine_task_id is not None:
        session_view = await _plane_read(
            provider,
            workspace_id=actor.workspace_id,
            company_id=parsed_company_id,
            read=lambda plane: plane.session_state.latest_for_task(engine_task_id),
        )
    return RunSessionState(
        run_id=parsed_run_id,
        engine_task_id=engine_task_id,
        session=session_view,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel(
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "cancel")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_run(session, run_id, user_id=actor.user_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        await request_cancel(session, run_id)
        run = await get_run(session, run_id, user_id=actor.user_id)
        assert run is not None
        return RunOut.model_validate(run)


@router.get("/runs/{run_id}/logs")
async def logs(
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    store: RunLogStore = Depends(_get_log_store),
) -> Response:
    """Stream a run's durable transcript from the log store. 404 if the run has produced none."""
    _authorize(actor, "read")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        run = await get_run(session, run_id, user_id=actor.user_id)
    if run is None or run.log_ref is None or not store.exists(run_id):
        raise HTTPException(status_code=404, detail="no logs for this run")
    data, sha256 = store.load(run_id)  # single read; Starlette sets Content-Length from the bytes
    return Response(
        content=data, media_type="text/plain; charset=utf-8", headers={"X-Log-Sha256": sha256}
    )
