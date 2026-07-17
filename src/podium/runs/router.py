"""Runs HTTP surface: authenticate → decide → tenant_session. The workspace is always the actor's.

Creating a run first fetches the target company through the tenant session, so a company that RLS
hides (another tenant's) yields 404 — no run is ever created against a foreign company.

Path ids are `uuid.UUID` — FastAPI validates the shape at the edge (a malformed id is a 422 before
any query runs).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.db import tenant_session
from podium.logs import RunLogStore
from podium.runs.schemas import RunCreate, RunOut
from podium.runs.service import create_run, get_run, request_cancel

router = APIRouter(prefix="/v1", tags=["runs"])


def _authorize(actor: Actor, action: str, company_id: uuid.UUID | None = None) -> None:
    resource = Resource(kind="run", workspace_id=actor.workspace_id, company_id=company_id)
    if not decide(actor, action, resource):
        raise HTTPException(status_code=403, detail="forbidden")


def _get_log_store(request: Request) -> RunLogStore:
    store: RunLogStore = request.app.state.log_store
    return store


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
        run = await get_run(session, run_id)
    if run is None or run.company_id != company_id:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel(
    run_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RunOut:
    _authorize(actor, "cancel")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        if await get_run(session, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        await request_cancel(session, run_id)
        run = await get_run(session, run_id)
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
        run = await get_run(session, run_id)  # RLS hides a foreign run → None → 404
    if run is None or run.log_ref is None or not store.exists(run_id):
        raise HTTPException(status_code=404, detail="no logs for this run")
    data, sha256 = store.load(run_id)  # single read; Starlette sets Content-Length from the bytes
    return Response(
        content=data, media_type="text/plain; charset=utf-8", headers={"X-Log-Sha256": sha256}
    )
