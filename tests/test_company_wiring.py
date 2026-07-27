"""The wiring-parity guard: ``company.build()`` must register every execution seam.

The full-spine experiment caught ``Chorus.build`` silently dropping ``memory_writer`` because the
facade and the hand-built example wiring had drifted. This suite pins the blessed graph: a seam
dropped anywhere between the four repos fails HERE, not in a live run.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from podium.conductor.company import CompanyConfig, CompanyGraph, build
from podium.conductor.company._bridge import ChorusGoalStore, ChorusIntakePort, ChorusOutcomeFeed


def _pg_conninfo(database_url: str, *, user: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", f"://{user}@")


def _config(tmp_path: Path, database_url: str) -> CompanyConfig:
    return CompanyConfig(
        api_key="test-key",
        base_url="https://example.invalid/openai/v1",
        deployment="gpt-test",
        workdir=tmp_path,
        company_id=str(uuid4()),
        ledger_dsn=database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@"),
    )


def test_build_returns_a_fully_wired_graph(tmp_path: Path, database_url: str) -> None:
    graph = build(_config(tmp_path, database_url))

    assert isinstance(graph, CompanyGraph)
    assert graph.governance is not None


def test_scheduler_registers_every_execution_seam(tmp_path: Path, database_url: str) -> None:
    """The memory_writer regression pin — plus every other seam the kernel executes through."""
    scheduler = build(_config(tmp_path, database_url)).org._scheduler

    required = (
        "_memory_writer",  # episodic capture (the seam Chorus.build once dropped)
        "_beat_runner_for",  # per-employee dream harness resolution
        "_landers",  # deliverable landing
        "_ledger",
        "_workforce",
        "_roles",  # intake DoD
        "_event_bus",  # telemetry
        "_company_root",  # lattice beat-end gate
    )
    missing = [name for name in required if getattr(scheduler, name) is None]
    assert not missing, f"Chorus.build dropped execution seams: {missing}"


def test_horizon_ports_are_bound_to_chorus_adapters(tmp_path: Path, database_url: str) -> None:
    graph = build(_config(tmp_path, database_url))

    assert isinstance(graph.horizon._goals, ChorusGoalStore)
    assert isinstance(graph.horizon._intake, ChorusIntakePort)
    assert isinstance(graph.horizon._outcomes, ChorusOutcomeFeed)
    # M8 delegation: team-shaped goals need the delegated door and the capacity read.
    assert graph.horizon._delegated_submitter is not None
    assert graph.horizon._capacity is not None


def test_ceo_factory_carries_governance_and_the_shared_ledger(
    tmp_path: Path, database_url: str
) -> None:
    graph = build(_config(tmp_path, database_url))

    assert graph.ceo_factory._governance is graph.governance
    # ONE ledger: a reviewer's verdict and the factory's capability tools land in the same store.
    assert graph.ceo_factory._ledger is graph.org._ledger
    assert graph.factory._ledger is graph.org._ledger


def test_intake_submit_lands_in_the_shared_ledger(tmp_path: Path, database_url: str) -> None:
    graph = build(_config(tmp_path, database_url))
    employee = graph.org.hire(name="Bex", role="backend_engineer")

    task_id = graph.horizon._intake.submit(
        "Ship the walking skeleton",
        assignee=employee.id,
        priority="high",
        origin_fingerprint="fp-1",
    )

    assert graph.org._ledger.tasks.get(task_id) is not None
    # Idempotent intake: the same fingerprint returns the same task, not a duplicate.
    assert (
        graph.horizon._intake.submit(
            "Ship the walking skeleton", assignee=employee.id, origin_fingerprint="fp-1"
        )
        == task_id
    )


def test_build_wires_token_pricing_into_both_factories(tmp_path: Path, database_url: str) -> None:
    """Live e2e finding: llm.call events carried cost_usd but cost_event stayed empty — beats
    priced nothing because podium never wired TokenPricing. Both factories must price spend."""
    from uuid import uuid4

    graph = build(
        CompanyConfig(
            api_key="k",
            base_url="https://x/openai/v1",
            deployment="gpt-x",
            workdir=tmp_path,
            company_id=str(uuid4()),
            ledger_dsn=_pg_conninfo(database_url, user="podium_app"),
        )
    )
    try:
        assert graph.factory._pricing is not None
        assert graph.ceo_factory._pricing is not None
        assert graph.factory._pricing.rate_for("any-model") is not None  # default rate prices all
    finally:
        graph.close()


def test_horizon_gets_a_live_reasoner(tmp_path: Path, database_url: str) -> None:
    """Activation 2026-07-18: the direction engine ran with reasoner=None since CP-1 —
    ~1.2k lines of generation/planning inert. build() now wires the same chat substrate
    the beats use, so decompose/generate no longer raise 'built without a reasoner'."""
    graph = build(_config(tmp_path, database_url))
    scout = getattr(graph.horizon, "_scout", None)
    assert scout is not None  # generation lights up only when a reasoner is present


def test_seed_horizon_direction_gives_a_live_decision_goal_and_report(
    tmp_path: Path, database_url: str
) -> None:
    """F2: podium now drives horizon. Seeding the founder objective's root goal gives horizon a live
    decision + adopted goal (chorus stays the source of truth — no duplicate goal), so its outcome
    listener has a record to fold into and its direction report populates (both were empty before)."""
    from podium.conductor._chorus_executor import _ensure_root_goal, _seed_horizon_direction

    graph = build(_config(tmp_path, database_url))
    try:
        objective = "Build an AI note-taker for professionals. It must sync across devices."
        root_goal_id = _ensure_root_goal(graph.org._ledger, objective)
        assert root_goal_id is not None

        # The F2 gap: before podium drives it, horizon is inert — no decisions, blank report.
        assert graph.horizon.state() == []

        _seed_horizon_direction(graph, root_goal_id=root_goal_id, objective=objective)

        state = graph.horizon.state()
        assert len(state) == 1
        assert [g.id for g in state[0].goals] == [root_goal_id]
        assert state[0].decision.statement == "Build an AI note-taker for professionals."
        assert "Build an AI note-taker for professionals." in graph.horizon.report()
        # No duplication — chorus still holds exactly the one root goal it authored.
        assert len(graph.org._ledger.goals.children(None)) == 1

        # Idempotent across runs — a second call mints neither a duplicate decision nor goal edge.
        _seed_horizon_direction(graph, root_goal_id=root_goal_id, objective=objective)
        assert len(graph.horizon.state()) == 1
        assert [g.id for g in graph.horizon.state()[0].goals] == [root_goal_id]
    finally:
        graph.close()
