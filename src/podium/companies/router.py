"""Companies HTTP surface. Every handler: authenticate (require_actor) → decide() → tenant_session.

The workspace the query runs in is ALWAYS `actor.workspace_id` (from the credential), never the raw
path — so RLS scopes to the caller's tenant even if a path check were ever wrong.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import Actor, Resource, decide, enforce_rate_limit, get_sessionmaker
from podium.companies.schemas import CompanyCreate, CompanyOut
from podium.companies.service import create_company, get_company, list_companies
from podium.db import tenant_session

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/companies", tags=["companies"])


def _authorize(
    actor: Actor, action: str, workspace_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> None:
    resource = Resource(kind="company", workspace_id=workspace_id, company_id=company_id)
    if not decide(actor, action, resource):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("", status_code=201, response_model=CompanyOut)
async def create(
    workspace_id: uuid.UUID,
    body: CompanyCreate,
    response: Response,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> CompanyOut:
    _authorize(actor, "create", workspace_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await create_company(
            session,
            workspace_id=actor.workspace_id,
            slug=body.slug,
            name=body.name,
            config=body.config,
        )
        response.headers["Location"] = f"/v1/workspaces/{workspace_id}/companies/{company.id}"
        return CompanyOut.model_validate(company)


@router.get("", response_model=list[CompanyOut])
async def list_(
    workspace_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> list[CompanyOut]:
    _authorize(actor, "read", workspace_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        rows = await list_companies(session)
        return [CompanyOut.model_validate(row) for row in rows]


@router.get("/{company_id}", response_model=CompanyOut)
async def get(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: Actor = Depends(enforce_rate_limit),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> CompanyOut:
    _authorize(actor, "read", workspace_id, company_id)
    async with tenant_session(sessionmaker, actor.workspace_id) as session:
        company = await get_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    return CompanyOut.model_validate(company)
