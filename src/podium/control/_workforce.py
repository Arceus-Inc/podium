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


class EmployeeView(BaseModel):
    """One workforce member — the stable, serializable product shape of an engine employee."""

    model_config = ConfigDict(frozen=True)

    id: str  # the employee's slug within the company (e.g. "ada")
    name: str
    role: str
    status: str  # engine EmployeeStatus value: pending|idle|active|running|…


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
    "DuplicateEmployee",
    "EmployeeView",
    "UnknownEmployeeError",
    "UnknownRole",
    "WorkforceFacade",
]
