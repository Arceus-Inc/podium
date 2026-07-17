"""WorkforceFacade — the roster surface (chorus employees) as podium DTOs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from chorus.ledger import Ledger


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


__all__ = ["EmployeeView", "WorkforceFacade"]
