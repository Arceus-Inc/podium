"""Control-plane HTTP doors. Every handler: authenticate → decide() → company visibility → plane.

Thin governed mappings onto CompanyControlPlane sub-facades (M4 §3.3): the router owns auth and
DTO serialization, the plane owns engine access — no engine type or connection escapes. Plane
reads run in a worker thread (the engine ledger is sync psycopg by design)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies.service import get_company
from podium.control._delegation import CapacityEntry, TeamSummary
from podium.control._direction import GoalNode
from podium.control._observe import CompanyStatus, SkillSummary
from podium.control._plane import CompanyControlPlane, ControlPlaneProvider
from podium.control._workforce import EmployeeView
from podium.db import tenant_session

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/companies/{company_id}", tags=["control"])

_T = TypeVar("_T")


def get_control_provider(request: Request) -> ControlPlaneProvider:
    provider = getattr(request.app.state, "control_provider", None)
    if provider is None:  # wired at lifespan from settings; absent only in misconfigured deploys
        raise HTTPException(status_code=503, detail="control plane unavailable")
    return provider  # type: ignore[no-any-return]


async def _visible_company_or_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    """Auth wall: decide() + product-DB visibility (RLS + ownership) — 404, never data."""
    resource = Resource(kind="company", workspace_id=workspace_id, company_id=company_id)
    if not decide(actor, "read", resource):
        raise HTTPException(status_code=403, detail="forbidden")
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, company_id, user_id=actor.user_id)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")


async def _plane_read(
    provider: ControlPlaneProvider,
    *,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    read: Callable[[CompanyControlPlane], _T],
) -> _T:
    """One plane per request, opened and closed in a worker thread (sync engine connection)."""

    def _run() -> _T:
        plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
        try:
            return read(plane)
        finally:
            plane.close()

    return await run_in_threadpool(_run)


@router.get("/goals", response_model=list[GoalNode])
async def goal_tree(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[GoalNode]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.direction.goal_tree(),
    )


@router.get("/workforce", response_model=list[EmployeeView])
async def workforce(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[EmployeeView]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.workforce.roster(),
    )


@router.get("/teams", response_model=list[TeamSummary])
async def teams(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[TeamSummary]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.delegation.teams(),
    )


@router.get("/capacity", response_model=list[CapacityEntry])
async def capacity(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[CapacityEntry]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.delegation.capacity(),
    )


@router.get("/status", response_model=CompanyStatus)
async def status(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> CompanyStatus:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.status(),
    )


@router.get("/employees/{employee_id}/skills", response_model=list[SkillSummary])
async def employee_skills(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    employee_id: str,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> list[SkillSummary]:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    return await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.observe.skills(employee_id),
    )


class GoalPatch(BaseModel):
    status: Literal["active", "archived"]


@router.patch("/goals/{goal_id}", response_model=GoalNode)
async def patch_goal(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    goal_id: str,
    body: GoalPatch,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    provider: ControlPlaneProvider = Depends(get_control_provider),
) -> GoalNode:
    await _visible_company_or_404(sessionmaker, actor, workspace_id, company_id)
    node = await _plane_read(
        provider,
        workspace_id=workspace_id,
        company_id=company_id,
        read=lambda plane: plane.direction.set_goal_status(goal_id, body.status),
    )
    if node is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return node
