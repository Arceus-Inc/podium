"""Typed persistence and bounded traversal for entity lineage."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Integer, String, bindparam, column, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID, insert
from sqlalchemy.ext.asyncio import AsyncSession

from podium.entity_edges.models import EntityEdge, EntityPredicate

PredicateInput = EntityPredicate | str
MAX_TRAVERSAL_HOPS = 4


class UnknownEntityPredicate(ValueError):
    """Raised when a caller supplies a predicate outside the stable vocabulary."""


class TraversalBudgetExhausted(RuntimeError):
    """Raised when traversal would return more edges than its caller permitted."""


@dataclass(frozen=True, slots=True)
class EntityEdgeDraft:
    src_type: str
    src_id: str
    predicate: PredicateInput
    dst_type: str
    dst_id: str
    run_id: uuid.UUID | None = None
    employee_id: str | None = None


class EntityEdgeConflictError(RuntimeError):
    """Raised when replay changes immutable provenance for an existing edge."""

    def __init__(self, *, existing: EntityEdge, requested: EntityEdgeDraft) -> None:
        self.edge_id = existing.id
        self.existing_run_id = existing.run_id
        self.requested_run_id = requested.run_id
        self.existing_employee_id = existing.employee_id
        self.requested_employee_id = requested.employee_id
        super().__init__(f"entity edge {existing.id} already has different immutable provenance")


@dataclass(frozen=True, slots=True)
class TraversedEntityEdge:
    edge: EntityEdge
    hop: int


def _predicate_values(predicates: Sequence[PredicateInput]) -> tuple[str, ...]:
    values: set[str] = set()
    for predicate in predicates:
        try:
            values.add(EntityPredicate(predicate).value)
        except (TypeError, ValueError) as exc:
            raise UnknownEntityPredicate(f"unknown entity predicate: {predicate!r}") from exc
    return tuple(sorted(values))


class EntityEdgeRepository:
    """A repository pinned to one company and workspace for its entire lifetime."""

    def __init__(
        self, session: AsyncSession, *, company_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> None:
        self._session = session
        self._company_id = company_id
        self._workspace_id = workspace_id

    async def append(self, draft: EntityEdgeDraft) -> EntityEdge:
        """Insert an edge once; replaying the same edge returns the original row."""
        predicate = _predicate_values((draft.predicate,))[0]
        stmt = (
            insert(EntityEdge)
            .values(
                company_id=self._company_id,
                workspace_id=self._workspace_id,
                src_type=draft.src_type,
                src_id=draft.src_id,
                predicate=predicate,
                dst_type=draft.dst_type,
                dst_id=draft.dst_id,
                run_id=draft.run_id,
                employee_id=draft.employee_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "company_id",
                    "src_type",
                    "src_id",
                    "predicate",
                    "dst_type",
                    "dst_id",
                ]
            )
            .returning(EntityEdge)
        )
        created = (await self._session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created

        existing = await self._session.scalar(
            select(EntityEdge).where(
                EntityEdge.company_id == self._company_id,
                EntityEdge.src_type == draft.src_type,
                EntityEdge.src_id == draft.src_id,
                EntityEdge.predicate == predicate,
                EntityEdge.dst_type == draft.dst_type,
                EntityEdge.dst_id == draft.dst_id,
            )
        )
        if existing is None:
            raise RuntimeError("entity edge was neither inserted nor visible after replay")
        if existing.run_id != draft.run_id or existing.employee_id != draft.employee_id:
            raise EntityEdgeConflictError(existing=existing, requested=draft)
        return existing

    async def traverse(
        self,
        root_type: str,
        root_id: str,
        predicates: Sequence[PredicateInput],
        hops: int = 2,
        budget: int = 100,
    ) -> list[TraversedEntityEdge]:
        """Follow outbound lineage edges without revisiting a node on the same path."""
        if hops < 0:
            raise ValueError("hops must be non-negative")
        if hops > MAX_TRAVERSAL_HOPS:
            raise ValueError(f"hops must not exceed {MAX_TRAVERSAL_HOPS}")
        if budget < 1:
            raise ValueError("budget must be positive")
        predicate_values = _predicate_values(predicates)
        if not predicate_values or hops == 0:
            return []

        # ponytail: recursive CTE work is bounded by four hops; raise the ceiling only after a
        # measured need, because `budget` limits returned unique edges rather than CTE path rows.
        walk = (
            text(
                """
                WITH RECURSIVE walk AS (
                    SELECT
                        e.id, e.src_type, e.src_id, e.dst_type, e.dst_id, 1 AS hop,
                        ARRAY[
                            jsonb_build_array(e.src_type, e.src_id)::text,
                            jsonb_build_array(e.dst_type, e.dst_id)::text
                        ] AS path,
                        jsonb_build_array(e.dst_type, e.dst_id)::text =
                            jsonb_build_array(:root_type, :root_id)::text AS closes_cycle
                    FROM entity_edges AS e
                    WHERE e.company_id = :company_id
                      AND e.workspace_id = :workspace_id
                      AND e.src_type = :root_type
                      AND e.src_id = :root_id
                      AND e.predicate = ANY(CAST(:predicates AS entity_edge_predicate[]))

                    UNION ALL

                    SELECT
                        e.id, e.src_type, e.src_id, e.dst_type, e.dst_id, walk.hop + 1,
                        walk.path || jsonb_build_array(e.dst_type, e.dst_id)::text,
                        jsonb_build_array(e.dst_type, e.dst_id)::text = ANY(walk.path)
                    FROM entity_edges AS e
                    JOIN walk ON e.src_type = walk.dst_type AND e.src_id = walk.dst_id
                    WHERE e.company_id = :company_id
                      AND e.workspace_id = :workspace_id
                      AND e.predicate = ANY(CAST(:predicates AS entity_edge_predicate[]))
                      AND walk.hop < :hops
                      AND NOT walk.closes_cycle
                )
                SELECT id, min(hop) AS hop
                FROM walk
                GROUP BY id
                """
            )
            .bindparams(
                bindparam("company_id", type_=UUID(as_uuid=True)),
                bindparam("workspace_id", type_=UUID(as_uuid=True)),
                bindparam("root_type", type_=String()),
                bindparam("root_id", type_=String()),
                bindparam("predicates", type_=ARRAY(String())),
                bindparam("hops", type_=Integer()),
            )
            .columns(column("id", UUID(as_uuid=True)), column("hop", Integer()))
            .subquery("walk")
        )
        statement = (
            select(EntityEdge, walk.c.hop)
            .join(walk, EntityEdge.id == walk.c.id)
            .order_by(
                walk.c.hop,
                EntityEdge.src_type,
                EntityEdge.src_id,
                EntityEdge.predicate,
                EntityEdge.dst_type,
                EntityEdge.dst_id,
                EntityEdge.id,
            )
            .limit(budget + 1)
        )
        rows = (
            await self._session.execute(
                statement,
                {
                    "company_id": self._company_id,
                    "workspace_id": self._workspace_id,
                    "root_type": root_type,
                    "root_id": root_id,
                    "predicates": list(predicate_values),
                    "hops": hops,
                },
            )
        ).all()
        if len(rows) > budget:
            raise TraversalBudgetExhausted(f"entity edge traversal budget {budget} exhausted")
        return [TraversedEntityEdge(edge=edge, hop=hop) for edge, hop in rows]
