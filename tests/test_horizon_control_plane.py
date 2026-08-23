"""The API read plane independently reads conductor-written Horizon PostgreSQL state."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from horizon.generation import CandidateGoal, DirectionBrief
from starlette.concurrency import run_in_threadpool

from podium.conductor.company import CompanyConfig, build
from podium.control import ControlPlaneProvider
from podium.control._direction import DecisionView, ProposalView, StrategyView

pytestmark = pytest.mark.anyio


def _postgres_dsn(database_url: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")


def _write_direction_state(database_url: str, workdir: Path, company_id: UUID) -> tuple[str, str, str]:
    """Use the conductor composition root, then close it before the API plane opens."""
    graph = build(
        CompanyConfig(
            api_key="test-key",
            base_url="https://example.invalid/openai/v1",
            deployment="gpt-test",
            workdir=workdir,
            company_id=company_id,
            ledger_dsn=_postgres_dsn(database_url),
        )
    )
    try:
        decision = graph.horizon.propose_roadmap(
            "Make renewal revenue predictable",
            [
                {
                    "title": "Improve renewal playbooks",
                    "metric": "Renewal rate",
                    "target": "90%",
                    "score": 0.8,
                }
            ],
            rationale="Retention is the strongest durable growth signal.",
        )
        proposal = graph.horizon.reconcile(
            [
                DirectionBrief(
                    candidate_id="candidate_1",
                    recommendation="Instrument churn risk before renewal",
                    rationale="Early signals make renewal playbooks measurable.",
                    confidence=0.9,
                    risks=["Low coverage"],
                    candidate_goals=[
                        CandidateGoal(
                            title="Ship churn-risk instrumentation",
                            metric="Coverage",
                            target="95%",
                            rationale="Measure the leading indicator.",
                            score=0.7,
                        )
                    ],
                    evidence_refs=["evidence_1"],
                )
            ]
        )[0]
        return decision.id, decision.goal_ids[0], proposal.id
    finally:
        graph.close()


def _read_direction(
    provider: ControlPlaneProvider, workspace_id: UUID, company_id: UUID
) -> tuple[list[DecisionView], list[StrategyView], list[ProposalView]]:
    """Mirror the router's open/read/close worker-thread pattern without adding an HTTP door."""
    plane = provider.read_plane(workspace_id=workspace_id, company_id=company_id)
    try:
        return plane.direction.decisions(), plane.direction.strategies(), plane.direction.proposals()
    finally:
        plane.close()


async def test_api_plane_reads_restart_persistent_horizon_state_with_company_rls(
    database_url: str, tmp_path: Path
) -> None:
    workspace_id, company_a, company_b = uuid4(), uuid4(), uuid4()
    decision_id, goal_id, proposal_id = _write_direction_state(
        database_url, tmp_path / "conductor", company_a
    )
    provider = ControlPlaneProvider(engine_dsn=_postgres_dsn(database_url))

    decisions, strategies, proposals = await run_in_threadpool(
        _read_direction, provider, workspace_id, company_a
    )
    assert decisions == [
        DecisionView(
            id=decision_id,
            statement="Make renewal revenue predictable",
            status="proposed",
            owner=None,
            rationale="Retention is the strongest durable growth signal.",
            goal_ids=(goal_id,),
        )
    ]
    assert len(strategies) == 1
    assert strategies[0].goal_id == goal_id
    assert strategies[0].title == "Improve renewal playbooks"
    assert strategies[0].score == 0.8
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.id == proposal_id
    assert proposal.status == "proposed"
    assert proposal.decision_statement == "Instrument churn risk before renewal"
    assert proposal.decided_by is None
    assert proposal.decided_at is None
    assert proposal.linked_decision_id is None
    assert proposal.brief is not None
    assert proposal.brief.candidate_goals[0].title == "Ship churn-risk instrumentation"

    other_decisions, other_strategies, other_proposals = await run_in_threadpool(
        _read_direction, provider, workspace_id, company_b
    )
    assert other_decisions == []
    assert other_strategies == []
    assert other_proposals == []
