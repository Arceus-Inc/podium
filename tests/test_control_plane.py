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


def test_observe_facade_reads_one_skill_revision_history(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    from chorus.ids import mint_id
    from chorus.ledger import Ledger
    from chorus.skills import SkillOrigin, SkillStore
    from chorus.workforce import Employee

    from podium.control._observe import UnknownSkillError

    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    author_one_id = mint_id()
    author_two_id = mint_id()
    author_three_id = mint_id()
    dsn = _pg_conninfo(database_url, user="podium_app")
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.employees.create(Employee(id="ada", name="Ada", role="backend_engineer"))
        ledger.employees.create(Employee(id="lea", name="Lea", role="pm"))
        store = SkillStore(ledger)
        skill, revision_one = store.create(
            employee_id="ada",
            slug="deploy-checklist",
            name="Deploy checklist",
            description="",
            when_to_use="",
            file_inventory=[],
            origin=SkillOrigin.CREATED,
            action="create",
            label="Initial",
            source_run_ids=("run-one",),
            author_run_id=author_one_id,
        )
        skill_id = skill.id
        store.append_revision(
            skill_id=skill_id,
            file_inventory=[],
            action="patch",
            label="Improve rollback",
            source_run_ids=("run-two", "run-three"),
            author_run_id=author_two_id,
        )
        store.append_revision(
            skill_id=skill_id,
            file_inventory=[],
            action="restore",
            label="Restore initial",
            source_run_ids=("run-four",),
            author_run_id=author_three_id,
            restored_from_revision_id=revision_one.id,
        )
    finally:
        ledger.close()

    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        revisions = plane.observe.skill_revisions("ada", skill_id)
        assert [revision.revision_no for revision in revisions] == [1, 2, 3]
        assert revisions[1].source_run_refs == ("run-two", "run-three")
        assert revisions[1].author_run_ref == author_two_id
        assert revisions[2].restored_from_ref == revision_one.id
        assert all(revision.created_at is not None for revision in revisions)

        with pytest.raises(UnknownSkillError):
            plane.observe.skill_revisions("lea", skill_id)
        with pytest.raises(UnknownSkillError):
            plane.observe.skill_revisions("nobody", skill_id)
        with pytest.raises(UnknownSkillError):
            plane.observe.skill_revisions("ada", "unknown")
    finally:
        plane.close()


def test_direction_facade_reads_the_goal_tree(
    database_url: str, provider: ControlPlaneProvider
) -> None:
    from chorus.ids import mint_id
    from chorus.ledger import Goal, GoalLevel, Ledger

    ws_id, company_id = uuid.uuid4(), uuid.uuid4()
    dsn = _pg_conninfo(database_url, user="podium_app")
    root_id, child_id = mint_id(), mint_id()
    ledger = Ledger.open(dsn, company_id=str(company_id))
    try:
        ledger.goals.create(Goal(id=root_id, title="Win launch week"))
        ledger.goals.create(
            Goal(id=child_id, title="Ship the page", level=GoalLevel.TEAM, parent_id=root_id)
        )
    finally:
        ledger.close()

    plane = provider.read_plane(workspace_id=ws_id, company_id=company_id)
    try:
        tree = plane.direction.goal_tree()
        assert len(tree) == 1  # one root
        root = tree[0]
        assert root.title == "Win launch week"
        assert root.level == "company"
        assert [child.title for child in root.children] == ["Ship the page"]
        assert root.children[0].children == []
    finally:
        plane.close()
