"""The wiring-parity guard: ``company.build()`` must register every execution seam.

The full-spine experiment caught ``Chorus.build`` silently dropping ``memory_writer`` because the
facade and the hand-built example wiring had drifted. This suite pins the blessed graph: a seam
dropped anywhere between the four repos fails HERE, not in a live run.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from company import CompanyConfig, CompanyGraph, build
from company._bridge import ChorusGoalStore, ChorusIntakePort, ChorusOutcomeFeed


def _config(tmp_path: Path, database_url: str) -> CompanyConfig:
    return CompanyConfig(
        api_key="test-key",
        base_url="https://example.invalid/openai/v1",
        deployment="gpt-test",
        workdir=tmp_path,
        company_id=str(uuid4()),
        ledger_dsn=database_url.replace("+asyncpg", "").replace(
            "://postgres@", "://podium_app@"
        ),
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


def test_ceo_factory_carries_governance_and_the_shared_ledger(tmp_path: Path, database_url: str) -> None:
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
