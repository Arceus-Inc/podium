"""Read-only eval-run comparison door with opaque company and run lookup failures."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from podium.auth import Actor, enforce_rate_limit, get_sessionmaker
from podium.companies import get_company
from podium.control import ControlPlaneProvider
from podium.db import tenant_session
from podium.evaluations.schemas import EvalRunComparisonOut
from podium.evaluations.service import (
    IncompatibleEvalRunsError,
    UnknownEvalRunError,
)

router = APIRouter(prefix="/v1/companies", tags=["evaluations"])


def _get_control_provider(request: Request) -> ControlPlaneProvider:
    provider = getattr(request.app.state, "control_provider", None)
    if not isinstance(provider, ControlPlaneProvider):
        raise HTTPException(status_code=503, detail="control plane unavailable")
    return provider


async def _company_visible(
    sessionmaker: async_sessionmaker[AsyncSession], actor: Actor, company_id: uuid.UUID
) -> bool:
    if actor.company_id is not None and actor.company_id != company_id:
        return False
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        return await get_company(session, company_id, user_id=actor.user_id) is not None


@router.get("/{company_id}/eval-runs/compare", response_model=EvalRunComparisonOut)
async def compare(
    company_id: str,
    baseline_run_id: str | None = None,
    candidate_run_id: str | None = None,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(_get_control_provider),
) -> EvalRunComparisonOut:
    """Compare two visible runs, preserving their pinned record rather than scoring prose."""
    try:
        parsed_company_id = uuid.UUID(company_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="eval run not found") from None
    if not await _company_visible(sessionmaker, actor, parsed_company_id):
        raise HTTPException(status_code=404, detail="eval run not found")

    def _read() -> EvalRunComparisonOut:
        plane = provider.read_plane(workspace_id=actor.workspace_id, company_id=parsed_company_id)
        try:
            comparison = plane.evaluations.compare(
                baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id
            )
        finally:
            plane.close()
        return EvalRunComparisonOut.from_domain(comparison)

    try:
        return await run_in_threadpool(_read)
    except UnknownEvalRunError:
        raise HTTPException(status_code=404, detail="eval run not found") from None
    except IncompatibleEvalRunsError:
        raise HTTPException(status_code=409, detail="eval runs are not comparable") from None
