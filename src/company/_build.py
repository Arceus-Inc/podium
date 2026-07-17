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
from chorus.ledger import Ledger, SqliteLedger
from chorus.ledger.postgres import PostgresLedger
from chorus.roles import RolePlugin, RoleRegistry, default_roles
from chorus_employee import default_landers
from chorus_harness import EmployeeHarnessFactory
from horizon import Horizon
from horizon.generation import ProposalStore
from horizon.governance import HorizonGovernance
from horizon.store import DecisionStore, StrategyStore

from company._bridge import ChorusGoalStore, ChorusIntakePort, ChorusOutcomeFeed


@dataclass(frozen=True)
class CompanyConfig:
    """Everything the composition root needs; all state lands under ``workdir``."""

    api_key: str
    base_url: str
    deployment: str
    workdir: Path
    company_id: str = "company"
    roles: Sequence[RolePlugin] | None = None  # None -> chorus default_roles()
    seed: Path | None = None  # seed repo copied into each worker worktree
    beat_timeout_s: float = 600.0
    max_concurrent_runs: int = 3
    default_assignee: str | None = None
    # M5: when set, the ledger is chorus's PostgresLedger on this DSN, scoped to `company_id` by
    # FORCE RLS (company_id must be canonical uuid text). None -> the SQLite file under workdir.
    ledger_dsn: str | None = None


@dataclass(frozen=True)
class CompanyGraph:
    """The wired object graph — the consumer hires, submits, and runs through these."""

    org: Chorus
    horizon: Horizon
    governance: HorizonGovernance
    factory: EmployeeHarnessFactory  # worker harnesses (worktrees, tools, skills)
    ceo_factory: EmployeeHarnessFactory  # governance-wired CEO harness


def _open_ledger(config: CompanyConfig) -> Ledger:
    """One ledger per company: Postgres (shared DB, RLS-scoped) when `ledger_dsn` is set."""
    if config.ledger_dsn is None:
        return SqliteLedger.open(str(config.workdir / "ledger.db"))
    try:
        uuid.UUID(config.company_id)
    except ValueError as exc:
        # The RLS policies cast the session GUC to uuid — fail here, at build, not mid-query.
        raise ValueError(
            f"company_id must be canonical uuid text for a Postgres ledger, "
            f"got {config.company_id!r}"
        ) from exc
    return PostgresLedger.open(config.ledger_dsn, company_id=config.company_id)


def build(config: CompanyConfig) -> CompanyGraph:
    """Assemble the full company graph over ONE ledger under ``config.workdir``."""
    config.workdir.mkdir(parents=True, exist_ok=True)
    plugins = list(config.roles) if config.roles is not None else list(default_roles())
    registry = RoleRegistry.from_plugins(plugins)
    ledger = _open_ledger(config)

    factory = EmployeeHarnessFactory(
        api_key=config.api_key,
        base_url=config.base_url,
        deployment=config.deployment,
        company_id=config.company_id,
        roles=registry,
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
        company_id=config.company_id,
        caps=Caps(max_concurrent_runs=config.max_concurrent_runs),
    )

    horizon = Horizon(
        goals=ChorusGoalStore(org),
        intake=ChorusIntakePort(org),
        delegated_intake=DelegatedIntakeAdapter(org, ledger, company_id=config.company_id),
        capacity=CapacityAdapter(ledger, company_id=config.company_id),
        outcomes=ChorusOutcomeFeed(org),
        reasoner=None,
        decisions=DecisionStore(config.workdir / "decisions.json"),
        strategy=StrategyStore(config.workdir / "strategy.json"),
        proposals=ProposalStore(config.workdir / "proposals.json"),
        default_assignee=config.default_assignee,
    )
    governance = HorizonGovernance(horizon)
    ceo_factory = EmployeeHarnessFactory(
        api_key=config.api_key,
        base_url=config.base_url,
        deployment=config.deployment,
        company_id=config.company_id,
        roles=registry,
        ledger=ledger,
        governance=governance,
        work_root=config.workdir / "work",
        timeout_s=config.beat_timeout_s,
    )
    return CompanyGraph(
        org=org, horizon=horizon, governance=governance, factory=factory, ceo_factory=ceo_factory
    )
