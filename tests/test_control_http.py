"""CP-2 — the direction doors: /goals over the control plane (auth → decide → visibility → plane).

The read plane serves the engine's goal tree through the CompanyControlPlane (CP-0); the router
is a thin governed mapping — no engine type escapes, the company must be visible to the actor,
and a foreign workspace's token sees 404, never data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(
        engine_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_company(
    admin: async_sessionmaker[AsyncSession], *, slug: str = "c"
) -> tuple[object, object, str]:
    async with admin() as s, s.begin():
        ws = await create_workspace(s, name=slug.upper(), slug=slug)
        company = await create_company(s, workspace_id=ws.id, slug=slug, name=slug.upper())
        _, token = await create_api_key(s, workspace_id=ws.id, name="k")
        return ws.id, company.id, token


def _seed_goals(database_url: str, company_id: object) -> tuple[str, str]:
    from chorus.ids import mint_id
    from chorus.ledger import Goal, GoalLevel, Ledger

    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    root_id, child_id = mint_id(), mint_id()
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.goals.create(Goal(id=root_id, title="Win launch week"))
        ledger.goals.create(
            Goal(id=child_id, title="Ship the page", level=GoalLevel.TEAM, parent_id=root_id)
        )
    finally:
        ledger.close()
    return root_id, child_id


async def test_goals_door_serves_the_tree(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _seed_company(sessionmaker)
    root_id, child_id = _seed_goals(database_url, company_id)

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/goals",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    tree = response.json()
    assert len(tree) == 1
    assert tree[0]["id"] == root_id
    assert tree[0]["title"] == "Win launch week"
    assert [child["id"] for child in tree[0]["children"]] == [child_id]


async def test_goals_door_requires_auth(
    sessionmaker: async_sessionmaker[AsyncSession], api: httpx.AsyncClient
) -> None:
    ws_id, company_id, _ = await _seed_company(sessionmaker)
    response = await api.get(f"/v1/workspaces/{ws_id}/companies/{company_id}/goals")
    assert response.status_code == 401


async def test_foreign_workspace_is_refused_at_decide(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_a, company_a, _ = await _seed_company(sessionmaker, slug="a")
    _seed_goals(database_url, company_a)
    _, _, token_b = await _seed_company(sessionmaker, slug="b")

    response = await api.get(
        f"/v1/workspaces/{ws_a}/companies/{company_a}/goals",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    # House convention for workspace-prefixed routes: a foreign workspace path is 403 at
    # decide() (leaks nothing — the path names the workspace, not the company); within the
    # right workspace, an invisible company is 404 via RLS/ownership.
    assert response.status_code == 403


async def test_read_doors_serve_workforce_teams_capacity_status_skills(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """One seed, five doors — each a thin fold of the same plane (OBS P4)."""
    from chorus.ids import mint_id
    from chorus.ledger import Ledger, Task, Team
    from chorus.skills import SkillOrigin, SkillStore
    from chorus.workforce import Employee

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="rd")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.tasks.submit(Task(id=mint_id(), intent="ship it"))
        ledger.teams.create(
            Team(id=mint_id(), name="launch", lead_employee_id="ada", created_by="ada")
        )
        SkillStore(ledger).create(
            employee_id="ada",
            slug="deploy-checklist",
            name="Deploy checklist",
            description="how we ship",
            when_to_use="before any deploy",
            file_inventory=[{"path": "SKILL.md", "content": "# Deploy"}],
            origin=SkillOrigin.CREATED,
            action="create",
        )
    finally:
        ledger.close()

    def _get(path: str) -> object:
        return api.get(
            f"/v1/workspaces/{ws_id}/companies/{company_id}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    workforce = await _get("/workforce")
    assert workforce.status_code == 200
    assert [m["id"] for m in workforce.json()] == ["ada"]

    teams = await _get("/teams")
    assert teams.status_code == 200
    assert [t["name"] for t in teams.json()] == ["launch"]

    capacity = await _get("/capacity")
    assert capacity.status_code == 200
    assert {c["role"] for c in capacity.json()} == {"backend_engineer"}

    status = await _get("/status")
    assert status.status_code == 200
    body = status.json()
    assert body["employees"] == 1
    assert body["open_tasks"] == 1

    skills = await _get("/employees/ada/skills")
    assert skills.status_code == 200
    assert [s["slug"] for s in skills.json()] == ["deploy-checklist"]


async def test_patch_goal_archives_it(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """The first write door: archive is an execution-independent ledger write (M4 §3.2 —
    api-side, short transaction, no conductor round-trip)."""
    ws_id, company_id, token = await _seed_company(sessionmaker, slug="wd")
    root_id, _ = _seed_goals(database_url, company_id)

    response = await api.patch(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/goals/{root_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "archived"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "archived"

    tree = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/goals",
        headers={"Authorization": f"Bearer {token}"},
    )
    root = next(node for node in tree.json() if node["id"] == root_id)
    assert root["status"] == "archived"


async def test_patch_goal_rejects_unknown_status_and_goal(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    ws_id, company_id, token = await _seed_company(sessionmaker, slug="wd2")
    root_id, _ = _seed_goals(database_url, company_id)
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/goals"

    bad_status = await api.patch(f"{base}/{root_id}", headers=headers, json={"status": "gone"})
    assert bad_status.status_code == 422  # closed vocabulary, schema-validated

    from chorus.ids import mint_id

    missing = await api.patch(f"{base}/{mint_id()}", headers=headers, json={"status": "archived"})
    assert missing.status_code == 404


async def test_post_goal_seeds_direction(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """Set direction over HTTP: create a root goal, then a child under it — the tree is durable
    engine truth (horizon's mirror), visible immediately through GET /goals."""
    ws_id, company_id, token = await _seed_company(sessionmaker, slug="sd")
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/goals"

    root = await api.post(
        base, headers=headers, json={"title": "Win launch week", "level": "company"}
    )
    assert root.status_code == 201
    root_id = root.json()["id"]
    assert root.json()["status"] == "active"

    child = await api.post(
        base,
        headers=headers,
        json={"title": "Ship the page", "level": "team", "parent_id": root_id},
    )
    assert child.status_code == 201

    tree = await api.get(base, headers=headers)
    assert [node["id"] for node in tree.json()] == [root_id]
    assert [c["title"] for c in tree.json()[0]["children"]] == ["Ship the page"]


async def test_post_goal_rejects_unknown_parent_and_level(
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    from chorus.ids import mint_id

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="sd2")
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/goals"

    bad_level = await api.post(base, headers=headers, json={"title": "x", "level": "galaxy"})
    assert bad_level.status_code == 422

    orphan = await api.post(
        base, headers=headers, json={"title": "x", "level": "team", "parent_id": mint_id()}
    )
    assert orphan.status_code == 404  # a child must attach to an existing goal


async def test_hire_and_terminate_doors(
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """Workforce writes are data edits with engine invariants (role registry, slug uniqueness,
    routine provisioning) — the door delegates to the real engine facade, never re-implements."""
    ws_id, company_id, token = await _seed_company(sessionmaker, slug="hf")
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}"

    hired = await api.post(
        f"{base}/employees", headers=headers, json={"name": "Ada", "role": "backend_engineer"}
    )
    assert hired.status_code == 201
    assert hired.json()["id"] == "ada"
    assert hired.json()["role"] == "backend_engineer"

    roster = await api.get(f"{base}/workforce", headers=headers)
    assert [m["id"] for m in roster.json()] == ["ada"]

    duplicate = await api.post(
        f"{base}/employees", headers=headers, json={"name": "Ada", "role": "backend_engineer"}
    )
    assert duplicate.status_code == 409  # engine slug invariant surfaces as conflict

    unknown_role = await api.post(
        f"{base}/employees", headers=headers, json={"name": "Zed", "role": "astronaut"}
    )
    assert unknown_role.status_code == 422  # engine role registry refuses

    await api.post(
        f"{base}/employees",
        headers=headers,
        json={"name": "Bex", "role": "pm", "reports_to": "ada"},
    )

    root_protected = await api.delete(f"{base}/employees/ada", headers=headers)
    assert root_protected.status_code == 409  # the org root cannot be terminated (engine invariant)

    gone = await api.delete(f"{base}/employees/bex", headers=headers)
    assert gone.status_code == 204
    roster_after = await api.get(f"{base}/workforce", headers=headers)
    statuses = {m["id"]: m["status"] for m in roster_after.json()}
    assert statuses["bex"] == "terminated"

    missing = await api.delete(f"{base}/employees/nobody", headers=headers)
    assert missing.status_code == 404


async def test_allocation_snapshot_reads_ledger_truth(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """CP-4: allocation is observable state (OBS P6) — queued wakes, running beats with leases,
    blocked tasks — read from the ledger, never reconstructed from events."""
    from datetime import UTC, datetime, timedelta

    from chorus.ids import mint_id
    from chorus.ledger import Ledger, Run, RunStatus, Task, TaskStatus, Wake, WakeReason
    from chorus.workforce import Employee

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="al")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        queued_task = mint_id()
        ledger.tasks.submit(Task(id=queued_task, intent="waiting", assignee_employee_id="ada"))
        ledger.tasks.set_status(queued_task, TaskStatus.TODO)
        ledger.wakes.enqueue(
            Wake(
                id=mint_id(),
                employee_id="ada",
                reason=WakeReason.TASK_ASSIGNED,
                payload={"task_id": queued_task},
            )
        )
        running_task = mint_id()
        run_id = mint_id()
        ledger.tasks.submit(Task(id=running_task, intent="live", assignee_employee_id="ada"))
        ledger.tasks.set_status(running_task, TaskStatus.TODO)
        assert ledger.tasks.checkout(running_task, employee_id="ada", run_id=run_id)
        ledger.runs.create(
            Run(
                id=run_id,
                employee_id="ada",
                task_id=running_task,
                status=RunStatus.RUNNING,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                started_at=datetime.now(UTC),
            )
        )
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/allocation",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [q["task_id"] for q in body["queued"]] == [queued_task]
    assert body["queued"][0]["reason"] == "task_assigned"
    assert [r["run_id"] for r in body["running"]] == [run_id]
    assert body["running"][0]["employee_id"] == "ada"
    assert body["running"][0]["lease_expires_at"] is not None
    assert isinstance(body["blocked"], list)


async def test_costs_door_aggregates_spend(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """CP-4: /costs?by=model|employee|day folds the engine's priced spend ledger."""
    from datetime import UTC, datetime

    from chorus.ids import mint_id
    from chorus.ledger import Ledger
    from chorus.ledger._models import CostEvent
    from chorus.workforce import Employee

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="co")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        for n, (model, cents) in enumerate([("gpt-x", 300), ("gpt-mini", 50)]):
            ledger.cost_events.record(
                CostEvent(
                    id=mint_id(),
                    employee_id="ada",
                    provider="dream",
                    model=model,
                    cost_cents=cents,
                    input_tokens=100,
                    output_tokens=20,
                    occurred_at=datetime(2026, 6, 1 + n, tzinfo=UTC),
                )
            )
    finally:
        ledger.close()

    headers = {"Authorization": f"Bearer {token}"}
    base = f"/v1/workspaces/{ws_id}/companies/{company_id}/costs"

    by_model = await api.get(f"{base}?by=model", headers=headers)
    assert by_model.status_code == 200
    rows = by_model.json()
    assert rows[0] == {
        "key": "gpt-x",
        "cost_cents": 300,
        "input_tokens": 100,
        "output_tokens": 20,
        "events": 1,
    }

    default_grouping = await api.get(base, headers=headers)  # day is the default window unit
    assert default_grouping.status_code == 200
    assert {row["key"] for row in default_grouping.json()} == {"2026-06-01", "2026-06-02"}

    bad = await api.get(f"{base}?by=provider", headers=headers)
    assert bad.status_code == 422


async def test_overview_combines_product_and_engine_truth(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """CP-4: /overview — runs by status (product DB) + workforce/tasks/spend (engine ledger)."""
    from datetime import UTC, datetime

    from chorus.ids import mint_id
    from chorus.ledger import Ledger
    from chorus.ledger._models import CostEvent
    from chorus.workforce import Employee

    from podium.db import tenant_session
    from podium.runs import create_run

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="ov")
    async with tenant_session(app_sessionmaker, ws_id) as s:
        await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d1", idempotency_key="o1"
        )
        await create_run(
            s, workspace_id=ws_id, company_id=company_id, directive="d2", idempotency_key="o2"
        )
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.cost_events.record(
            CostEvent(
                id=mint_id(),
                employee_id="ada",
                provider="dream",
                model="gpt-x",
                cost_cents=250,
                occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runs_by_status"] == {"queued": 2}
    assert body["employees"] == 1
    assert body["spend_cents"] == 250
    assert body["running_beats"] == 0


async def test_report_door_serves_the_org_rollup(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    """CP-4: /report — the inspector's combined manager+leaf rollup, flat counts only."""
    from chorus.ids import mint_id
    from chorus.ledger import Ledger, Task
    from chorus.workforce import Employee

    ws_id, company_id, token = await _seed_company(sessionmaker, slug="rp")
    dsn = database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.tasks.submit(Task(id=mint_id(), intent="one open task"))
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/workspaces/{ws_id}/companies/{company_id}/report",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["employees"] == 1
    assert body["tasks_total"] == 1
    assert body["tasks_done"] == 0
    assert 0.0 <= body["completion_rate"] <= 1.0
