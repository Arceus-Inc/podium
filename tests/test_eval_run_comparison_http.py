"""GET eval-run comparisons only reads pinned Chorus evaluation records."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from chorus.ids import mint_id
from chorus.ledger import (
    AgentConfigRevision,
    AgentConfigRevisionRef,
    AgentIdentity,
    AgentsMdReference,
    Artifact,
    ArtifactRevision,
    ArtifactType,
    EffectiveToolPin,
    EvalCase,
    EvalInputSnapshot,
    EvalOutputSnapshot,
    EvalRun,
    EvalRunStatus,
    EvalRunUsage,
    EvalSuite,
    Ledger,
    ProviderModelConfig,
    Run,
    RunStatus,
    SandboxProfile,
    Skill,
    SkillOrigin,
    SkillRevision,
    SkillRevisionPin,
    Task,
)
from chorus.workforce import Employee
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from podium.auth import create_api_key
from podium.companies import create_company
from podium.control import ControlPlaneProvider
from podium.evaluations import EvalRunComparisonFacade
from podium.main import create_app
from podium.workspaces import create_workspace

_STARTED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def api(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    database_url: str,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(engine_dsn=_dsn(database_url))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_company(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, str]:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name="Eval workspace", slug="eval-workspace")
        company = await create_company(
            session, workspace_id=workspace.id, slug="eval-company", name="Eval Company"
        )
        _, token = await create_api_key(session, workspace_id=workspace.id, name="eval-reader")
    return company.id, token


def _dsn(database_url: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")


def _create_artifact_revision(ledger: Ledger, suffix: str) -> ArtifactRevision:
    employee_id = f"artifact-author-{suffix}"
    ledger.employees.create(Employee(id=employee_id, name="Artifact Author", role="engineer"))
    task = ledger.tasks.submit(Task(id=mint_id(), intent=f"evaluation evidence {suffix}"))
    run = ledger.runs.create(
        Run(id=mint_id(), employee_id=employee_id, task_id=task.id, status=RunStatus.SUCCEEDED)
    )
    artifact = ledger.artifacts.create(
        Artifact(id=mint_id(), task_id=task.id, type=ArtifactType.DOC)
    )
    return ledger.artifact_revisions.record(
        ArtifactRevision(
            id=mint_id(),
            artifact_id=artifact.id,
            resource_ref={"kind": "eval-evidence", "uri": f"artifact://{suffix}"},
            summary=f"Pinned evidence for {suffix}",
            created_by_run_id=run.id,
        )
    )


def _create_suite(ledger: Ledger, suffix: str) -> tuple[EvalSuite, SkillRevision]:
    employee_id = f"eval-agent-{suffix}"
    ledger.employees.create(Employee(id=employee_id, name="Eval Agent", role="engineer"))
    skill = ledger.skills.insert(
        Skill(
            id=mint_id(),
            employee_id=employee_id,
            slug=f"eval-skill-{suffix}",
            name="Eval skill",
            origin=SkillOrigin.CREATED,
        )
    )
    revision = ledger.skill_revisions.append(
        SkillRevision(
            id=mint_id(),
            skill_id=skill.id,
            revision_no=1,
            action="create",
            file_inventory=(
                '[{"path":"SKILL.md","kind":"file","content":"# Pinned eval skill\\n"}]'
            ),
            content_hash=f"hash-{suffix}",
            label="Pinned eval revision",
            source_run_ids=(mint_id(),),
            author_run_id=mint_id(),
        )
    )
    case = ledger.eval_cases.create(
        EvalCase(
            id=mint_id(),
            skill_revision_id=revision.id,
            name="Pinned comparison case",
            input_text="Evaluate the pinned behavior.",
            expected_behavior="Return the expected response.",
        )
    )
    suite = ledger.eval_suites.create(
        EvalSuite(id=mint_id(), skill_revision_id=revision.id, case_ids=(case.id,))
    )
    return suite, revision


def _create_run(
    ledger: Ledger,
    *,
    suite: EvalSuite,
    revision: SkillRevision,
    suffix: str,
    model: str,
    output: str,
    usage: EvalRunUsage,
    status: EvalRunStatus,
    completed_at: datetime,
) -> EvalRun:
    config = ledger.agent_config_revisions.create(
        AgentConfigRevision(
            id=f"eval-config-{suffix}",
            agent=AgentIdentity(f"eval-agent-config-{suffix}"),
            revision_no=1,
            agents_md=AgentsMdReference("agents-md@1", "pinned instructions"),
            provider_model=ProviderModelConfig("anthropic", model),
            sandbox_profile=SandboxProfile("workspace-write"),
            skill_pins=(SkillRevisionPin(revision.id),),
            tool_pins=(
                EffectiveToolPin("shell", "builtin:schema@3"),
                EffectiveToolPin("search", "plugin:exa@2"),
            ),
        )
    )
    artifact_revision = _create_artifact_revision(ledger, suffix)
    return ledger.eval_runs.create(
        EvalRun(
            id=mint_id(),
            eval_suite_id=suite.id,
            skill_revision_id=revision.id,
            agent_config_revision=AgentConfigRevisionRef(config.id),
            provider="anthropic",
            model=model,
            input_snapshot=EvalInputSnapshot("Pinned evaluation input."),
            output_snapshot=EvalOutputSnapshot(output),
            usage=usage,
            artifact_revision_ids=(artifact_revision.id,),
            status=status,
            started_at=_STARTED_AT,
            completed_at=completed_at,
        )
    )


def _create_comparable_runs(database_url: str, company_id: uuid.UUID) -> tuple[EvalRun, EvalRun]:
    ledger = Ledger.open(_dsn(database_url), company_id=str(company_id))
    try:
        suite, revision = _create_suite(ledger, "comparison")
        baseline = _create_run(
            ledger,
            suite=suite,
            revision=revision,
            suffix="baseline",
            model="claude-sonnet",
            output="Baseline output.",
            usage=EvalRunUsage(12, 34, Decimal("0.005001")),
            status=EvalRunStatus.COMPLETED,
            completed_at=_STARTED_AT + timedelta(minutes=1),
        )
        candidate = _create_run(
            ledger,
            suite=suite,
            revision=revision,
            suffix="candidate",
            model="claude-opus",
            output="Candidate output.",
            usage=EvalRunUsage(10, 40, Decimal("0.005700")),
            status=EvalRunStatus.FAILED,
            completed_at=_STARTED_AT + timedelta(minutes=1, seconds=30),
        )
        return baseline, candidate
    finally:
        ledger.close()


async def test_compares_two_visible_pinned_eval_runs(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    company_id, token = await _seed_company(sessionmaker)
    baseline, candidate = _create_comparable_runs(database_url, company_id)

    response = await api.get(
        f"/v1/companies/{company_id}/eval-runs/compare",
        params={"baseline_run_id": baseline.id, "candidate_run_id": candidate.id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["baseline"]["id"] == baseline.id
    assert body["baseline"]["eval_suite_id"] == baseline.eval_suite_id
    eval_case = body["baseline"]["eval_cases"][0]
    assert eval_case["id"]
    assert eval_case["skill_revision_id"] == baseline.skill_revision_id
    assert eval_case["name"] == "Pinned comparison case"
    assert eval_case["input_text"] == "Evaluate the pinned behavior."
    assert eval_case["expected_behavior"] == "Return the expected response."
    assert eval_case["created_at"]

    skill_revision = body["baseline"]["skill_revision"]
    assert skill_revision["id"] == baseline.skill_revision_id
    assert skill_revision["skill_id"]
    assert skill_revision["revision_no"] == 1
    assert skill_revision["action"] == "create"
    assert skill_revision["file_inventory"] == (
        '[{"path":"SKILL.md","kind":"file","content":"# Pinned eval skill\\n"}]'
    )
    assert skill_revision["content_hash"] == "hash-comparison"
    assert skill_revision["label"] == "Pinned eval revision"
    assert skill_revision["source_run_ids"]
    assert skill_revision["author_run_id"]
    assert skill_revision["restored_from_revision_id"] is None
    assert skill_revision["created_at"]

    agent_config = body["baseline"]["agent_config_revision"]
    assert agent_config["id"] == baseline.agent_config_revision.value
    assert agent_config["agent_id"] == "eval-agent-config-baseline"
    assert agent_config["revision_no"] == 1
    assert agent_config["agents_md_revision"] == "agents-md@1"
    assert agent_config["agents_md_content"] == "pinned instructions"
    assert agent_config["provider"] == "anthropic"
    assert agent_config["model"] == "claude-sonnet"
    assert agent_config["sandbox_profile"] == "workspace-write"
    assert agent_config["skill_pins"] == [{"skill_revision_id": baseline.skill_revision_id}]
    assert agent_config["tool_pins"] == [
        {"identifier": "shell", "provenance": "builtin:schema@3"},
        {"identifier": "search", "provenance": "plugin:exa@2"},
    ]
    assert agent_config["created_at"]

    assert body["baseline"]["model"] == "claude-sonnet"
    assert body["baseline"]["output_snapshot"] == "Baseline output."
    assert body["baseline"]["cost_usd"] == "0.005001"
    assert body["baseline"]["status"] == "completed"
    assert body["baseline"]["created_at"]
    artifact_revision = body["baseline"]["artifact_revisions"][0]
    assert artifact_revision["id"] == baseline.artifact_revision_ids[0]
    assert artifact_revision["artifact_id"]
    assert artifact_revision["revision"] == 1
    assert artifact_revision["resource_ref"] == {
        "kind": "eval-evidence",
        "uri": "artifact://baseline",
    }
    assert artifact_revision["summary"] == "Pinned evidence for baseline"
    assert artifact_revision["created_by_run_id"]
    assert artifact_revision["created_at"]
    assert body["candidate"]["id"] == candidate.id
    assert body["candidate"]["model"] == "claude-opus"
    assert body["candidate"]["output_snapshot"] == "Candidate output."
    assert body["candidate"]["status"] == "failed"
    assert body["deltas"] == {
        "input_tokens": -2,
        "output_tokens": 6,
        "cost_usd": "0.000699",
        "duration_ms": 30000,
    }


async def test_rejects_runs_from_different_suites(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    company_id, token = await _seed_company(sessionmaker)
    baseline, _candidate = _create_comparable_runs(database_url, company_id)
    ledger = Ledger.open(_dsn(database_url), company_id=str(company_id))
    try:
        other_suite, other_revision = _create_suite(ledger, "incompatible")
        incompatible = _create_run(
            ledger,
            suite=other_suite,
            revision=other_revision,
            suffix="incompatible",
            model="claude-sonnet",
            output="Different suite output.",
            usage=EvalRunUsage(1, 1, Decimal("0.000001")),
            status=EvalRunStatus.COMPLETED,
            completed_at=_STARTED_AT + timedelta(seconds=1),
        )
    finally:
        ledger.close()

    response = await api.get(
        f"/v1/companies/{company_id}/eval-runs/compare",
        params={"baseline_run_id": baseline.id, "candidate_run_id": incompatible.id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "eval runs are not comparable"


async def test_hides_malformed_and_missing_eval_run_ids(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    company_id, token = await _seed_company(sessionmaker)
    baseline, candidate = _create_comparable_runs(database_url, company_id)
    url = f"/v1/companies/{company_id}/eval-runs/compare"
    headers = {"Authorization": f"Bearer {token}"}

    for baseline_run_id, candidate_run_id in (
        ("not-a-uuid", candidate.id),
        (str(uuid.uuid4()), baseline.id),
    ):
        response = await api.get(
            url,
            params={
                "baseline_run_id": baseline_run_id,
                "candidate_run_id": candidate_run_id,
            },
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "eval run not found"

    missing_parameter = await api.get(
        url,
        params={"candidate_run_id": candidate.id},
        headers=headers,
    )
    assert missing_parameter.status_code == 404
    assert missing_parameter.json()["error"]["message"] == "eval run not found"


async def test_hides_other_company_eval_runs(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    company_id, _token = await _seed_company(sessionmaker)
    baseline, candidate = _create_comparable_runs(database_url, company_id)
    async with sessionmaker() as session, session.begin():
        other_workspace = await create_workspace(
            session, name="Other workspace", slug="other-workspace"
        )
        _, other_token = await create_api_key(
            session, workspace_id=other_workspace.id, name="other"
        )

    response = await api.get(
        f"/v1/companies/{company_id}/eval-runs/compare",
        params={"baseline_run_id": baseline.id, "candidate_run_id": candidate.id},
        headers={"Authorization": f"Bearer {other_token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "eval run not found"


async def test_hides_malformed_company_id(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    company_id, token = await _seed_company(sessionmaker)
    baseline, candidate = _create_comparable_runs(database_url, company_id)

    response = await api.get(
        "/v1/companies/not-a-uuid/eval-runs/compare",
        params={"baseline_run_id": baseline.id, "candidate_run_id": candidate.id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "eval run not found"


async def test_missing_pinned_record_is_a_persisted_invariant_failure(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, _token = await _seed_company(sessionmaker)
    baseline, candidate = _create_comparable_runs(database_url, company_id)
    ledger = Ledger.open(_dsn(database_url), company_id=str(company_id))
    try:
        monkeypatch.setattr(ledger.artifact_revisions, "get", lambda _revision_id: None)
        with pytest.raises(RuntimeError, match="missing artifact revision"):
            EvalRunComparisonFacade(ledger).compare(
                baseline_run_id=baseline.id, candidate_run_id=candidate.id
            )
    finally:
        ledger.close()
