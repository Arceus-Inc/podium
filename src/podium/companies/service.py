"""Company access — tenant-scoped. Callers pass a `tenant_session`; RLS enforces the workspace wall.

`workspace_id` must equal the session's `app.workspace_id` GUC, or the RLS WITH CHECK rejects the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from podium.companies.models import Company


async def create_company(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    slug: str,
    name: str,
    config: dict[str, Any] | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> Company:
    company = Company(
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        slug=slug,
        name=name,
        config=config or {},
    )
    session.add(company)
    await session.flush()  # surface constraint/RLS violations within the caller's transaction
    return company


async def mark_company_idle(session: AsyncSession, company_id: uuid.UUID) -> bool:
    """The provisioning saga's happy edge: provisioning -> idle, guarded so a retry is a no-op and
    a later state (running/stopped) is never resurrected. True iff this call made the flip."""
    stmt = (
        update(Company)
        .where(Company.id == company_id, Company.state == "provisioning")
        .values(state="idle")
        .returning(Company.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def company_visible(company: Company, *, user_id: uuid.UUID | None) -> bool:
    """Ownership authz WITHIN the workspace (M5 §2.5): a service actor (user_id None) sees all;
    a user actor sees workspace-owned companies (no owner) and their own."""
    return user_id is None or company.owner_user_id is None or company.owner_user_id == user_id


async def list_companies(
    session: AsyncSession, *, user_id: uuid.UUID | None = None
) -> Sequence[Company]:
    """Every company the actor may see: RLS walls the workspace; ownership filters within it —
    in SQL, so a user actor never over-fetches other members' rows."""
    stmt = select(Company)
    if user_id is not None:
        stmt = stmt.where(or_(Company.owner_user_id.is_(None), Company.owner_user_id == user_id))
    return (await session.execute(stmt)).scalars().all()


async def get_company(
    session: AsyncSession, company_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> Company | None:
    """Fetch by id within the tenant session. RLS hides another tenant's id; ownership hides
    another member's company (both read as None → 404 at the boundary)."""
    company = await session.get(Company, company_id)
    if company is None or not company_visible(company, user_id=user_id):
        return None
    return company
