"""CompanyControlPlane + ControlPlaneProvider — the seam itself (CP-0).

The READ plane opens the company's RLS-scoped engine ledger directly: no heartbeat, no model
keys, no workdir. That is the CQRS split M4 §3.2 designed and M5 unblocked — the api reads the
same Postgres ledger the conductor writes, concurrently and safely. The provider's DSN must be
the non-superuser runtime role (``podium_app``): FORCE RLS is the tenancy wall, and a superuser
would walk through it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from chorus.ledger import Ledger
from horizon.store.postgres import (
    PostgresDecisionRepository,
    PostgresProposalRepository,
    PostgresStrategyRepository,
    open_postgres_connection,
)
from psycopg import Connection

from podium.control._allocation import AllocationFacade
from podium.control._comments import CommentsFacade
from podium.control._delegation import DelegationFacade
from podium.control._direction import DirectionFacade
from podium.control._governance import GovernanceFacade
from podium.control._observe import ObserveFacade
from podium.control._routines import RoutinesFacade
from podium.control._session_state import SessionStateFacade
from podium.control._workforce import WorkforceFacade
from podium.evaluations import EvalRunComparisonFacade


class CompanyControlPlane:
    """One company's product surface: typed sub-facades over its engine state.

    Holds the engine ledger privately — routers and projections use the sub-facades only, so
    engine internals never escape the plane (M4 §3.1).
    """

    def __init__(
        self,
        *,
        workspace_id: uuid.UUID,
        company_id: uuid.UUID,
        ledger: Ledger,
        horizon_connection: Connection[tuple[object, ...]],
        direction: DirectionFacade,
    ) -> None:
        self.workspace_id = workspace_id
        self.company_id = company_id
        self._ledger = ledger
        self._horizon_connection = horizon_connection
        self._direction = direction

    @property
    def allocation(self) -> AllocationFacade:
        return AllocationFacade(self._ledger)

    @property
    def direction(self) -> DirectionFacade:
        return self._direction

    @property
    def workforce(self) -> WorkforceFacade:
        return WorkforceFacade(self._ledger)

    @property
    def delegation(self) -> DelegationFacade:
        return DelegationFacade(self._ledger, str(self.company_id))

    @property
    def governance(self) -> GovernanceFacade:
        return GovernanceFacade(self._ledger)

    @property
    def observe(self) -> ObserveFacade:
        return ObserveFacade(self._ledger)

    @property
    def routines(self) -> RoutinesFacade:
        return RoutinesFacade(self._ledger)

    @property
    def comments(self) -> CommentsFacade:
        return CommentsFacade(self._ledger)

    @property
    def evaluations(self) -> EvalRunComparisonFacade:
        return EvalRunComparisonFacade(self._ledger)

    def session_state(self) -> SessionStateFacade:
        return SessionStateFacade(self._ledger)

    def close(self) -> None:
        """Release the plane's Horizon and Chorus connections even if one close fails."""
        try:
            self._horizon_connection.close()
        finally:
            self._ledger.close()


@dataclass(frozen=True)
class ControlPlaneProvider:
    """Builds planes for companies. ``read_plane`` is heartbeat-free — safe in the api process."""

    engine_dsn: str = field(repr=False)  # runtime-role DSN; may embed credentials — never repr it

    def read_plane(self, *, workspace_id: uuid.UUID, company_id: uuid.UUID) -> CompanyControlPlane:
        ledger = Ledger.open(self.engine_dsn, company_id=str(company_id))
        horizon_connection: Connection[tuple[object, ...]] | None = None
        try:
            horizon_connection = open_postgres_connection(self.engine_dsn, company_id=company_id)
            direction = DirectionFacade(
                ledger,
                decisions=PostgresDecisionRepository(horizon_connection),
                strategy=PostgresStrategyRepository(horizon_connection),
                proposals=PostgresProposalRepository(horizon_connection),
            )
            return CompanyControlPlane(
                workspace_id=workspace_id,
                company_id=company_id,
                ledger=ledger,
                horizon_connection=horizon_connection,
                direction=direction,
            )
        except BaseException:
            if horizon_connection is not None:
                horizon_connection.close()
            ledger.close()
            raise


__all__ = ["CompanyControlPlane", "ControlPlaneProvider"]
