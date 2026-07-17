"""The cockpit snapshot — one call feeds every nav count and the component map (design §3).

Visibility only: internal components (delegation kernel, harness, lattice gate) surface as
counts and read-only trees, never as control endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401  -- register every model so FK targets resolve
from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.main import create_app
from podium.workspaces import create_workspace


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    )
    app.state.cockpit_workdir = tmp_path  # per-company lattice/semantic trees live under this
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_snapshot_covers_every_component(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    from chorus.ids import mint_id
    from chorus.ledger import Ledger, Task, Team
    from chorus.skills import SkillOrigin, SkillStore
    from chorus.workforce import Employee
    from lattice.contracts.atom import Atom
    from lattice.stores.memory_md import MemoryMdStore

    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="CK", slug="ck")
        company = await create_company(s, workspace_id=ws.id, slug="ck", name="CK")
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        ws_id, company_id = ws.id, company.id

    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.tasks.submit(Task(id=mint_id(), intent="open work"))
        ledger.teams.create(
            Team(id=mint_id(), name="launch", lead_employee_id="ada", created_by="ada")
        )
        SkillStore(ledger).create(
            employee_id="ada",
            slug="ship-checklist",
            name="Ship checklist",
            description="how we ship",
            when_to_use="always",
            file_inventory=[{"path": "SKILL.md", "content": "# Ship"}],
            origin=SkillOrigin.CREATED,
            action="create",
        )
    finally:
        ledger.close()

    # One durable semantic fact under the company's lattice tree (design correction: lattice
    # yields semantic atoms AND skills).
    atoms = MemoryMdStore(tmp_path / str(company_id) / "lattice")
    atoms.write(
        Atom(
            key="deploy.window",
            value="ship after 10am IST",
            employee_id="ada",
            source_run_ids=(mint_id(),),
            created_at=datetime.now(UTC),
        )
    )

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/cockpit/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    snapshot = response.json()
    components = snapshot["components"]
    assert components["chorus"]["employees"] == 1
    assert components["chorus"]["open_tasks"] == 1
    assert components["delegation"]["teams"] == 1
    assert components["skills"]["heads"] == 1
    assert components["semantic"]["atoms"] == 1
    assert components["semantic"]["by_employee"] == {"ada": 1}
    assert components["episodic"]["records"] == 0  # engine store, none yet
    assert components["horizon"]["goals"] == 0
    assert components["llmops"]["spend_cents"] == 0
    assert "generated_at" in snapshot

    detail = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/cockpit/semantic/ada",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    facts = detail.json()
    assert facts[0]["key"] == "deploy.window"
    assert facts[0]["value"] == "ship after 10am IST"
    assert facts[0]["activation"] == 1.0


async def test_snapshot_requires_auth(
    sessionmaker: async_sessionmaker[AsyncSession], api: httpx.AsyncClient
) -> None:
    async with sessionmaker() as s, s.begin():
        ws = await create_workspace(s, name="CX", slug="cx")
        company = await create_company(s, workspace_id=ws.id, slug="cx", name="CX")
        ws_id, company_id = ws.id, company.id
    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/cockpit/snapshot"
    )
    assert response.status_code == 401
