"""Fault-injection proofs for the two owners of engine database connections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from chorus.facade import Chorus
from chorus.ledger import Ledger
from chorus_harness import EmployeeHarnessFactory
from horizon import Horizon
from horizon.governance import HorizonGovernance
from psycopg import Connection

from podium.conductor.company import _build as company_build
from podium.control import _plane as control_plane
from podium.control._direction import DirectionFacade


@dataclass
class _CloseProbe:
    name: str
    calls: list[str]
    failure: BaseException | None = None

    def close(self) -> None:
        self.calls.append(self.name)
        if self.failure is not None:
            raise self.failure


@dataclass
class _OrgProbe:
    _ledger: Ledger


class _FactoryProbe:
    def __init__(self, company_root: Path) -> None:
        self.company_root = company_root

    def bind_governance(self, _governance: HorizonGovernance) -> None:
        return None


def _config(tmp_path: Path) -> company_build.CompanyConfig:
    return company_build.CompanyConfig(
        api_key="test-key",
        base_url="https://example.invalid/openai/v1",
        deployment="gpt-test",
        workdir=tmp_path,
        company_id=uuid.uuid4(),
        ledger_dsn="postgresql://example.invalid/podium",
    )


@pytest.mark.parametrize("failure_point", ["pricing", "factory"])
def test_company_build_closes_ledger_before_horizon_connection(
    tmp_path: Path, failure_point: str
) -> None:
    calls: list[str] = []
    ledger = _CloseProbe("ledger", calls)
    primary = RuntimeError(f"{failure_point} failed")

    pricing = (
        patch.object(company_build, "default_pricing_from_env", side_effect=primary)
        if failure_point == "pricing"
        else patch.object(company_build, "default_pricing_from_env", return_value=object())
    )
    factory = (
        patch.object(company_build, "EmployeeHarnessFactory", side_effect=primary)
        if failure_point == "factory"
        else patch.object(company_build, "EmployeeHarnessFactory")
    )
    with (
        patch.object(company_build, "_open_ledger", return_value=cast(Ledger, ledger)),
        pricing,
        factory,
        pytest.raises(RuntimeError) as caught,
    ):
        company_build.build(_config(tmp_path))

    assert caught.value is primary
    assert calls == ["ledger"]


def test_company_build_preserves_horizon_and_both_cleanup_failures(tmp_path: Path) -> None:
    calls: list[str] = []
    horizon_failure = RuntimeError("Horizon construction failed")
    horizon_close_failure = RuntimeError("Horizon close failed")
    ledger_close_failure = RuntimeError("ledger close failed")
    ledger_probe = _CloseProbe("ledger", calls, ledger_close_failure)
    connection_probe = _CloseProbe("horizon", calls, horizon_close_failure)
    ledger = cast(Ledger, ledger_probe)
    connection = cast(Connection[tuple[object, ...]], connection_probe)
    org = cast(Chorus, _OrgProbe(ledger))
    factory = cast(EmployeeHarnessFactory, _FactoryProbe(tmp_path / "company"))

    with (
        patch.object(company_build, "_open_ledger", return_value=ledger),
        patch.object(company_build, "default_pricing_from_env", return_value=object()),
        patch.object(company_build, "EmployeeHarnessFactory", return_value=factory),
        patch.object(company_build, "default_landers", return_value=[]),
        patch.object(company_build.Chorus, "build", return_value=org),
        patch.object(company_build, "ChorusGoalStore", return_value=object()),
        patch.object(company_build, "ChorusIntakePort", return_value=object()),
        patch.object(company_build, "DelegatedIntakeAdapter", return_value=object()),
        patch.object(company_build, "CapacityAdapter", return_value=object()),
        patch.object(company_build, "ChorusOutcomeFeed", return_value=object()),
        patch.object(company_build, "PostgresDecisionRepository", return_value=object()),
        patch.object(company_build, "PostgresStrategyRepository", return_value=object()),
        patch.object(company_build, "PostgresProposalRepository", return_value=object()),
        patch.object(company_build, "open_postgres_connection", return_value=connection),
        patch.object(company_build, "Horizon", side_effect=horizon_failure),
        pytest.raises(BaseExceptionGroup) as caught,
    ):
        company_build.build(_config(tmp_path))

    assert caught.value.exceptions == (
        horizon_failure,
        horizon_close_failure,
        ledger_close_failure,
    )
    assert calls == ["horizon", "ledger"]


def test_control_plane_provider_closes_ledger_when_horizon_open_fails() -> None:
    calls: list[str] = []
    ledger = cast(Ledger, _CloseProbe("ledger", calls))
    primary = RuntimeError("Horizon connection failed")
    provider = control_plane.ControlPlaneProvider(engine_dsn="postgresql://example.invalid/podium")

    with (
        patch.object(control_plane.Ledger, "open", return_value=ledger),
        patch.object(control_plane, "open_postgres_connection", side_effect=primary),
        pytest.raises(RuntimeError) as caught,
    ):
        provider.read_plane(workspace_id=uuid.uuid4(), company_id=uuid.uuid4())

    assert caught.value is primary
    assert calls == ["ledger"]


def test_control_plane_provider_preserves_direction_and_cleanup_failures() -> None:
    calls: list[str] = []
    primary = RuntimeError("direction construction failed")
    horizon_close_failure = RuntimeError("Horizon close failed")
    ledger_close_failure = RuntimeError("ledger close failed")
    ledger = cast(Ledger, _CloseProbe("ledger", calls, ledger_close_failure))
    connection = cast(
        Connection[tuple[object, ...]],
        _CloseProbe("horizon", calls, horizon_close_failure),
    )
    provider = control_plane.ControlPlaneProvider(engine_dsn="postgresql://example.invalid/podium")

    with (
        patch.object(control_plane.Ledger, "open", return_value=ledger),
        patch.object(control_plane, "open_postgres_connection", return_value=connection),
        patch.object(control_plane, "DirectionFacade", side_effect=primary),
        pytest.raises(BaseExceptionGroup) as caught,
    ):
        provider.read_plane(workspace_id=uuid.uuid4(), company_id=uuid.uuid4())

    assert caught.value.exceptions == (
        primary,
        horizon_close_failure,
        ledger_close_failure,
    )
    assert calls == ["horizon", "ledger"]


def test_company_graph_close_attempts_both_resources_and_preserves_errors() -> None:
    calls: list[str] = []
    horizon_failure = RuntimeError("Horizon close failed")
    ledger_failure = RuntimeError("ledger close failed")
    ledger_probe = _CloseProbe("ledger", calls, ledger_failure)
    connection_probe = _CloseProbe("horizon", calls, horizon_failure)
    ledger = cast(Ledger, ledger_probe)
    connection = cast(Connection[tuple[object, ...]], connection_probe)
    graph = company_build.CompanyGraph(
        org=cast(Chorus, _OrgProbe(ledger)),
        horizon=cast(Horizon, object()),
        governance=cast(HorizonGovernance, object()),
        factory=cast(EmployeeHarnessFactory, object()),
        ceo_factory=cast(EmployeeHarnessFactory, object()),
        horizon_connection=connection,
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        graph.close()

    assert caught.value.exceptions == (horizon_failure, ledger_failure)
    assert calls == ["horizon", "ledger"]

    connection_probe.failure = None
    ledger_probe.failure = None
    graph.close()
    graph.close()
    assert calls == ["horizon", "ledger", "horizon", "ledger", "horizon", "ledger"]


def test_control_plane_close_attempts_both_resources_and_is_retryable() -> None:
    calls: list[str] = []
    horizon_failure = RuntimeError("Horizon close failed")
    ledger_failure = RuntimeError("ledger close failed")
    ledger_probe = _CloseProbe("ledger", calls, ledger_failure)
    connection_probe = _CloseProbe("horizon", calls, horizon_failure)
    ledger = cast(Ledger, ledger_probe)
    connection = cast(Connection[tuple[object, ...]], connection_probe)
    plane = control_plane.CompanyControlPlane(
        workspace_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        ledger=ledger,
        horizon_connection=connection,
        direction=cast(DirectionFacade, object()),
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        plane.close()

    assert caught.value.exceptions == (horizon_failure, ledger_failure)
    assert calls == ["horizon", "ledger"]

    connection_probe.failure = None
    ledger_probe.failure = None
    plane.close()
    plane.close()

    assert calls == ["horizon", "ledger", "horizon", "ledger", "horizon", "ledger"]
