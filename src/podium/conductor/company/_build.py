"""``company.build`` — the blessed assembly of dream + chorus + lattice + horizon.

Every seam the example scripts used to hand-roll (and drift on) is wired here exactly once:
one shared ledger, the harness factory as the per-employee beat resolver, landers, episodic
memory, the governance port for the CEO, and the M8 delegation/capacity ports for team-shaped
goals. ``tests/test_company_wiring.py`` pins the graph.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dream
from chorus.adapters import CapacityAdapter, DelegatedIntakeAdapter
from chorus.facade import Caps, Chorus
from chorus.ledger import Ledger
from chorus.roles import RolePlugin, RoleRegistry, default_roles
from chorus_cli._beats import default_pricing_from_env
from chorus_employee import default_landers
from chorus_harness import EmployeeHarnessFactory
from dream.api.openai import OpenAIChatSubstrate
from horizon import Horizon
from horizon.governance import HorizonGovernance
from horizon.store.postgres import (
    PostgresDecisionRepository,
    PostgresProposalRepository,
    PostgresStrategyRepository,
    open_postgres_connection,
)
from psycopg import Connection

from podium.conductor.company._bridge import ChorusGoalStore, ChorusIntakePort, ChorusOutcomeFeed


@dataclass(frozen=True)
class CompanyConfig:
    """Everything the composition root needs; all state lands under ``workdir``."""

    api_key: str
    base_url: str
    deployment: str
    workdir: Path
    company_id: uuid.UUID
    roles: Sequence[RolePlugin] | None = None  # None -> chorus default_roles()
    seed: Path | None = None  # seed repo copied into each worker worktree
    beat_timeout_s: float = 600.0
    max_concurrent_runs: int = 3
    default_assignee: str | None = None
    # The engine store: chorus's Postgres ledger on this DSN, scoped to `company_id` by FORCE RLS
    # (company_id must be canonical uuid text). SQLite is retired — a DSN is always required.
    ledger_dsn: str = ""


@dataclass(frozen=True)
class CompanyGraph:
    """The wired object graph — the consumer hires, submits, and runs through these."""

    org: Chorus
    horizon: Horizon
    governance: HorizonGovernance
    factory: EmployeeHarnessFactory  # worker harnesses (worktrees, tools, skills)
    ceo_factory: EmployeeHarnessFactory  # governance-wired CEO harness
    horizon_connection: Connection[tuple[object, ...]]

    def close(self) -> None:
        """Release the company's owned Horizon and Chorus Postgres connections."""
        try:
            self.horizon_connection.close()
        finally:
            self.org._ledger.close()


def _open_ledger(config: CompanyConfig) -> Ledger:
    """One Postgres ledger per company, RLS-scoped (SQLite is retired)."""
    if not config.ledger_dsn:
        raise ValueError("ledger_dsn is required — the engine store is Postgres-only")
    if not isinstance(config.company_id, uuid.UUID):
        raise TypeError("company_id must be a UUID")
    return Ledger.open(config.ledger_dsn, company_id=str(config.company_id))


def build(config: CompanyConfig) -> CompanyGraph:
    """Assemble the full company graph over ONE ledger under ``config.workdir``."""
    config.workdir.mkdir(parents=True, exist_ok=True)
    plugins = list(config.roles) if config.roles is not None else list(default_roles())
    registry = RoleRegistry.from_plugins(plugins)
    ledger = _open_ledger(config)
    # Spend is priced at the beat seam (spec 04 §3): without a TokenPricing every beat reports
    # cost_cents=0 and the priced ledger stays empty (found by the live e2e — llm.call events
    # carried cost while cost_event had none). Env-tunable default rates price every model.
    pricing = default_pricing_from_env()

    horizon_connection: Connection[tuple[object, ...]] | None = None
    try:
        chorus_company_id = str(config.company_id)
        factory = EmployeeHarnessFactory(
            api_key=config.api_key,
            base_url=config.base_url,
            deployment=config.deployment,
            company_id=chorus_company_id,
            roles=registry,
            pricing=pricing,
            seed=config.seed,
            ledger=ledger,
            work_root=config.workdir / "work",
            timeout_s=config.beat_timeout_s,
        )
        org = Chorus.build(
            ledger=ledger,
            org_repo=str(config.workdir / "org"),
            memory_repo=str(factory.company_root / "memory"),
            dream=dream,
            beat_runner_for=factory,
            landers=default_landers(factory.company_root, ledger=ledger),
            roles=plugins,
            company_id=chorus_company_id,
            caps=Caps(max_concurrent_runs=config.max_concurrent_runs),
        )

        # The direction engine runs on the same substrate the beats use — horizon's Reasoner
        # protocol is one `complete()` method dream's chat substrate satisfies structurally.
        # (Activated 2026-07-18: this was `reasoner=None` since CP-1, leaving ~1.2k lines of
        # generation/planning inert — the company had execution but no self-directed direction.)
        reasoner = OpenAIChatSubstrate(
            name="horizon-reasoner",
            api_key=config.api_key,
            model=config.deployment,
            base_url=config.base_url,
        )
        horizon_connection = open_postgres_connection(
            config.ledger_dsn, company_id=config.company_id
        )
        horizon = Horizon(
            goals=ChorusGoalStore(org),
            intake=ChorusIntakePort(org),
            delegated_intake=DelegatedIntakeAdapter(
                org, ledger, company_id=chorus_company_id
            ),
            capacity=CapacityAdapter(ledger, company_id=chorus_company_id),
            outcomes=ChorusOutcomeFeed(org),
            reasoner=reasoner,
            model=config.deployment,
            decisions=PostgresDecisionRepository(horizon_connection),
            strategy=PostgresStrategyRepository(horizon_connection),
            proposals=PostgresProposalRepository(horizon_connection),
            default_assignee=config.default_assignee,
        )
        governance = HorizonGovernance(horizon)
        # Bind the governance seam onto the factory that ACTUALLY runs the beats (`beat_runner_for` above).
        # horizon/governance need the org (hence this factory) to exist first, so the port is bound here,
        # after construction. Without this the CEO's beats run on a port-less factory and its governance
        # tools (governance_read / roadmap_propose / proposal_*) are dropped fail-closed — the CEO could
        # propose a workforce but never author or steer direction (horizon's engine sat inert).
        factory.bind_governance(governance)
        ceo_factory = EmployeeHarnessFactory(
            api_key=config.api_key,
            base_url=config.base_url,
            deployment=config.deployment,
            company_id=chorus_company_id,
            roles=registry,
            pricing=pricing,
            ledger=ledger,
            governance=governance,
            work_root=config.workdir / "work",
            timeout_s=config.beat_timeout_s,
        )
        return CompanyGraph(
            org=org,
            horizon=horizon,
            governance=governance,
            factory=factory,
            ceo_factory=ceo_factory,
            horizon_connection=horizon_connection,
        )
    except BaseException:
        if horizon_connection is not None:
            horizon_connection.close()
        ledger.close()
        raise
