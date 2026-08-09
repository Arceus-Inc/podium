"""CompanyGraph owns Horizon's PostgreSQL repositories and their company-scoped lifecycle."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from horizon.generation import Proposal
from horizon.model import Decision, StrategyRecord
from horizon.store.postgres import (
    PostgresDecisionRepository,
    PostgresProposalRepository,
    PostgresStrategyRepository,
)

from podium.conductor.company import CompanyConfig, build


def _postgres_dsn(database_url: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")


def _config(workdir: Path, database_url: str, company_id: UUID) -> CompanyConfig:
    return CompanyConfig(
        api_key="test-key",
        base_url="https://example.invalid/openai/v1",
        deployment="gpt-test",
        workdir=workdir,
        company_id=company_id,
        ledger_dsn=_postgres_dsn(database_url),
    )


def test_company_graph_reopens_horizon_state_from_postgres(
    database_url: str, tmp_path: Path
) -> None:
    company_id = uuid4()
    config = _config(tmp_path / "company", database_url, company_id)
    decision = Decision(
        id="decision_1",
        statement="Prioritize durable retention",
        status="proposed",
        owner="casey",
        rationale="The renewal cohort has the strongest evidence.",
        goal_ids=["goal_1"],
    )
    strategy = StrategyRecord(
        goal_id="goal_1",
        title="Improve renewal rate",
        score=0.8,
        health="on_track",
        evidence=["Renewal evidence"],
        decision_id=decision.id,
    )
    proposal = Proposal(
        id="proposal_1",
        decision_statement="Improve renewal rate",
        decision_rationale="Evidence supports a focused renewal initiative.",
    )

    first_graph = build(config)
    try:
        assert isinstance(first_graph.horizon._decisions, PostgresDecisionRepository)
        assert isinstance(first_graph.horizon._strategy, PostgresStrategyRepository)
        assert isinstance(first_graph.horizon._proposals, PostgresProposalRepository)
        assert first_graph.horizon._decisions._connection is first_graph.horizon_connection
        assert first_graph.horizon._strategy._connection is first_graph.horizon_connection
        assert first_graph.horizon._proposals._connection is first_graph.horizon_connection

        assert first_graph.horizon.seed_decision(decision) == decision
        assert first_graph.horizon._strategy.put(strategy) == strategy
        assert first_graph.horizon._proposals.put(proposal) == proposal
    finally:
        first_graph.close()

    for filename in ("decisions.json", "strategy.json", "proposals.json"):
        assert not (config.workdir / filename).exists()

    restarted_graph = build(config)
    try:
        assert restarted_graph.horizon._decisions.get(decision.id) == decision
        assert restarted_graph.horizon._strategy.get(strategy.goal_id) == strategy
        assert restarted_graph.horizon._proposals.get(proposal.id) == proposal
    finally:
        restarted_graph.close()


def test_company_graph_horizon_repositories_are_isolated_by_company_rls(
    database_url: str, tmp_path: Path
) -> None:
    graph_a = build(_config(tmp_path / "a", database_url, uuid4()))
    graph_b = build(_config(tmp_path / "b", database_url, uuid4()))
    try:
        decision_a = Decision(id="decision_shared", statement="Company A direction")
        decision_b = Decision(id="decision_shared", statement="Company B direction")
        strategy_a = StrategyRecord(goal_id="goal_shared", title="Company A strategy")
        strategy_b = StrategyRecord(goal_id="goal_shared", title="Company B strategy")
        proposal_a = Proposal(id="proposal_shared", decision_statement="Company A proposal")
        proposal_b = Proposal(id="proposal_shared", decision_statement="Company B proposal")

        graph_a.horizon.seed_decision(decision_a)
        graph_a.horizon._strategy.put(strategy_a)
        graph_a.horizon._proposals.put(proposal_a)
        graph_b.horizon.seed_decision(decision_b)
        graph_b.horizon._strategy.put(strategy_b)
        graph_b.horizon._proposals.put(proposal_b)

        assert graph_a.horizon._decisions.get(decision_a.id) == decision_a
        assert graph_b.horizon._decisions.get(decision_b.id) == decision_b
        assert graph_a.horizon._decisions.all() == [decision_a]
        assert graph_b.horizon._decisions.all() == [decision_b]
        assert graph_a.horizon._strategy.get(strategy_a.goal_id) == strategy_a
        assert graph_b.horizon._strategy.get(strategy_b.goal_id) == strategy_b
        assert graph_a.horizon._strategy.all() == [strategy_a]
        assert graph_b.horizon._strategy.all() == [strategy_b]
        assert graph_a.horizon._proposals.get(proposal_a.id) == proposal_a
        assert graph_b.horizon._proposals.get(proposal_b.id) == proposal_b
        assert graph_a.horizon._proposals.all() == [proposal_a]
        assert graph_b.horizon._proposals.all() == [proposal_b]
    finally:
        graph_a.close()
        graph_b.close()
