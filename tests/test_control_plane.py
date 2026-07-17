"""CP-0 — the CompanyControlPlane seam (product-plane program, M4a).

The read plane is podium's product composition root opened WITHOUT a heartbeat or model keys:
just the company's RLS-scoped engine ledger behind typed sub-facades that translate engine rows
into podium DTOs. Routers and dashboard snapshots will map onto these 1:1 — nothing past the
plane ever sees `Ledger` internals.
"""

from __future__ import annotations

import uuid

import pytest

from podium.control import CompanyControlPlane, ControlPlaneProvider

pytestmark = pytest.mark.anyio


def _pg_conninfo(database_url: str, *, user: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", f"://{user}@")


def _seed_company(dsn: str, company_id: uuid.UUID, *, employees: list[tuple[str, str]]) -> None:
    """Seed engine rows the way the conductor would — through chorus, not raw SQL."""
    from chorus.ledger import Ledger
    from chorus.workforce import Employee

    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        for name, role in employees:
            ledger.employees.create(Employee(id=name.lower(), name=name, role=role))
    finally:
        ledger.close()


@pytest.fixture
def provider(database_url: str) -> ControlPlaneProvider:
    return ControlPlaneProvider(engine_dsn=_pg_conninfo(database_url, user="podium_app"))


def test_read_plane_opens_without_model_keys_or_heartbeat(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        assert isinstance(plane, CompanyControlPlane)
        assert plane.company_id == company_id
        assert plane.workspace_id == ws_id
    finally:
        plane.close()


def test_workforce_roster_translates_engine_rows_to_dtos(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    dsn = _pg_conninfo(database_url, user="podium_app")
    _seed_company(dsn, company_id, employees=[("Ada", "backend_engineer"), ("Pia", "pm")])

    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        roster = plane.workforce.roster()
        by_id = {member.id: member for member in roster}
        assert set(by_id) == {"ada", "pia"}
        assert by_id["ada"].role == "backend_engineer"
        assert by_id["ada"].name == "Ada"
        assert by_id["ada"].status == "idle"  # engine default: hired, not yet dispatched
        # DTOs, not engine rows: pydantic models with a stable, serializable shape.
        assert by_id["ada"].model_dump()["id"] == "ada"
    finally:
        plane.close()


def test_read_planes_are_company_isolated(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    """FORCE RLS is the wall: company B's plane sees none of A's workforce."""
    ws_id = uuid.uuid4()
    company_a, company_b = uuid.uuid4(), uuid.uuid4()
    dsn = _pg_conninfo(database_url, user="podium_app")
    _seed_company(dsn, company_a, employees=[("Ada", "backend_engineer")])

    plane_b = provider.read_plane(workspace_id=ws_id, company_id=company_b)
    try:
        assert plane_b.workforce.roster() == []
    finally:
        plane_b.close()


def _seed_work(dsn: str, company_id: uuid.UUID) -> None:
    """A task + a team + a skill — the cold state the CK2 facades read."""
    from chorus.ids import mint_id
    from chorus.ledger import Ledger, Task, Team, TeamMember, TeamMembershipRole
    from chorus.skills import SkillOrigin, SkillStore
    from chorus.workforce import Employee

    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.employees.create(Employee(id="lea", name="Lea", role="pm"))
        ledger.tasks.submit(Task(id=mint_id(), intent="ship the launch page"))
        team_id = mint_id()
        ledger.teams.create(
            Team(id=team_id, name="launch", lead_employee_id="lea", created_by="lea")
        )
        ledger.team_members.add(
            TeamMember(
                team_id=team_id,
                employee_id="ada",
                source_manager_id="lea",
                membership_role=TeamMembershipRole.MEMBER,
            )
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


def test_delegation_facade_reads_teams_and_capacity(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    dsn = _pg_conninfo(database_url, user="podium_app")
    _seed_work(dsn, company_id)

    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        teams = plane.delegation.teams()
        assert len(teams) == 1
        assert teams[0].name == "launch"
        assert teams[0].lead == "lea"
        assert teams[0].members == ["ada"]
        assert teams[0].status == "forming"

        capacity = {entry.role: entry for entry in plane.delegation.capacity()}
        assert set(capacity) == {"backend_engineer", "pm"}
        assert capacity["backend_engineer"].eligible == 1
        assert capacity["backend_engineer"].running == 0
    finally:
        plane.close()


def test_observe_facade_reads_status_and_skills(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    dsn = _pg_conninfo(database_url, user="podium_app")
    _seed_work(dsn, company_id)

    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        status = plane.observe.status()
        assert status.employees == 2
        assert status.open_tasks == 1
        assert status.running_beats == 0

        skills = plane.observe.skills("ada")
        assert [skill.slug for skill in skills] == ["deploy-checklist"]
        assert skills[0].revision_no == 1
        assert skills[0].origin == "created"

        assert plane.observe.skills("lea") == []  # per-employee, not company-wide
    finally:
        plane.close()
