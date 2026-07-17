"""The product composition root (CP-0): typed sub-facades over a company's engines.

``CompanyControlPlane`` is to the product what ``src/company.build()`` is to the engine — the
one place the four engines' facades compose into a product surface. HTTP routers and dashboard
snapshots map onto its sub-facades 1:1; nothing past the plane sees engine internals.
"""

from podium.control._allocation import (
    AllocationBoard,
    AllocationFacade,
    BlockedTask,
    QueuedWake,
    RunningBeat,
)
from podium.control._delegation import CapacityEntry, DelegationFacade, TeamSummary
from podium.control._direction import DirectionFacade, GoalNode
from podium.control._observe import CompanyStatus, ObserveFacade, SkillSummary
from podium.control._plane import CompanyControlPlane, ControlPlaneProvider
from podium.control._workforce import EmployeeView, WorkforceFacade

__all__ = [
    "AllocationBoard",
    "AllocationFacade",
    "BlockedTask",
    "CapacityEntry",
    "CompanyControlPlane",
    "CompanyStatus",
    "ControlPlaneProvider",
    "DelegationFacade",
    "DirectionFacade",
    "EmployeeView",
    "GoalNode",
    "ObserveFacade",
    "QueuedWake",
    "RunningBeat",
    "SkillSummary",
    "TeamSummary",
    "WorkforceFacade",
]
