"""Cockpit HTTP doors — visibility only, behind the same walls as every control door.

authenticate → decide() → product-DB company visibility → plane/workdir reads in a worker
thread. Internal components (delegation kernel, harness, lattice gate) surface here as counts
and read-only detail; control never enters through the cockpit.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from cockpit.views import build_snapshot, semantic_facts
from podium.auth import Actor, enforce_rate_limit, get_sessionmaker
from podium.control.router import (
    _visible_company_or_404,
    get_control_provider,
)

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/companies/{company_id}/cockpit", tags=["cockpit"]
)


def get_cockpit_workdir(request: Request) -> Path:
    workdir = getattr(request.app.state, "cockpit_workdir", None)
    if workdir is None:  # wired at lifespan from settings.workdir
        raise HTTPException(status_code=503, detail="cockpit unavailable")
    return Path(workdir)


@router.get("/snapshot")
async def snapshot(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> dict[str, Any]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    provider = get_control_provider(request)
    workdir = get_cockpit_workdir(request)

    def _read() -> dict[str, Any]:
        plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
        try:
            return build_snapshot(plane, workdir=workdir, company_id=company_id)
        finally:
            plane.close()

    return await run_in_threadpool(_read)


@router.get("/semantic/{employee_id}")
async def semantic_detail(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    request: Request,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> list[dict[str, Any]]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    workdir = get_cockpit_workdir(request)
    return await run_in_threadpool(semantic_facts, workdir, company_id, employee_id)


__all__ = ["router"]
