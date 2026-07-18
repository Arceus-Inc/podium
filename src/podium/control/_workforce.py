"""WorkforceFacade — the roster surface (chorus employees) as podium DTOs.

Writes delegate to a minimal REAL ``Chorus`` facade (roles registry + workforce over the same
RLS-scoped ledger, no dream/scheduler) so every engine invariant — role registry, slug
uniqueness, routine provisioning, terminate's cancel/drop sweep — applies unre-implemented."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chorus.errors import OrgInvariantViolation
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.facade import Chorus
    from chorus.ledger import Ledger


class UnknownRole(ValueError):
    """The role is not in the engine's registry."""


class DuplicateEmployee(ValueError):
    """The slug already exists in this company."""


class UnknownEmployeeError(ValueError):
    """No such employee in this company."""


class EmployeeConflict(ValueError):
    """The employee is not in a state this action applies to (e.g. terminated)."""


class EmployeeView(BaseModel):
    """One workforce member — the stable, serializable product shape of an engine employee."""

    model_config = ConfigDict(frozen=True)

    id: str  # the employee's slug within the company (e.g. "ada")
    name: str
    role: str
    status: str  # engine EmployeeStatus value: pending|idle|active|running|…


class BudgetView(BaseModel):
    """The employee's spend cap — the ceiling that hard-stops (and auto-pauses) on breach."""

    model_config = ConfigDict(frozen=True)

    scope_id: str
    amount_cents: int
    warn_percent: int
    hard_stop_enabled: bool
    window_kind: str


class WorkforceFacade:
    """Pure delegation to the engine's employee table; translation, never business logic."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def _org(self) -> Chorus:
        from chorus.facade import Caps, Chorus
        from chorus.observability import EventBus, LedgerInspector
        from chorus.roles import RoleRegistry, default_roles
        from chorus.workforce._ledger import LedgerWorkforce

        return Chorus(
            ledger=self._ledger,
            workforce=LedgerWorkforce(self._ledger.employees),
            memory_writer=None,  # type: ignore[arg-type]  # data edits never run beats
            scheduler=None,  # type: ignore[arg-type]
            event_bus=EventBus(),
            inspector=LedgerInspector(self._ledger),
            dream=None,
            roles=RoleRegistry.from_plugins(default_roles()),
            caps=Caps(),
        )

    def hire(self, *, name: str, role: str, reports_to: str | None = None) -> EmployeeView:
        """Hire through the engine facade; its invariants surface as typed errors."""
        try:
            employee = self._org().hire(name=name, role=role, reports_to=reports_to)
        except OrgInvariantViolation as exc:
            message = str(exc)
            if "unknown role" in message:
                raise UnknownRole(message) from exc
            raise DuplicateEmployee(message) from exc
        return EmployeeView(
            id=employee.id, name=employee.name, role=employee.role, status=employee.status.value
        )

    def terminate(self, employee_id: str) -> None:
        """Terminate through the engine facade (cancels runs, drops wakes)."""
        if self._ledger.employees.get(employee_id) is None:
            raise UnknownEmployeeError(employee_id)
        self._org().terminate(employee_id)

    def pause(self, employee_id: str) -> EmployeeView:
        """Pause the employee — invokability gates on PAUSED, so no new beat dispatches."""
        return self._set_status(employee_id, paused=True)

    def resume(self, employee_id: str) -> EmployeeView:
        """Resume a paused employee back to idle; the next wake dispatches normally."""
        return self._set_status(employee_id, paused=False)

    def _set_status(self, employee_id: str, *, paused: bool) -> EmployeeView:
        from chorus.workforce import EmployeeStatus

        employee = self._ledger.employees.get(employee_id)
        if employee is None:
            raise UnknownEmployeeError(employee_id)
        if employee.status is EmployeeStatus.TERMINATED:
            raise EmployeeConflict(f"{employee_id} is terminated")
        status = EmployeeStatus.PAUSED if paused else EmployeeStatus.IDLE
        self._ledger.employees.set_status(employee_id, status)
        refreshed = self._ledger.employees.get(employee_id)
        assert refreshed is not None  # just written under the same RLS scope
        return EmployeeView(
            id=refreshed.id,
            name=refreshed.name,
            role=refreshed.role,
            status=refreshed.status.value,
        )

    def set_budget(self, employee_id: str, *, amount_cents: int) -> BudgetView:
        """Upsert the employee's monthly cost cap — the hard-stop ceiling stays enabled."""
        import uuid as _uuid

        from chorus.ledger import BudgetPolicy, BudgetScope

        if self._ledger.employees.get(employee_id) is None:
            raise UnknownEmployeeError(employee_id)
        existing = self._ledger.budget_policies.find(
            scope_type=BudgetScope.EMPLOYEE, scope_id=employee_id
        )
        if existing is not None:
            self._ledger.budget_policies.set_amount(existing.id, amount_cents)
        else:
            self._ledger.budget_policies.create(
                BudgetPolicy(
                    id=str(_uuid.uuid4()),
                    scope_type=BudgetScope.EMPLOYEE,
                    scope_id=employee_id,
                    amount=amount_cents,
                )
            )
        policy = self._ledger.budget_policies.find(
            scope_type=BudgetScope.EMPLOYEE, scope_id=employee_id
        )
        assert policy is not None  # just upserted under the same RLS scope
        return BudgetView(
            scope_id=policy.scope_id,
            amount_cents=policy.amount,
            warn_percent=policy.warn_percent,
            hard_stop_enabled=policy.hard_stop_enabled,
            window_kind=policy.window_kind,
        )

    def export_bundle(self) -> dict[str, object]:
        """The portable workforce (spec 09 §3 fields) as one JSON bundle.

        ponytail: synchronous — a workforce is tens of rows; the 202-async job is the growth
        path when orgs outgrow one response body.
        """
        return {
            "format": "workforce/v1",
            "employees": [
                {
                    "id": employee.id,
                    "name": employee.name,
                    "role": employee.role,
                    "reports_to": employee.reports_to,
                    "memory_scope": employee.memory_scope,
                    "status": employee.status.value,
                }
                for employee in self._ledger.employees.list()
            ],
        }

    def roster(self) -> list[EmployeeView]:
        """Every employee of the company, engine order."""
        return [
            EmployeeView(
                id=employee.id,
                name=employee.name,
                role=employee.role,
                status=employee.status.value,
            )
            for employee in self._ledger.employees.list()
        ]


__all__ = [
    "BudgetView",
    "DuplicateEmployee",
    "EmployeeConflict",
    "EmployeeView",
    "UnknownEmployeeError",
    "UnknownRole",
    "WorkforceFacade",
]
