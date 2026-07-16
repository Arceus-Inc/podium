"""Company access — tenant-scoped. Callers pass a `tenant_session`; RLS enforces the workspace wall.

`workspace_id` must equal the session's `app.workspace_id` GUC, or the RLS WITH CHECK rejects the row.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from podium.companies.models import Company


def _mint_id() -> str:
    return f"cmp_{uuid4().hex}"


async def create_company(
    session: AsyncSession,
    *,
    workspace_id: str,
    slug: str,
    name: str,
    config: dict[str, Any] | None = None,
    ledger_backend: str = "sqlite",
) -> Company:
    company = Company(
        id=_mint_id(),
        workspace_id=workspace_id,
        slug=slug,
        name=name,
        config=config or {},
        ledger_backend=ledger_backend,
    )
    session.add(company)
    await session.flush()  # surface constraint/RLS violations within the caller's transaction
    return company


async def list_companies(session: AsyncSession) -> Sequence[Company]:
    """Every company the current tenant session may see. RLS scopes the rows — no WHERE needed."""
    return (await session.execute(select(Company))).scalars().all()


async def get_company(session: AsyncSession, company_id: str) -> Company | None:
    """Fetch by id within the tenant session. RLS returns None for another tenant's id."""
    return await session.get(Company, company_id)
